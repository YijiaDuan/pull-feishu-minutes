#!/usr/bin/env bash
# 一次性环境准备：建 venv、装 playwright、下 chromium 内核。
# 幂等，重复运行安全。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$SKILL_DIR/.venv"

# 找一个可用的 python3（playwright 目前对 3.9~3.13 支持最稳）
PY=""
for c in python3.12 python3.11 python3.13 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到 python3，请先安装 Python 3.10+" >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "→ 创建虚拟环境（$PY）..."
  "$PY" -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c "import playwright" >/dev/null 2>&1; then
  echo "→ 安装 playwright..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q playwright
fi

# oss2 仅 ASR 兜底用（把无转写妙记的音频传到 OSS 中转），装上不占多少空间
if ! "$VENV/bin/python" -c "import oss2" >/dev/null 2>&1; then
  echo "→ 安装 oss2（ASR 兜底可选依赖）..."
  "$VENV/bin/pip" install -q oss2
fi

echo "→ 检查 chromium 内核..."
"$VENV/bin/python" -m playwright install chromium

echo "✅ 环境就绪：$VENV/bin/python"
