#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PIDFILE_BACKEND="$PROJECT_ROOT/logs/.backend.pid"
PIDFILE_FRONTEND="$PROJECT_ROOT/logs/.frontend.pid"

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
        rm -f "$PIDFILE_BACKEND"
        log "后端已停止 (pid=$pid)"
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
    nohup env -i PATH="$PATH" HOME="$HOME" MYCODE_TRACING="$MYCODE_TRACING" LANGFUSE_TRACING_ENABLED="$LANGFUSE_TRACING_ENABLED" MYCODE_SESSION_BACKEND="$MYCODE_SESSION_BACKEND" LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}" LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}" LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL" LANGFUSE_TRACING_ENVIRONMENT="${LANGFUSE_TRACING_ENVIRONMENT:-}" LANGFUSE_RELEASE="${LANGFUSE_RELEASE:-}" MYCODE_LANGFUSE_PROJECT_ID="${MYCODE_LANGFUSE_PROJECT_ID:-}" python frontend/server/main.py > "$PROJECT_ROOT/logs/.backend.log" 2>&1 &
    echo $! > "$PIDFILE_BACKEND"
    log "后端已启动 (pid=$!, log=.backend.log) [TRACING=$MYCODE_TRACING, SESSION=$MYCODE_SESSION_BACKEND]"
}

start_frontend() {
    log "启动前端 (Vite @ port $FRONTEND_PORT) ..."
    cd "$PROJECT_ROOT/frontend"
    nohup npm run dev > "$PROJECT_ROOT/logs/.frontend.log" 2>&1 &
    echo $! > "$PIDFILE_FRONTEND"
    log "前端已启动 (pid=$!, log=.frontend.log)"
}

wait_ready() {
    local port=$1 name=$2 max_wait=${3:-10}
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

case "${1:-}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        echo ""
        echo "  start    启动前后端服务"
        echo "  stop     停止所有服务"
        echo "  restart  重启所有服务"
        echo "  status   查看服务状态"
        exit 1
        ;;
esac
