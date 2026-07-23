#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可选的 ASR 兜底：飞书免费版只有 300 分钟转写额度，超额的妙记没有逐字稿。
本模块把这类妙记的音频抠出来 → 传到对象存储拿一个公网 URL → 交给
阿里云百炼 Paraformer 转写（带说话人分离 + 时间戳），补出逐字稿。

**完全可选**。只有当以下环境变量齐备时才会启用；否则没转写的妙记会被跳过。

  DASHSCOPE_API_KEY           百炼 API key（语音转写 + 文本都用它）
  ALIYUN_ACCESS_KEY_ID        OSS 上传用（做中转，百炼需要公网 URL 才能取音频）
  ALIYUN_ACCESS_KEY_SECRET
  FEISHU_ASR_OSS_BUCKET       用作中转的 OSS bucket 名
  FEISHU_ASR_OSS_ENDPOINT     OSS endpoint，默认 oss-cn-hangzhou.aliyuncs.com

为什么要 OSS 中转：飞书音频地址是登录态保护的，百炼够不着；百炼的录音文件
识别接口只收「它自己能访问的公网 URL」。音频进的是你自己的私有 bucket，用完
即删，且只给百炼一个 2 小时过期的签名 URL，不公开。
"""

import os
import json
import time
import urllib.request

ASR_SUBMIT = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
ASR_TASK = "https://dashscope.aliyuncs.com/api/v1/tasks/"


def asr_available():
    """所需环境变量是否齐备。"""
    need = ["DASHSCOPE_API_KEY", "ALIYUN_ACCESS_KEY_ID",
            "ALIYUN_ACCESS_KEY_SECRET", "FEISHU_ASR_OSS_BUCKET"]
    return all(os.environ.get(k) for k in need)


def missing_env():
    need = ["DASHSCOPE_API_KEY", "ALIYUN_ACCESS_KEY_ID",
            "ALIYUN_ACCESS_KEY_SECRET", "FEISHU_ASR_OSS_BUCKET"]
    return [k for k in need if not os.environ.get(k)]


def get_audio_src(page, token, base):
    """打开妙记播放页，读出 <audio> 的实际地址。"""
    page.goto(f"{base}/minutes/{token}", wait_until="domcontentloaded", timeout=60000)
    for _ in range(25):
        src = page.evaluate(
            "()=>{const a=document.querySelector('audio');return a?a.currentSrc||a.src:null;}")
        if src:
            return src
        time.sleep(1)
    return None


def download_audio(ctx, src, dest):
    """带登录态下载音频到本地。"""
    resp = ctx.request.get(src, headers={"Referer": "https://meetings.feishu.cn/"},
                           timeout=300000)
    if resp.status not in (200, 206):
        raise RuntimeError(f"音频下载 HTTP {resp.status}")
    data = resp.body()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def upload_oss(local_path, object_key):
    """传到 OSS，返回 2 小时签名 URL。"""
    import oss2
    endpoint = os.environ.get("FEISHU_ASR_OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    auth = oss2.Auth(os.environ["ALIYUN_ACCESS_KEY_ID"],
                     os.environ["ALIYUN_ACCESS_KEY_SECRET"])
    bucket = oss2.Bucket(auth, endpoint, os.environ["FEISHU_ASR_OSS_BUCKET"])
    bucket.put_object_from_file(object_key, local_path)
    url = bucket.sign_url("GET", object_key, 7200)
    return bucket, url


def transcribe(file_url, timeout=1200):
    """提交 Paraformer 异步任务并轮询，返回结果 JSON（transcripts 结构）。"""
    key = os.environ["DASHSCOPE_API_KEY"]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = json.dumps({
        "model": "paraformer-v2",
        "input": {"file_urls": [file_url]},
        "parameters": {"language_hints": ["zh"], "diarization_enabled": True},
    }).encode()
    req = urllib.request.Request(ASR_SUBMIT, data=body,
                                 headers={**hdr, "X-DashScope-Async": "enable"}, method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    task_id = d["output"]["task_id"]

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(6)
        r = urllib.request.Request(ASR_TASK + task_id, headers=hdr)
        s = json.loads(urllib.request.urlopen(r, timeout=30).read())
        st = s["output"]["task_status"]
        if st == "SUCCEEDED":
            turl = s["output"]["results"][0]["transcription_url"]
            return json.loads(urllib.request.urlopen(turl, timeout=60).read())
        if st == "FAILED":
            raise RuntimeError("Paraformer 转写失败：" +
                               json.dumps(s.get("output", {}), ensure_ascii=False)[:300])
    raise RuntimeError("Paraformer 转写超时")


def _fmt_ts(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def paraformer_to_transcript(result_json, meta):
    """把 Paraformer 结果转成与飞书导出同构的纯文本（头部 + 说话人段落）。

    这样下游的 parse_transcript / build_markdown 能不加区分地复用。
    """
    trans = result_json.get("transcripts") or []
    sents = trans[0].get("sentences") if trans else []
    lines = []
    cur, buf, start = None, [], None
    for s in sents or []:
        spk = str(s.get("speaker_id", "0"))
        if spk != cur:
            if buf:
                lines.append(f"说话人 {int(cur) + 1} {start}\n{''.join(buf)}")
            cur, buf, start = spk, [s.get("text", "")], _fmt_ts(s.get("begin_time", 0))
        else:
            buf.append(s.get("text", ""))
    if buf:
        lines.append(f"说话人 {int(cur) + 1} {start}\n{''.join(buf)}")

    # 头部：与飞书导出对齐（首行 时间|时长，再 关键词/文字记录）
    from datetime import datetime
    dt = None
    try:
        dt = datetime.fromtimestamp(int(meta.get("start_time") or meta.get("create_time")) / 1000)
    except (TypeError, ValueError):
        pass
    dur_ms = int(meta.get("duration") or 0)
    h, mm, ss = dur_ms // 3600000, dur_ms % 3600000 // 60000, dur_ms % 60000 // 1000
    dur = (f"{h}小时 {mm}分钟 {ss}秒" if h else f"{mm}分钟 {ss}秒") if dur_ms else ""
    header = f"{dt:%Y-%m-%d %H:%M} CST|{dur}" if dt else dur
    body = "\n\n".join(lines)
    return f"{header}\n\n文字记录:\n\n{body}\n", len(sents or [])


def transcribe_minute(page, ctx, token, base, meta, tmp_dir):
    """对一条没有飞书转写的妙记做完整 ASR 兜底，返回 (逐字稿文本, 句数)。"""
    src = get_audio_src(page, token, base)
    if not src:
        raise RuntimeError("拿不到音频地址（可能是无音频的纯文档妙记）")
    audio = os.path.join(tmp_dir, f".asr_{token}.m4a")
    try:
        download_audio(ctx, src, audio)
        bucket, url = upload_oss(audio, f"_feishu_asr_tmp/{token}.m4a")
        try:
            result = transcribe(url)
        finally:
            try:
                bucket.delete_object(f"_feishu_asr_tmp/{token}.m4a")
            except Exception:
                pass
        return paraformer_to_transcript(result, meta)
    finally:
        if os.path.exists(audio):
            os.remove(audio)
