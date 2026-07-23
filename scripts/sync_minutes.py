#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书妙记 → 本地 Markdown 同步器

用浏览器登录态（Playwright 持久化配置）拉取「我的妙记」全部条目，
把还没拉过的导出成带 frontmatter 的 Markdown。首次全量，之后增量。

不需要自建飞书应用、不需要申请 API 权限、不需要管理员审批——
用户只要在弹出的浏览器里登录一次飞书即可，之后登录态会保存在本地配置目录。

用法:
    python sync_minutes.py --out <输出目录> [--profile <浏览器配置目录>]
                           [--headless] [--limit N] [--timeout 300]

输出:
    正常日志走 stderr；stdout 最后打印一行 JSON 结果，供调用方（AI agent）解析：
    {"ok":true,"total":N,"new":[{"token":..,"title":..,"path":..}],"skipped":M}
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # 可选的 ASR 兜底，未配 key 时自动禁用

LIST_HOST = "https://meetings.feishu.cn"
LIST_API = LIST_HOST + "/minutes/api/space/list"
EXPORT_API = LIST_HOST + "/minutes/api/export"
HOME_URL = LIST_HOST + "/minutes/me"
STATE_FILE = ".feishu_minutes_state.json"
# 登录态放用户级目录，换输出目录不必重新登录
DEFAULT_PROFILE = os.path.expanduser("~/.config/feishu-minutes/browser-profile")

# 飞书是国内域名，走系统代理常出问题，直连
NO_PROXY_HOSTS = "feishu.cn,*.feishu.cn,larksuite.com,*.larksuite.com"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_state(out_dir):
    p = os.path.join(out_dir, STATE_FILE)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"minutes": {}}


