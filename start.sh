#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PIDFILE_BACKEND="$PROJECT_ROOT/logs/.backend.pid"
PIDFILE_FRONTEND="$PROJECT_ROOT/logs/.frontend.pid"

# logs/ 内容全被 .gitignore 按 basename 忽略，git 不跟踪空目录，
# 全新克隆里它并不存在——不先建好，下面重定向与 pidfile 写入会让 set -e 直接中止启动。
mkdir -p "$PROJECT_ROOT/logs"

# setsid 让服务脱离调用方会话：WSL / CI 里那种瞬时 shell 退出时会按会话整树回收
# 进程，nohup 挡不住（它只忽略 SIGHUP）。macOS 不自带 util-linux 的 setsid，
# 缺失时退回纯 nohup——对"在终端里启动后关掉终端"这一常规场景 nohup 已经够用。
if command -v setsid >/dev/null 2>&1; then
    SETSID="setsid"
else
    SETSID=""
fi

BACKEND_PORT=5555
FRONTEND_PORT=8090

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
err()  { echo "[$(date '+%H:%M:%S')] ❌ $*" >&2; }

# ── helpers ──────────────────────────────────────────────────────────────────

kill_port() {
    local port=$1
    local pids
    # 只杀监听者（-sTCP:LISTEN）：曾误杀所有连着该端口的客户端
    # （如持有 SSE 长连接的 eval.smoke.runner → exit 137）
    pids=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
        log "已清理端口 $port 上的监听进程: $pids"
    fi
}

stop_backend() {
    if [[ -f "$PIDFILE_BACKEND" ]]; then
        local pid
        pid=$(cat "$PIDFILE_BACKEND")
        kill "$pid" 2>/dev/null || true
        # 必须等优雅退出：lifespan 的 shutdown 段要 flush Langfuse trace，并给活跃
        # 会话合成 turn/end{shutdown}。下面的 kill_port 是 kill -9，不等就等于每次
        # stop/restart 都丢 trace、并把在跑的会话留成 interrupted（下次启动误判为崩溃）。
        local waited=0
        while kill -0 "$pid" 2>/dev/null && (( waited < 20 )); do
            sleep 0.5
            waited=$((waited + 1))
        done
        rm -f "$PIDFILE_BACKEND"
        if kill -0 "$pid" 2>/dev/null; then
            log "后端未在 10s 内优雅退出，将强制清理 (pid=$pid)"
        else
            log "后端已停止 (pid=$pid)"
        fi
    fi
    kill_port "$BACKEND_PORT"
}

stop_frontend() {
    if [[ -f "$PIDFILE_FRONTEND" ]]; then
        local pid
        pid=$(cat "$PIDFILE_FRONTEND")
        kill "$pid" 2>/dev/null || true
        rm -f "$PIDFILE_FRONTEND"
        log "前端已停止 (pid=$pid)"
    fi
    kill_port "$FRONTEND_PORT"
}

start_backend() {
    log "启动后端 (FastAPI @ port $BACKEND_PORT) ..."
    cd "$PROJECT_ROOT"
    source .venv/bin/activate
    # 加载 .env（Langfuse 密钥等，已 gitignore）
    if [ -f "$PROJECT_ROOT/.env" ]; then
        set -a
        source "$PROJECT_ROOT/.env"
        set +a
    fi
    export MYCODE_TRACING="${MYCODE_TRACING:-1}"
    export LANGFUSE_TRACING_ENABLED="${LANGFUSE_TRACING_ENABLED:-true}"
    export LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-https://cloud.langfuse.com}"
    export MYCODE_SESSION_BACKEND="${MYCODE_SESSION_BACKEND:-jsonl}"
    # PYTHONUNBUFFERED=1：stdout 重定向到日志文件时默认全缓冲，应用的 print 诊断
    # （[STARTUP]/[SHUTDOWN] 等）会滞留缓冲区、丢失或与 stderr 的 uvicorn 日志乱序。
    $SETSID nohup env -i PATH="$PATH" HOME="$HOME" PYTHONUNBUFFERED=1 MYCODE_TRACING="$MYCODE_TRACING" LANGFUSE_TRACING_ENABLED="$LANGFUSE_TRACING_ENABLED" MYCODE_SESSION_BACKEND="$MYCODE_SESSION_BACKEND" LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}" LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}" LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL" LANGFUSE_TRACING_ENVIRONMENT="${LANGFUSE_TRACING_ENVIRONMENT:-}" LANGFUSE_RELEASE="${LANGFUSE_RELEASE:-}" MYCODE_LANGFUSE_PROJECT_ID="${MYCODE_LANGFUSE_PROJECT_ID:-}" MYCODE_HOST="${MYCODE_HOST:-}" MYCODE_AUTH_TOKEN="${MYCODE_AUTH_TOKEN:-}" python frontend/server/main.py > "$PROJECT_ROOT/logs/.backend.log" 2>&1 &
    echo $! > "$PIDFILE_BACKEND"
    log "后端已启动 (pid=$!, log=.backend.log) [TRACING=$MYCODE_TRACING, SESSION=$MYCODE_SESSION_BACKEND]"
}

