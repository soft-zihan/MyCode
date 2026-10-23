#!/usr/bin/env bash
# 在 kind 网桥的网关地址上开一个 TCP 转发口，把集群内 Pod 的流量转给宿主代理。
#
# 为什么需要它（2026-10-23 实测，四条对照）：
#   · Pod → github.com:443            超时（DNS 能解析出 20.205.243.166，TCP 连不上）
#   · Pod → api.github.com            200 / 0.6s      ← 同一时刻是通的，所以不是"网络全断"
#   · kind 节点(host netns) → github.com:443   超时
#   · WSL 宿主 + 代理 → github.com    200；git ls-remote 也成功
# 也就是说 github.com 只能经宿主代理出去，而**宿主代理绑在 127.0.0.1**，Pod 够不着
# （Pod → 192.168.64.1:7897 直接 connection refused）。这个脚本就是补上那一段：
# 在网桥网关上监听，转到宿主的 127.0.0.1:7897。
#
# 开了之后实测：Pod -x http://<网关>:7899 → github.com 200 / 1.8s，
# git 的 smart-http 端点 `.../info/refs?service=git-upload-pack` 也回 200。
# ArgoCD 的 repo-server 要在 Pod 里 clone 仓库，靠的就是这个口。
#
# 用法：
#   ./scripts/host-proxy-relay.sh start     # 起转发（幂等）
#   ./scripts/host-proxy-relay.sh url       # 打印 Pod 里该用的代理地址
#   ./scripts/host-proxy-relay.sh stop
#
# ⚠️ 这个转发进程活在被 WSL 拆发行版的那条规则下（见 deploy/README.md §8.3）：
#    发行版被拆它就没了。开工前先钉住发行版，或者重新 start 一次。
set -euo pipefail

LISTEN_PORT="${LISTEN_PORT:-7899}"
UPSTREAM_HOST="${UPSTREAM_HOST:-127.0.0.1}"
UPSTREAM_PORT="${UPSTREAM_PORT:-7897}"
CLUSTER="${CLUSTER:-mycode}"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p logs
PIDFILE="logs/.proxy-relay.pid"

gateway() {
  # kind 网络是双栈的，而 `IPAM.Config` 里 **IPv6 那条排在前面**，
  # 所以 `{{(index .IPAM.Config 0).Gateway}}` 会给出 `fc00:f853:ccd:e793::1`，
  # 拼出来是 `http://fc00:f853:ccd:e793::1:7899` —— 语法上像个 URL，实际连不上
  # （既没加方括号，Pod 也没有 IPv6 路由）。这里列出全部网关、取第一个含点号的，
  # 也就是 IPv4 的那个（2026-10-23 实测踩到）。
  docker network inspect kind --format '{{range .IPAM.Config}}{{.Gateway}}{{"\n"}}{{end}}' \
    | grep -m1 '\.'
}

listening() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

# 只看 PID 文件不够：端口可能被**不是这个脚本起的**进程占着（例如手工敲过一条
# python 转发命令）。那时 bind 会失败，而失败信息如果又被上层 `>/dev/null` 吞掉，
# 调用方只会看到一个没有理由的 rc=1（2026-10-23 实测就是这么白找了一轮）。
port_busy() {
  python3 - "$LISTEN_PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

case "${1:-}" in
  start)
    if listening; then
      echo "[relay] 已在跑（pid $(cat "$PIDFILE")），跳过"
    elif port_busy; then
      echo "[relay] 端口 $LISTEN_PORT 已被别的进程占用，且不是本脚本起的（没有 $PIDFILE）"
      echo "[relay] 先找出它：ps -eo pid,args | grep -F '$LISTEN_PORT'  然后 kill <pid>"
      exit 1
    else
      GW=$(gateway)
      [ -n "$GW" ] || { echo "[relay] 取不到 kind 网桥网关，集群起了吗？"; exit 1; }
      # 转发程序放在独立文件里，不用 heredoc 内联。内联那版写成
      # `python3 - args <<'PY' ... PY` 又跟了 `</dev/null`，后者把前者喂程序的
      # stdin 覆盖掉了，python 读到空程序立刻退出（2026-10-23 实测，报错只有一句
      # "起不来"）。日志落到 logs/.proxy-relay.log，失败时能直接看到 traceback。
      setsid nohup python3 scripts/host_proxy_relay.py \
        "$LISTEN_PORT" "$UPSTREAM_HOST" "$UPSTREAM_PORT" \
        >logs/.proxy-relay.log 2>&1 </dev/null &
      echo $! > "$PIDFILE"
      sleep 2
      listening || { echo "[relay] 起不来，日志："; cat logs/.proxy-relay.log; exit 1; }
      port_busy || { echo "[relay] 进程活着但端口没监听，日志："; cat logs/.proxy-relay.log; exit 1; }
      echo "[relay] 已监听 0.0.0.0:$LISTEN_PORT → $UPSTREAM_HOST:$UPSTREAM_PORT（pid $(cat "$PIDFILE")）"
    fi
    ;;
  url)
    GW=$(gateway)
    [ -n "$GW" ] || { echo "[relay] 取不到 kind 网桥网关"; exit 1; }
    echo "http://${GW}:${LISTEN_PORT}"
    ;;
  stop)
    if listening; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo "[relay] 已停"
    else
      echo "[relay] 本来就没在跑"
      rm -f "$PIDFILE"
    fi
    ;;
  *)
    echo "用法: $0 {start|url|stop}"; exit 2
    ;;
esac