def save_state(out_dir, state):
    with open(os.path.join(out_dir, STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def scan_existing_tokens(out_dir):
    """扫已有 md 的 frontmatter，兼容手工改名/state 丢失的情况。"""
    found = set()
    if not os.path.isdir(out_dir):
        return found
    for fn in os.listdir(out_dir):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                head = f.read(800)
        except OSError:
            continue
        m = re.search(r"^minute_token:\s*(\S+)", head, re.M)
        if m:
            found.add(m.group(1))
    return found


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:80] or "未命名妙记"


# ---------- 浏览器与登录 ----------

# 未登录时妙记页面导航里会出现的登录入口（多语言）。
# 注意：不能靠接口判断——未登录时 space/list 同样返回 {"code":0,"list":[]}，
# 与「已登录但没有妙记」完全无法区分，会造成静默的假成功。
LOGIN_HINTS = [
    "Sign Up/Log In", "Sign up/Log in", "Sign Up / Log In",
    "登录/注册", "注册/登录", "登入/註冊", "註冊/登入",
]


def is_logged_in(page):
    """靠页面上有没有「登录/注册」入口来判断。有 = 未登录。

    绝不 reload：用户可能正停在登录页扫码或输密码，刷新会把登录流程冲掉。
    用户不在妙记页时（比如正跳转到 accounts.feishu.cn 登录），直接判为「还没好」，
    等飞书登录完自己跳回来。
    """
    # 不要用 wait_for_load_state("networkidle")：妙记页面有长连接，
    # 永远到不了 networkidle，会一直超时抛异常，被吞掉后表现为「死等登录」。
    try:
        if "/minutes" not in page.url:
            return False
        body = page.inner_text("body", timeout=5000)
    except Exception:
        return False

    # 应用外壳渲染出来了才敢下结论，否则说明还在加载，继续等。
    shell = ("My content", "Shared content", "Trash",
             "我的内容", "共享内容", "回收站")
    if not any(s in body for s in shell):
        return False

    return not any(h in body for h in LOGIN_HINTS)


def wait_for_login(page, timeout):
    """等待用户在浏览器里完成登录。返回 True/False。"""
    deadline = time.time() + timeout
    warned = False
    streak = 0
    while time.time() < deadline:
        try:
            # 连续两次判定一致才认账：页面加载中途的骨架内容可能刚好
            # 「有外壳、没登录按钮」，单次判断会误判成已登录。
            if is_logged_in(page):
                streak += 1
                if streak >= 2:
                    return True
            else:
                streak = 0
        except Exception:
            streak = 0
        if not warned:
            log("")
            log("=" * 62)
            log("  请在弹出的浏览器窗口里登录飞书（扫码或账号密码均可）。")
            log("  登录成功后本脚本会自动继续，无需其他操作。")
            log("=" * 62)
            log("")
            warned = True
        time.sleep(3)
    return False


def _ts_to_sec(ts):
    parts = [int(x) for x in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def transcript_incomplete(paras, duration_ms):
    """判断飞书转写是不是残缺（免费额度耗尽时会只转开头一小段就停）。

    信号：转写段落覆盖到的最后时间点，只占录音时长的一小部分。
    不看总字数——一段真实的长转写和一段被截断的短转写，靠覆盖率才分得清。
    """
    if not paras:
        return True
    try:
        dur = int(duration_ms) / 1000
    except (TypeError, ValueError):
        return False  # 不知道时长就别瞎触发 ASR
    if dur <= 0:
        return False
    last = max(_ts_to_sec(ts) for _, ts, _ in paras)
    return last < dur * 0.5  # 覆盖不到一半 = 残缺


def api_base(page):
    """登录后飞书会跳到企业专属域名（如 xxx.feishu.cn），用页面当前源发请求，
    避免跨域，也避免拿不到 cookie。"""
    try:
        m = re.match(r"(https://[^/]+\.feishu\.cn)", page.url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return LIST_HOST


def get_csrf(ctx, page):
    """取与页面同源的 bv_csrf_token。

    每个域名（meetings.feishu.cn 与企业专属域名）各有一份不同的 token，
    取错域会让导出接口一律返回 HTTP 400。
    """
    host = re.sub(r"^https?://", "", api_base(page))
    fallback = ""
    for c in ctx.cookies():
        if c.get("name") != "bv_csrf_token":
            continue
        if c.get("domain", "").lstrip(".") == host:
            return c.get("value", "")
        fallback = fallback or c.get("value", "")
    return fallback


def call_list(page, timestamp):
    """在页面上下文里 fetch 列表接口（cookie 自动携带）。失败返回 None。"""
    url = f"{api_base(page)}/minutes/api/space/list?size=20&space_name="
    if timestamp:
        url += f"&timestamp={timestamp}"
    res = page.evaluate(
        """async (url) => {
            const r = await fetch(url, {
                method: 'GET',
                credentials: 'include',
                headers: {'referer': 'https://meetings.feishu.cn/minutes/me'}
            });
            const t = await r.text();
            try { return {status: r.status, json: JSON.parse(t)}; }
            catch (e) { return {status: r.status, json: null}; }
        }""",
        url,
    )
    j = res.get("json")
    if not j or j.get("code") != 0:
        return None
    return j.get("data") or {}


def fetch_all_minutes(page):
    """分页拉全部妙记。翻页游标是上一页最后一条的 share_time。"""
    out, timestamp, seen = [], None, set()
    for _ in range(500):  # 安全上限
        data = call_list(page, timestamp)
        if data is None:
            raise RuntimeError("列表接口返回异常（登录态可能已失效）")
        lst = data.get("list") or []
        if not lst:
            break
        fresh = [m for m in lst if m.get("object_token") not in seen]
        for m in fresh:
            seen.add(m.get("object_token"))
        out.extend(fresh)
        if not data.get("has_more") or not fresh:
            break
        timestamp = lst[-1].get("share_time")
        if not timestamp:
            break
    return out


def export_transcript(page, token, csrf):
    """导出逐字稿纯文本（带说话人+时间戳）。

    这是个 POST 接口，必须带 bv-csrf-token 请求头，否则一律 HTTP 400。
    """
    params = f"object_token={token}&add_speaker=true&add_timestamp=true&format=2"
    export_url = f"{api_base(page)}/minutes/api/export"
    res = page.evaluate(
        """async ({url, csrf, ref}) => {
            const r = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'referer': ref,
                    'bv-csrf-token': csrf,
                    'content-type': 'application/x-www-form-urlencoded'
                },
                body: ''
            });
            return {status: r.status, text: await r.text()};
        }""",
        {"url": f"{export_url}?{params}", "csrf": csrf,
         "ref": f"{api_base(page)}/minutes/me"},
    )
    if res.get("status") != 200:
        raise RuntimeError(f"导出接口 HTTP {res.get('status')}")
    return res.get("text") or ""


# ---------- 逐字稿解析与 Markdown ----------

def parse_transcript(raw):
    """返回 (录制时间, 时长, 关键词, [(说话人, 时间戳, 正文)])"""
    lines = raw.splitlines()
    rec_time = duration = keywords = ""
    if lines and "|" in lines[0]:
        rec_time, duration = [x.strip() for x in lines[0].split("|", 1)]

    for i, ln in enumerate(lines):
        if ln.strip().startswith("关键词"):
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    keywords = nxt.strip()
                    break
            break

    paras, cur = [], None
    pat = re.compile(r"^说话人\s*(\d+)\s+(\d{1,2}:\d{2}(?::\d{2})?)(?:\.\d+)?\s*$")
    for ln in lines:
        m = pat.match(ln.strip())
        if m:
            if cur:
                paras.append(cur)
            cur = [f"说话人 {m.group(1)}", m.group(2), ""]
        elif cur is not None and ln.strip():
            cur[2] = (cur[2] + " " + ln.strip()).strip()
    if cur:
        paras.append(cur)
    return rec_time, duration, keywords, paras


def ms_to_dt(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000)
    except (TypeError, ValueError):
        return None


def build_markdown(meta, raw):
    token = meta.get("object_token")
    title = meta.get("topic") or "未命名妙记"
    rec_time, duration, keywords, paras = parse_transcript(raw)

    dt = ms_to_dt(meta.get("start_time") or meta.get("create_time"))
    date_str = dt.strftime("%Y-%m-%d %H:%M") if dt else (rec_time or "")
    day = dt.strftime("%Y-%m-%d") if dt else (date_str[:10] or "undated")

    url = meta.get("url") or f"{LIST_HOST}/minutes/{token}"
    speakers = sorted({p[0] for p in paras}, key=lambda s: int(s.split()[-1]))
    kw = " · ".join(k for k in (x.strip() for x in re.split(r"[、,，]+", keywords)) if k)

    lines = [
        "---",
        f'title: "{title}"',
        "type: 飞书妙记",
        "场景: 待判断",
        f"date: {date_str}",
        f"duration: {duration}",
        f"source: {url}",
        f"minute_token: {token}",
        f"speakers: {len(speakers)}",
        f"imported: {datetime.now():%Y-%m-%d %H:%M}",
        "enriched: false",
        "---",
        "",
        f"# {title}",
        "",
        f"> 📅 {date_str}　⏱ {duration}　🎙 {len(speakers)} 位说话人　·　[在飞书打开]({url})",
        "",
    ]
    if kw:
        lines += [f"**关键词**　{kw}", ""]
    lines += ["---", "", "## 原始逐字稿", "", "> 以下为完整原始转写，未做删改。", ""]
    for spk, ts, text in paras:
        lines += [f"**{spk}**　`{ts}`", "", text, ""]

    return "\n".join(lines), sanitize(f"{day} {title}"), len(paras)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Markdown 输出目录")
    ap.add_argument("--profile", default=None, help=f"浏览器配置目录（默认 {DEFAULT_PROFILE}）")
    ap.add_argument("--headless", action="store_true", help="无头模式（仅在已登录后可用）")
    ap.add_argument("--limit", type=int, default=0, help="最多拉取几条新的（0=不限）")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录的秒数")
    ap.add_argument("--no-asr", action="store_true",
                    help="即使配了 key 也不对无转写的妙记做 ASR 兜底")
    args = ap.parse_args()

    use_asr = (not args.no_asr) and asr.asr_available()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"ok": False, "error": "playwright 未安装，请先运行 setup.sh"},
                         ensure_ascii=False))
        return 1

    out_dir = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out_dir, exist_ok=True)
    profile_dir = os.path.abspath(os.path.expanduser(args.profile or DEFAULT_PROFILE))
    os.makedirs(profile_dir, exist_ok=True)

    state = load_state(out_dir)
    known = set(state.get("minutes", {}).keys()) | scan_existing_tokens(out_dir)
    log(f"已记录 {len(known)} 条历史妙记，输出目录：{out_dir}")

    result = {"ok": False, "total": 0, "new": [], "skipped": 0}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile_dir,
            headless=args.headless,
            args=[f"--proxy-bypass-list={NO_PROXY_HOSTS}"],
            viewport={"width": 1280, "height": 860},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"打开妙记首页失败：{e}")

        if not wait_for_login(page, args.timeout):
            ctx.close()
            print(json.dumps({"ok": False, "error": "等待登录超时，请重跑并在浏览器中完成登录"},
                             ensure_ascii=False))
            return 1
        log("✅ 已检测到登录态")

        csrf = get_csrf(ctx, page)
        if not csrf:
            log("⚠️ 没取到 bv_csrf_token，导出可能会失败")

        result["untranscribed"] = []
        if use_asr:
            log("ℹ️ ASR 兜底已启用：无飞书转写的妙记将用百炼 Paraformer 转写")
        elif not args.no_asr and asr.missing_env():
            log(f"ℹ️ ASR 兜底未启用（缺环境变量：{', '.join(asr.missing_env())}）；"
                f"无转写的妙记会被跳过")

        minutes = fetch_all_minutes(page)
        result["total"] = len(minutes)
        log(f"云端共 {len(minutes)} 条妙记")

        # 保险：登录态判断一旦失灵，接口会安静地返回空列表而不是报错。
        # 与其假装成功，不如明确告警。
        if not minutes:
            result["warning"] = ("未拉到任何妙记。若你确信账号里有妙记，"
                                 "多半是登录态没真正生效——请删掉 ~/.config/feishu-minutes/browser-profile 后重跑并重新登录。")
            log("⚠️ " + result["warning"])

        todo = [m for m in minutes if m.get("object_token") not in known]
        result["skipped"] = len(minutes) - len(todo)
        if args.limit:
            todo = todo[: args.limit]
        log(f"待拉取 {len(todo)} 条（跳过已有 {result['skipped']} 条）")

        for i, m in enumerate(todo, 1):
            token = m.get("object_token")
            title = m.get("topic") or "未命名妙记"
            try:
                raw = export_transcript(page, token, csrf)
                source = "feishu"
                # 飞书没转写 / 只转了开头一小段（免费额度耗尽）时，用覆盖率判断。
                _, _, _, paras = parse_transcript(raw)
                if transcript_incomplete(paras, m.get("duration")):
                    if use_asr:
                        cov = "无" if not paras else f"仅覆盖开头，共 {len(paras)} 段"
                        log(f"  [{i}/{len(todo)}] 🎙 飞书转写残缺（{cov}），改用 Paraformer：{title}")
                        raw, nsent = asr.transcribe_minute(page, ctx, token, api_base(page), m, out_dir)
                        source = "dashscope-paraformer"
                        log(f"      ✅ ASR 完成，{nsent} 句")
                    else:
                        log(f"  [{i}/{len(todo)}] ⏭ 无飞书转写、未启用 ASR，跳过：{title}")
                        result["untranscribed"].append({"token": token, "title": title})
                        continue
                md, base, npara = build_markdown(m, raw)
                if source != "feishu":
                    md = md.replace("enriched: false",
                                    f"enriched: false\ntranscribed_by: {source}", 1)
                path = os.path.join(out_dir, base + ".md")
                n = 2
                while os.path.exists(path):
                    path = os.path.join(out_dir, f"{base} ({n}).md")
                    n += 1
                with open(path, "w", encoding="utf-8") as f:
                    f.write(md)
                state["minutes"][token] = {
                    "title": title,
                    "file": os.path.basename(path),
                    "pulled_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "enriched": False,
                    "source": source,
                }
                result["new"].append({"token": token, "title": title, "path": path,
                                      "paragraphs": npara, "source": source})
                log(f"  [{i}/{len(todo)}] ✅ {title}（{npara} 段）")
                save_state(out_dir, state)
            except Exception as e:
                log(f"  [{i}/{len(todo)}] ❌ {title}：{e}")

        ctx.close()

    save_state(out_dir, state)
    result["ok"] = True
    log(f"完成：新增 {len(result['new'])} 条")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
