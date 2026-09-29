#!/usr/bin/env bash
# 一键准备环境：前置检查 → 建 venv → 装 Python 依赖 → 装前端依赖 → 生成 .env。
#
# 用法:
#   ./install.sh          只装运行所需
#   ./install.sh --dev    追加 pytest / requests（跑测试与评测需要）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

WITH_DEV=0
if [[ "${1:-}" == "--dev" ]]; then
    WITH_DEV=1
elif [[ -n "${1:-}" ]]; then
    echo "用法: $0 [--dev]" >&2
    exit 1
fi

log()  { echo "[install] $*"; }
fail() { echo "[install] ❌ $*" >&2; exit 1; }

# ── 前置依赖检查 ────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || fail "缺少 python3（需 3.11+）"
command -v node    >/dev/null 2>&1 || fail "缺少 node（需 18+）"
command -v npm     >/dev/null 2>&1 || fail "缺少 npm"
command -v git     >/dev/null 2>&1 || fail "缺少 git（快照 / worktree 机制依赖）"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') 过低，需 3.11+"

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if (( NODE_MAJOR < 18 )); then
    fail "Node $(node -v) 过低，需 18+"
fi

# lsof 缺失不致命，但 start.sh 的端口探活/清理会退化，明确告知而非静默
if ! command -v lsof >/dev/null 2>&1; then
    log "⚠️  未找到 lsof：start.sh 的端口探活与清理将失效（精简 Linux 镜像常缺，建议安装）"
fi

log "Python $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') / Node $(node -v) ✓"

# ── Python 环境 ─────────────────────────────────────────────────────────────
if [[ ! -x .venv/bin/python ]]; then
    log "创建 .venv ..."
    python3 -m venv .venv
fi

REQ="requirements.txt"
if (( WITH_DEV )); then
    REQ="requirements-dev.txt"
fi
log "安装 Python 依赖（$REQ）..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r "$REQ"
log "Python 依赖完成"

# ── 前端依赖 ────────────────────────────────────────────────────────────────
log "安装前端依赖（frontend/）..."
(cd frontend && npm install --no-audit --no-fund)
log "前端依赖完成"

# ── 运行时目录与本地配置 ────────────────────────────────────────────────────
mkdir -p logs

if [[ ! -f .env && -f .env.example ]]; then
    cp .env.example .env
    log "已从 .env.example 生成 .env（按需填写 Langfuse 密钥等；该文件已 gitignore）"
fi

# ── 下一步 ──────────────────────────────────────────────────────────────────
echo ""
log "✅ 环境就绪。下一步："
log "   1) 启动服务          ./start.sh"
log "   2) 浏览器打开        http://localhost:8090"
log "   3) 配置模型端点      Web UI 的 Agents 设置页，或编辑 ~/.my-code/config.json"
if (( WITH_DEV == 0 )); then
    log ""
    log "   跑测试 / 评测需追加依赖：./install.sh --dev"
fi