start_frontend() {
    log "启动前端 (Vite @ port $FRONTEND_PORT) ..."
    cd "$PROJECT_ROOT/frontend"
    $SETSID nohup npm run dev > "$PROJECT_ROOT/logs/.frontend.log" 2>&1 &
    echo $! > "$PIDFILE_FRONTEND"
    log "前端已启动 (pid=$!, log=.frontend.log)"
}

wait_ready() {
    # 60s：后端启动会串行尝试连接 .mcp.json 里的每个 MCP server，配置了不可达
    # server 时冷启动可超过 10s。与 scripts/run_tests.sh 的 wait_health 对齐。
    local port=$1 name=$2 max_wait=${3:-60}
    local elapsed=0
    while ! lsof -ti:"$port" >/dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))
        if (( elapsed >= max_wait )); then
            err "$name 在 ${max_wait}s 内未就绪，请检查日志"
            return 1
        fi
    done
    log "$name 已就绪 ✓  (port $port)"
}

# ── commands ─────────────────────────────────────────────────────────────────

do_start() {
    log "===== 启动服务 ====="
    start_backend
    start_frontend
    wait_ready "$BACKEND_PORT"  "后端"
    wait_ready "$FRONTEND_PORT" "前端"
    echo ""
    log "   全部就绪:"
    log "   前端  → http://localhost:$FRONTEND_PORT"
    log "   后端  → http://localhost:$BACKEND_PORT"
    if [ -n "${MYCODE_AUTH_TOKEN:-}" ]; then
        log "   鉴权  → token 已启用（HTTP Bearer + WS query token）"
    fi
    log "   远程访问（推荐 SSH 隧道，无需鉴权）: ssh -L $BACKEND_PORT:localhost:$BACKEND_PORT -L $FRONTEND_PORT:localhost:$FRONTEND_PORT user@server"
    log "            直接暴露需显式设置: MYCODE_HOST=0.0.0.0 + MYCODE_AUTH_TOKEN=<token>"
    log ""
    log "停止服务: ./start.sh stop"
}

do_stop() {
    log "===== 停止服务 ====="
    stop_frontend
    stop_backend
    log "所有服务已停止"
}

do_restart() {
    log "===== 重启服务 ====="
    stop_frontend
    stop_backend
    sleep 1
    do_start
}

# 生产模式：后端直接托管 frontend/dist，不再跑 Vite dev server。
# dev server 无压缩、带 HMR 开销、且固定绑 0.0.0.0，只适合本地开发。
do_prod() {
    log "===== 生产模式（后端托管构建产物）====="
    if [[ ! -d "$PROJECT_ROOT/frontend/dist" ]]; then
        log "未发现 frontend/dist，先构建前端 ..."
        (cd "$PROJECT_ROOT/frontend" && npm run build)
    fi
    stop_frontend
    stop_backend
    sleep 1
    start_backend
    wait_ready "$BACKEND_PORT" "后端"
    echo ""
    log "   就绪 → http://localhost:$BACKEND_PORT"
    log "   此模式下 :$FRONTEND_PORT 不使用，status 会显示前端未运行，属正常。"
    log "   前端有改动时：(cd frontend && npm run build) 后 ./start.sh prod"
    log ""
    log "停止服务: ./start.sh stop"
}

do_status() {
    echo "===== 服务状态 ====="
    if lsof -ti:"$BACKEND_PORT" >/dev/null 2>&1; then
        log "后端  ✓ 运行中 (port $BACKEND_PORT)"
    else
        err "后端  ✗ 未运行"
    fi
    if lsof -ti:"$FRONTEND_PORT" >/dev/null 2>&1; then
        log "前端  ✓ 运行中 (port $FRONTEND_PORT)"
    else
        err "前端  ✗ 未运行"
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────

case "${1:-start}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    prod)    do_prod    ;;
    status)  do_status  ;;
    *)
        echo "用法: $0 {start|stop|restart|prod|status}"
        echo ""
        echo "  start    启动前后端服务（默认，可省略；前端为 Vite dev server）"
        echo "  prod     生产模式：构建前端并由后端托管 dist，不跑 dev server"
        echo "  stop     停止所有服务"
        echo "  restart  重启所有服务"
        echo "  status   查看服务状态"
        exit 1
        ;;
esac
