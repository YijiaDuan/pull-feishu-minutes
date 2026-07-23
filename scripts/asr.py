#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可选的 ASR 兜底：飞书免费版只有 300 分钟转写额度，超额的妙记没有逐字稿、
或只转了开头一小段。本模块把这类妙记的音频抠出来 → 传对象存储拿公网 URL →
交给语音大模型转写（带说话人分离 + 时间戳），补出逐字稿。

**完全可选**。缺环境变量时自动禁用，没转写的妙记会被跳过。

支持两个后端，`FEISHU_ASR_BACKEND` 选（默认 auto）：
  volcano    火山引擎 豆包语音大模型（Seed-ASR，录音文件极速版）——中文/专有名词更准，推荐
  paraformer 阿里云百炼 Paraformer
  auto       有 VOLC_ASR_KEY 走 volcano，否则有 DASHSCOPE_API_KEY 走 paraformer

环境变量：
  # 中转（两个后端都要，语音服务需要它够得到的公网 URL）
  ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET
  FEISHU_ASR_OSS_BUCKET               中转 OSS bucket
  FEISHU_ASR_OSS_ENDPOINT             默认 oss-cn-hangzhou.aliyuncs.com
  # 火山后端
  VOLC_ASR_KEY                        火山 X-Api-Key（控制台开通「录音文件识别大模型版」后获取）
  VOLC_ASR_RESOURCE_ID               默认 volc.bigasr.auc_turbo（极速版）
  # 百炼后端
  DASHSCOPE_API_KEY

