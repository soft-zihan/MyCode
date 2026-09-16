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
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
        log "已清理端口 $port 上的进程: $pids"
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
    export MYCODE_OTEL="${MYCODE_OTEL:-1}"
    export MYCODE_OTEL_ENDPOINT="${MYCODE_OTEL_ENDPOINT:-${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}/api/public/otel/v1/traces}"
    export MYCODE_SESSION_BACKEND="${MYCODE_SESSION_BACKEND:-jsonl}"
    export MYCODE_REWIND="${MYCODE_REWIND:-0}"
    nohup env -i PATH="$PATH" HOME="$HOME" MYCODE_OTEL="$MYCODE_OTEL" MYCODE_OTEL_ENDPOINT="$MYCODE_OTEL_ENDPOINT" MYCODE_SESSION_BACKEND="$MYCODE_SESSION_BACKEND" MYCODE_REWIND="$MYCODE_REWIND" LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY" LANGFUSE_BASE_URL="$LANGFUSE_BASE_URL" python frontend/server/main.py > "$PROJECT_ROOT/logs/.backend.log" 2>&1 &
    echo $! > "$PIDFILE_BACKEND"
    log "后端已启动 (pid=$!, log=.backend.log) [OTEL=$MYCODE_OTEL, SESSION=$MYCODE_SESSION_BACKEND, REWIND=$MYCODE_REWIND]"
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
