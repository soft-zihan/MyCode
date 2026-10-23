#!/usr/bin/env bash
# 钉住 WSL 发行版，让集群与前后端服务活过「本次命令返回」。
#
# 根因（deploy/README.md §8.3，B1 最贵的一个坑）：WSL2 会在发行版内**没有前台进程**
# 时把它整个关掉。从 Windows 侧 `wsl.exe -e bash -lc '...'` 调进来时，命令启动 →
# 发行版被唤醒 → systemd 拉起 docker → kind 节点起来 → 命令退出 → **发行版被拆** →
# dockerd 死 → 节点容器全停。于是「`mycode start` 起了服务却没反应」、
# 「集群刚才还在，现在 kubectl 连不上」——不是服务坏了，是地基每几十秒被抽走一次。
#
# 修法就是留一个常驻进程。幂等：用 pidfile 去重，否则每调一次就多钉一根。
# 不在 WSL 里时是 no-op，所以调用方不需要判断环境。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/logs/.wsl-pin.pid"

# WSL_DISTRO_NAME 由 WSL 自己注入，是「我在 WSL 里」最可靠的判据
[ -n "${WSL_DISTRO_NAME:-}" ] || exit 0

alive() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

alive && exit 0

mkdir -p "$PROJECT_ROOT/logs"
# 里层 bash 先写自己的 pid 再 exec，pidfile 里存的才是真正常驻的那个进程。
# 直接 `setsid nohup sleep infinity & echo $!` 记到的是 setsid 的 pid——它 fork 完就退，
# 下次 alive 判定失败，于是每跑一次多钉一根。
setsid nohup bash -c 'echo $$ > "$1"; exec sleep infinity' _ "$PIDFILE" \
  >/dev/null 2>&1 </dev/null &

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if alive; then
    echo "[pin] WSL 发行版已钉住（pid $(cat "$PIDFILE")）"
    exit 0
  fi
  sleep 0.2
done

# 钉不住不该让整个启动失败：服务还是起得来，只是活不过本次命令。把话说清楚就够了。
echo "[pin] ⚠️ 没能钉住发行版，服务可能在这次命令返回后被 WSL 拆掉" >&2
exit 0
