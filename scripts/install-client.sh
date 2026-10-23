#!/usr/bin/env bash
# 安装「端侧」安装包：建 venv → 装依赖 → 把 mycode 链进 PATH。
#
# 装完之后任意目录敲 mycode 就能用：CLI 是 `mycode [options] [prompt]`，
# Web UI 是 `mycode prod`（后端托管包里自带的 frontend/dist，不需要 node）。
#
# 用法（在解压出来的包目录里，或在源码仓库里）：
#   ./scripts/install-client.sh
#   INSTALL_DIR=~/.local/bin PYTHON=python3.12 ./scripts/install-client.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINK_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
PY="${PYTHON:-python3}"

[ -f "$ROOT/mycode" ] || { echo "❌ $ROOT 不像是 MyCode 的安装包（没有 mycode 启动器）" >&2; exit 1; }

command -v "$PY" >/dev/null 2>&1 || { echo "❌ 找不到 $PY。用 PYTHON=/path/to/python 指定一个 ≥3.11 的解释器。" >&2; exit 1; }
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "❌ 需要 Python ≥ 3.11，$PY 是 $("$PY" -V 2>&1)。用 PYTHON=python3.12 $0 指定。" >&2
  exit 1
fi

VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "→ 建虚拟环境 $VENV"
  if ! "$PY" -m venv "$VENV"; then
    echo "❌ 建 venv 失败。Debian/Ubuntu 上要装解释器同版本的 venv 包，例如：" >&2
    echo "     sudo apt install $("$PY" -c 'import sys;print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")')" >&2
    exit 1
  fi
fi

echo "→ 装依赖（requirements.txt）"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"

chmod +x "$ROOT/mycode" "$ROOT/start.sh" "$ROOT"/scripts/*.sh 2>/dev/null || true

mkdir -p "$LINK_DIR"
ln -sf "$ROOT/mycode" "$LINK_DIR/mycode"

echo ""
echo "✓ 已安装：$LINK_DIR/mycode -> $ROOT/mycode"
case ":$PATH:" in
  *":$LINK_DIR:"*) ;;
  *)
    echo "⚠ $LINK_DIR 不在 PATH 里，加一行到 ~/.bashrc 或 ~/.zshrc："
    echo "    export PATH=\"$LINK_DIR:\$PATH\""
    ;;
esac
echo ""
echo "下一步："
echo "  mycode --version       确认装的是哪一版（端与云必须同版本）"
echo "  mycode --help          看 CLI 全部选项（one-shot / REPL）"
echo "  mycode prod            起 Web UI → http://localhost:5555"
echo "  mycode                 在当前目录开交互式会话"
