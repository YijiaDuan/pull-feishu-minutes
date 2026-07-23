# pull-feishu-minutes.skill

> *"The meeting's over. The talk's over. The coffee chat was great. The recording sits in Feishu's cloud, and your local notes are empty."*

**No custom Feishu app. No API scopes. No admin approval. Log in once in a browser window — everything else is automatic.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/macOS%20%7C%20Linux%20%7C%20Windows-black.svg)](https://playwright.dev)
[![Playwright](https://img.shields.io/badge/Playwright-persistent%20context-orange.svg)](https://playwright.dev/python/)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Skill-blueviolet)](https://claude.ai/code)

&nbsp;

Tell Claude "pull my Feishu minutes." It opens a browser for you to log in, then pulls **every** transcript from your Feishu Minutes library to disk — one Markdown file each, with **an AI-written summary on top and the full verbatim transcript at the bottom**. Full sync the first time, incremental after that.

The honest tradeoff: Feishu's public API cannot do this (see below), so this skill drives the Minutes web app's internal endpoints. Zero setup friction is the upside; **Feishu shipping a redesign could break it** is the downside. When that happens the script fails loudly rather than pretending to succeed — that behavior is deliberate.

[Install](#install) · [Usage](#usage) · [What it does](#what-it-does) · [Why not the public API](#why-not-the-public-api) · [Good fit / bad fit](#good-fit--bad-fit) · [中文](README.md)

---

## Install

> Claude Code auto-loads skills from `~/.claude/skills/` (global) or `.claude/skills/` in your git repo root (project-local).

```bash
# Global (available everywhere) — recommended
git clone https://github.com/YijiaDuan/pull-feishu-minutes \
  ~/.claude/skills/pull-feishu-minutes

# Or project-local
mkdir -p .claude/skills
git clone https://github.com/YijiaDuan/pull-feishu-minutes \
  .claude/skills/pull-feishu-minutes
```

On first use Claude runs `scripts/setup.sh` for you — creates a venv, installs Playwright, downloads the Chromium build. Nothing to install by hand.

**Requirements**: Python 3.10+ and network access to Feishu. That's it.

---

## Usage

Just say it in plain language:

```
pull my feishu minutes
```

All of these trigger it too:

- "sync my Feishu minutes to Obsidian"
- "export my Feishu meeting transcripts"
- "download all my 妙记"
- "grab the recordings from my meetings this week"

**First run** opens a browser window (titled *Google Chrome for Testing* — not your everyday Chrome). Click "Sign Up/Log In" and scan the QR code with the Feishu mobile app. The script detects the login and continues on its own. No cookie copying, no DevTools, nothing technical.

**Later runs** don't open a window at all — the session lives in `~/.config/feishu-minutes/browser-profile` and the script runs headless.

---

## What it does

```
   "pull my feishu minutes"
            │
            ▼
   ┌────────────────────┐
   │ browser → you log in│  ← first run only
   └────────┬───────────┘
            ▼
   enumerate every minute (paginated, nothing skipped)
            │
            ▼
   diff against what's on disk ──► skip what you already have
            │
            ▼
   export each transcript (with speakers + timestamps)
            │
            ▼
   Claude reads each one ──► writes summary, tags scene, renames file
            │
            ▼
   Markdown on disk
```

Each note contains:

| Section | Contents |
|---|---|
| **frontmatter** | Rewritten title, `场景` (coffee chat / talk / meeting…), tags, date, duration, speaker count, source link, `minute_token` |
| **What this was** | Written for *you, one month from now, having forgotten everything*: what the event was, who spoke, what happened, one-line memory hook |
| **What stuck** | The genuinely valuable material — concrete numbers, cases, and sharp phrasing from the actual conversation |
| **Ideas worth stealing** | A few things you can act on |
| **Raw transcript** | Preserved verbatim, untouched |

While summarizing, Claude actively **filters out** small talk, tech-difficulty grumbling, gossip, and personal/private material (job changes, health, salary, relationships). Coffee chats are full of that, and you probably don't want it in your knowledge base.

### Optional: transcribe what Feishu didn't

Free Feishu accounts get only **300 minutes** of transcription. Recordings past that cap have no transcript, or only the first few sentences. With a speech-model key configured, those minutes are detected automatically, their audio is pulled and re-transcribed (with speaker diarization and timestamps), and folded into the same Markdown.

- **Off by default** — without a key the core feature is unaffected; untranscribed minutes are skipped and listed in the result.
- Currently wired to **Alibaba Bailian's Paraformer**: ~¥0.2 per hour of audio, results in 1–2 minutes.
- Needs `DASHSCOPE_API_KEY` plus a set of OSS credentials (the audio is relayed through object storage so Bailian can reach it — into your own private bucket, deleted right after). See the "ASR fallback" section in [SKILL.md](SKILL.md).
- Whether a minute "needs re-transcribing" is judged by **coverage**, not by whether there's any text — when the quota runs out Feishu often has already transcribed the first couple of sentences, so a text-presence check would miss it.

---

## Why not the public API

Not laziness — this is the conclusion after going down that road:

| | Feishu public API | This skill |
|---|---|---|
| **Can it list all my minutes?** | ❌ No list endpoint. Only a keyword search that returns a few relevance-ranked hits — **incomplete by design** | ✅ Full paginated enumeration |
| **What does exporting a transcript need?** | The sensitive `minutes:minutes.transcript:export` scope | One login |
| **Custom app required?** | ✅ Yes, plus App ID / Secret | ❌ No |
| **Admin approval required?** | ✅ **A tenant admin must grant it in the admin console** | ❌ No |
| **Can a regular employee do it alone?** | ❌ You're probably not an admin | ✅ Yes |
| **Stability** | Official, stable long-term | Internal endpoints, may break on redesign |

In short: the public API **dead-ends at "list what minutes I have"**, and the export scope sits behind admin approval. For most people that's a wall.

---

## Good fit / bad fit

| ✅ Good fit | ❌ Bad fit |
|---|---|
| Getting your own meetings, talks, and coffee chats into a local knowledge base | Bulk-downloading minutes **other people** shared with you (only your own space is pulled) |
| Obsidian / Logseq / any local Markdown setup | Wanting the original audio or video (text only) |
| Sick of copy-pasting transcripts by hand | 24/7 unattended production sync (sessions expire) |
| Wanting recordings to arrive pre-summarized instead of as walls of text | Minutes in someone else's tenant that you can't view |
| Regular employees without Feishu admin rights | Commercial integrations needing an official SLA |

---

## Pitfalls (all learned the hard way)

- **The browser it opens is fully isolated from your everyday Chrome.** Being logged into Feishu there does nothing here — that isolation is exactly what makes "works for someone who never authorized Feishu" possible.
- **The first run needs a visible window.** Pass `--headless` and there's nothing to log into; you'll just wait for the timeout.
- **When logged out, Feishu's list endpoint still returns "success, empty list"** — indistinguishable from "logged in with no minutes." So this skill checks the page for a login button instead, and **loudly warns** when it pulls zero. It will not silently claim success.
- **The script never reloads the page while waiting for login.** A reload wipes the QR code you're scanning. Learned that one the hard way.
- **After login Feishu redirects you to a tenant-specific domain** (`xxx.feishu.cn`), not `meetings.feishu.cn`. Requests must follow the current origin; cross-origin fetches get blocked by CORS.
- **The export endpoint has a separate CSRF token per domain.** Grab the wrong one and every call returns HTTP 400.
- **Renaming and moving files is safe.** Deduplication keys on `minute_token` in the frontmatter, not the filename.
- **Sessions expire.** Re-run, scan again, and nothing already pulled gets re-fetched.

---

## Project layout

```
pull-feishu-minutes/
├── SKILL.md              # the playbook Claude Code reads
├── README.md             # 中文
├── README_EN.md          # this file
├── LICENSE
└── scripts/
    ├── setup.sh          # venv + Playwright + Chromium
    └── sync_minutes.py   # login / enumerate / export / dedupe
```

**Deliberately absent**: no `prompts/` (the summarization instructions live in SKILL.md, since that *is* the instruction file); no `requirements.txt` (Playwright is the only dependency and `setup.sh` handles it); no committed `.venv` (147MB, generated locally). Sparse on purpose rather than padded with empty directories.

`SKILL.md` also carries a **"Do not break these"** section for maintainers, enumerating every trap that causes silent false success. Read it before touching `sync_minutes.py`.

---

## FAQ

**Does it work on Windows / Linux?**
It should — Playwright is cross-platform and there's no macOS-specific logic. Only tested on macOS though; issue reports welcome.

**Will it pull minutes shared with me?**
No. Your own space only.

**Can it download the audio/video?**
No, transcripts only. Use the Minutes web UI for media files.

**Can I run it on a schedule?**
The script supports `--headless`, so cron / LaunchAgent works technically. Two caveats: sessions expire (the job will fail and log it), and the summarization step needs Claude present — unattended runs leave you bare transcripts.

**Is it safe? Does anything get sent to a third party?**
No. Login happens in a browser on your machine, credentials stay in `~/.config/feishu-minutes/browser-profile`, and the script only talks to `feishu.cn`. Nothing is uploaded anywhere. The code is short and fully open — read it yourself.

**What if Feishu breaks it?**
The script errors out loudly instead of faking success. SKILL.md documents every internal endpoint and gotcha, so patching is tractable. Issues and PRs welcome.

---

## License

[MIT](LICENSE)
