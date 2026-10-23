#!/usr/bin/env bash
# 构建控制面镜像并做冒烟断言。这是 Dockerfile 那一层的「测试」——
# Dockerfile 本身没法单测，所以把断言做成可重复执行的脚本。
#
# 输出同时落盘到 logs/.container-smoke.log：wsl.exe 的 stdout 直接回传是
# UTF-16 乱码，必须落盘再读。
set -euo pipefail

# 清空代理：下面的 curl 全打在 127.0.0.1 上。宿主 HTTP_PROXY 指向 Windows 侧的
# 127.0.0.1:7897 时，curl 会把请求交给代理，代理再从 Windows 侧绕回来——
# 靠 WSL2 的 localhostForwarding 碰巧能通，但那是运气不是设计，而且失败时代理会回
# 一个带 `Proxy-Connection` 头的假 502，把排查带偏（详见 verify/deploy-probe.sh 顶部）。
# docker build/pull 不受影响：那些走 dockerd 自己的代理配置，不看 shell 环境。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p logs

IMAGE="${IMAGE:-mycode/control-plane:b1}"
PORT="${PORT:-15555}"
CID_FILE="logs/.container-smoke.cid"
LOG="logs/.container-smoke.log"

cleanup() {
  if [ -f "$CID_FILE" ]; then
    docker rm -f "$(cat "$CID_FILE")" >/dev/null 2>&1 || true
    rm -f "$CID_FILE"
  fi
}
trap cleanup EXIT

fail() { echo "FAIL: $1" | tee -a "$LOG"; exit 1; }
pass() { echo "PASS: $1" | tee -a "$LOG"; }

for b in docker curl; do
  command -v "$b" >/dev/null || { echo "缺 $b"; exit 1; }
done

: > "$LOG"

echo "=== 构建 $IMAGE ===" | tee -a "$LOG"
T0=$(date +%s)
docker build -t "$IMAGE" . 2>&1 | tail -25 | tee -a "$LOG"
BUILD_SECONDS=$(( $(date +%s) - T0 ))
echo "BUILD_SECONDS=$BUILD_SECONDS" | tee -a "$LOG"

SIZE=$(docker image inspect "$IMAGE" --format '{{.Size}}')
echo "IMAGE_SIZE_MB=$(( SIZE / 1024 / 1024 ))" | tee -a "$LOG"

echo "=== 起容器（宿主端口 $PORT）===" | tee -a "$LOG"
# 刻意不挂任何 config.json：load_config() 读不到文件时返回空配置，服务仍应起来。
# 这条同时验证了「镜像里没有 ~/.my-code」不会导致启动崩溃。
docker run -d --cidfile "$CID_FILE" -p "$PORT:5555" \
  -e MYCODE_HOST=0.0.0.0 -e MYCODE_TRACING=0 "$IMAGE" >/dev/null
CID=$(cat "$CID_FILE")

echo "=== 等就绪（最多 90s）===" | tee -a "$LOG"
READY_AFTER=""
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    READY_AFTER=$i; break
  fi
  # 容器已经死了就别傻等 90 秒
  if ! docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null | grep -q true; then
    docker logs "$CID" 2>&1 | tail -40 | tee -a "$LOG"
    fail "容器启动后退出"
  fi
  sleep 1
done
[ -n "$READY_AFTER" ] || { docker logs "$CID" 2>&1 | tail -40 | tee -a "$LOG"; fail "90s 内未就绪"; }
echo "READY_AFTER=${READY_AFTER}s" | tee -a "$LOG"

echo "=== 断言 ===" | tee -a "$LOG"

# 1. 健康端点返回约定形状（frontend/server/main.py 的 /api/health）
[ "$(curl -fsS "http://127.0.0.1:$PORT/api/health")" = '{"status":"ok"}' ] \
  || fail "/api/health 形状不对"
pass '/api/health == {"status":"ok"}'

# 2. 前端由后端托管：首页 200 且是 HTML（说明 dist 进了镜像、catch-all 生效）
CODE=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/")
[ "$CODE" = "200" ] || fail "首页 HTTP $CODE"
curl -sS -D- -o /dev/null "http://127.0.0.1:$PORT/" | grep -qi "content-type: text/html" \
  || fail "首页 content-type 不是 text/html"
pass "SPA 首页由后端托管"

# 3. /api 下未知路径必须是 JSON 404，不能吐 HTML（main.py 的 spa_fallback 约定）
BODY=$(curl -sS "http://127.0.0.1:$PORT/api/definitely-not-a-route")
echo "$BODY" | grep -q "detail" || fail "/api 404 不是 JSON：$BODY"
pass "/api 未知路径返回 JSON 404"

# 4. 容器内是非 root，且 uid 与 manifests 里要对齐的 10001 一致
[ "$(docker exec "$CID" id -u)" = "10001" ] || fail "运行用户不是 10001"
pass "runAsUser=10001"

# 5. eval/ 确实被裁掉，且服务没因此崩（main.py 里那段导入是 non-fatal try）
docker exec "$CID" test ! -e /app/eval || fail "eval/ 不该在镜像里"
docker logs "$CID" 2>&1 | grep -q "Eval tailer init failed" \
  || fail "没看到预期的 eval non-fatal 日志——可能 COPY 多了，也可能日志被吞"
pass "eval/ 已裁掉且是非致命的"

# 6. 会话目录可写（B1 本地 docker run 用镜像内目录，K8s 里换成 PVC）
docker exec "$CID" sh -c 'touch /home/app/.mycode/sessions/.w && rm /home/app/.mycode/sessions/.w' \
  || fail "/home/app/.mycode/sessions 不可写"
pass "会话目录可写"

# 7. HOME 下那几个散落的写路径都得能建（只读根 + 逐个挂卷会漏，见 deployment.yaml）
docker exec "$CID" sh -c 'mkdir -p /home/app/.agents/skills /home/app/.my-code && touch /home/app/.probe' \
  || fail "HOME 下有不可写位置"
pass "HOME 下 .agents / .my-code / 顶层均可写"

# 8. git 在镜像里，且工作区根可写——「把项目推到云端」靠 Pod 自己 clone
#    （POST /api/workspace/import），分支切换与 worktree 也都 shell out 到 git。
#    python:3.12-slim 不带 git，而本地开发机上早就有了，所以这个缺失只在云端暴露
#    （2026-10-24 实测：本地全绿，云端调 import 直接 FileNotFoundError）。
docker exec "$CID" git --version >/dev/null 2>&1 || fail "镜像里没有 git，云端导入/分支切换会死"
docker exec "$CID" sh -c 'mkdir -p /home/app/.mycode/workspaces/.probe && rmdir /home/app/.mycode/workspaces/.probe'   || fail "/home/app/.mycode/workspaces 不可写"
pass "git 可用，云端工作区根可写"

echo "=== 冒烟全部通过 ===" | tee -a "$LOG"
echo "台账：IMAGE_SIZE_MB / BUILD_SECONDS / READY_AFTER 见 $LOG"