火山后端需要 ffmpeg（把飞书的 m4a 转成 mp3，火山极速版只收 wav/mp3/ogg）。
"""

import os
import json
import time
import uuid
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

DASH_SUBMIT = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
DASH_TASK = "https://dashscope.aliyuncs.com/api/v1/tasks/"
VOLC_FLASH = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"


# ---------- 后端选择 ----------

def backend():
    b = os.environ.get("FEISHU_ASR_BACKEND", "auto").lower()
    if b in ("volcano", "volc"):
        return "volcano"
    if b in ("paraformer", "dashscope", "bailian"):
        return "paraformer"
    if os.environ.get("VOLC_ASR_KEY"):
        return "volcano"
    if os.environ.get("DASHSCOPE_API_KEY"):
        return "paraformer"
    return None


def backend_label():
    return {"volcano": "volcano-seed-asr",
            "paraformer": "dashscope-paraformer"}.get(backend(), "asr")


def missing_env():
    miss = [k for k in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET",
                        "FEISHU_ASR_OSS_BUCKET") if not os.environ.get(k)]
    b = backend()
    if b is None:
        miss.append("VOLC_ASR_KEY 或 DASHSCOPE_API_KEY")
    elif b == "volcano" and not shutil.which("ffmpeg"):
        miss.append("ffmpeg（火山后端需要，用于 m4a→mp3）")
    return miss


def asr_available():
    return not missing_env()


# ---------- 从飞书取音频 ----------

def get_audio_src(page, token, base):
    page.goto(f"{base}/minutes/{token}", wait_until="domcontentloaded", timeout=60000)
    for _ in range(25):
        src = page.evaluate(
            "()=>{const a=document.querySelector('audio');return a?a.currentSrc||a.src:null;}")
        if src:
            return src
        time.sleep(1)
    return None


def download_audio(ctx, src, dest):
    resp = ctx.request.get(src, headers={"Referer": "https://meetings.feishu.cn/"},
                           timeout=300000)
    if resp.status not in (200, 206):
        raise RuntimeError(f"音频下载 HTTP {resp.status}")
    with open(dest, "wb") as f:
        f.write(resp.body())


def to_mp3(m4a_path, mp3_path):
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", m4a_path, "-ac", "1", "-ar", "16000", "-b:a", "64k", mp3_path],
        capture_output=True)
    if r.returncode != 0 or not os.path.exists(mp3_path):
        raise RuntimeError("ffmpeg 转码失败：" + r.stderr.decode("utf8", "replace")[-300:])


# ---------- OSS 中转 ----------

def upload_oss(local_path, object_key):
    import oss2
    endpoint = os.environ.get("FEISHU_ASR_OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    auth = oss2.Auth(os.environ["ALIYUN_ACCESS_KEY_ID"], os.environ["ALIYUN_ACCESS_KEY_SECRET"])
    bucket = oss2.Bucket(auth, endpoint, os.environ["FEISHU_ASR_OSS_BUCKET"])
    bucket.put_object_from_file(object_key, local_path)
    return bucket, bucket.sign_url("GET", object_key, 7200)


# ---------- 后端：火山 Seed-ASR ----------

def _volcano(mp3_url):
    h = {"X-Api-Key": os.environ["VOLC_ASR_KEY"],
         "X-Api-Resource-Id": os.environ.get("VOLC_ASR_RESOURCE_ID", "volc.bigasr.auc_turbo"),
         "X-Api-Request-Id": str(uuid.uuid4()), "X-Api-Sequence": "-1",
         "Content-Type": "application/json"}
    body = json.dumps({
        "user": {"uid": "feishu-minutes-skill"},
        "audio": {"url": mp3_url, "format": "mp3"},
        "request": {"model_name": "bigmodel", "enable_itn": True,
                    "enable_punc": True, "enable_speaker_info": True},
    }).encode()
    req = urllib.request.Request(VOLC_FLASH, data=body, headers=h, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=600)
        sc, raw = r.headers.get("X-Api-Status-Code"), r.read()
    except urllib.error.HTTPError as e:
        sc, raw = e.headers.get("X-Api-Status-Code"), e.read()
    if str(sc) != "20000000":
        raise RuntimeError(f"火山转写失败 status={sc}：{raw.decode('utf8', 'replace')[:200]}")
    utts = (json.loads(raw).get("result") or {}).get("utterances") or []
    rows = []
    for u in utts:
        spk = str(u.get("additions", {}).get("speaker") or u.get("speaker") or "0")
        rows.append((spk, u.get("start_time", 0), u.get("text", "")))
    return rows


# ---------- 后端：百炼 Paraformer ----------

def _paraformer(audio_url):
    key = os.environ["DASHSCOPE_API_KEY"]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = json.dumps({"model": "paraformer-v2", "input": {"file_urls": [audio_url]},
                       "parameters": {"language_hints": ["zh"], "diarization_enabled": True}}).encode()
    req = urllib.request.Request(DASH_SUBMIT, data=body,
                                 headers={**hdr, "X-DashScope-Async": "enable"}, method="POST")
    task_id = json.loads(urllib.request.urlopen(req, timeout=30).read())["output"]["task_id"]
    t0 = time.time()
    while time.time() - t0 < 1200:
        time.sleep(6)
        s = json.loads(urllib.request.urlopen(
            urllib.request.Request(DASH_TASK + task_id, headers=hdr), timeout=30).read())
        st = s["output"]["task_status"]
        if st == "SUCCEEDED":
            turl = s["output"]["results"][0]["transcription_url"]
            rj = json.loads(urllib.request.urlopen(turl, timeout=60).read())
            sents = (rj.get("transcripts") or [{}])[0].get("sentences") or []
            return [(str(x.get("speaker_id", "0")), x.get("begin_time", 0), x.get("text", ""))
                    for x in sents]
        if st == "FAILED":
            raise RuntimeError("Paraformer 转写失败：" +
                               json.dumps(s.get("output", {}), ensure_ascii=False)[:300])
    raise RuntimeError("Paraformer 转写超时")


# ---------- 组装成与飞书导出同构的文本 ----------

def _assemble(rows, meta):
    """rows: [(speaker_id, start_ms, text)] → 飞书导出同构的纯文本（头部 + 说话人段落）。
    下游 parse_transcript / build_markdown 可不加区分地复用。"""
    def ts(ms):
        s = int(ms / 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    paras, cur, buf, start = [], None, [], None
    for spk, ms, text in rows:
        if spk != cur:
            if buf:
                paras.append(f"说话人 {int(cur) + 1} {start}\n{''.join(buf)}")
            cur, buf, start = spk, [text], ts(ms)
        else:
            buf.append(text)
    if buf:
        paras.append(f"说话人 {int(cur) + 1} {start}\n{''.join(buf)}")

    dt = None
    try:
        dt = datetime.fromtimestamp(int(meta.get("start_time") or meta.get("create_time")) / 1000)
    except (TypeError, ValueError):
        pass
    d = int(meta.get("duration") or 0)
    h, mm, ss = d // 3600000, d % 3600000 // 60000, d % 60000 // 1000
    dur = (f"{h}小时 {mm}分钟 {ss}秒" if h else f"{mm}分钟 {ss}秒") if d else ""
    header = f"{dt:%Y-%m-%d %H:%M} CST|{dur}" if dt else dur
    return f"{header}\n\n文字记录:\n\n" + "\n\n".join(paras) + "\n", len(rows)


# ---------- 对外：对一条妙记做完整 ASR 兜底 ----------

def transcribe_minute(page, ctx, token, base, meta, tmp_dir):
    src = get_audio_src(page, token, base)
    if not src:
        raise RuntimeError("拿不到音频地址（可能是无音频的纯文档妙记）")
    m4a = os.path.join(tmp_dir, f".asr_{token}.m4a")
    mp3 = os.path.join(tmp_dir, f".asr_{token}.mp3")
    b = backend()
    tmp_files = [m4a]
    try:
        download_audio(ctx, src, m4a)
        if b == "volcano":
            to_mp3(m4a, mp3)
            tmp_files.append(mp3)
            bucket, url = upload_oss(mp3, f"_feishu_asr_tmp/{token}.mp3")
            okey = f"_feishu_asr_tmp/{token}.mp3"
        else:
            bucket, url = upload_oss(m4a, f"_feishu_asr_tmp/{token}.m4a")
            okey = f"_feishu_asr_tmp/{token}.m4a"
        try:
            rows = _volcano(url) if b == "volcano" else _paraformer(url)
        finally:
            try:
                bucket.delete_object(okey)
            except Exception:
                pass
        return _assemble(rows, meta)
    finally:
        for f in tmp_files:
            if os.path.exists(f):
                os.remove(f)
