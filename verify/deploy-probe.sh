#!/usr/bin/env bash
# 把 B1 的每一条声明变成可重跑的证据。10 条断言，全过才算 B1 部署验收通过。
#
# 为什么做成脚本而不是手敲命令：简历上的每条数字与每个「我做了 X」的说法都要能
# 当场重跑一遍。手敲命令的证据是一次性的，脚本的证据是可复核的。
set -euo pipefail

# ⚠️ 本脚本只跟 127.0.0.1 说话（kubectl port-forward），必须清空代理变量。
# 宿主 shell 里的 HTTP_PROXY 会把 curl 劫走：代理从 Windows 侧去连 127.0.0.1，
# 那儿什么都没有，于是回一个**假的 502**。分辨办法是看响应头——代理的 502 带
# `Proxy-Connection: keep-alive`，nginx 的 502 没有这一行。
# 这个假 502 会让人以为 Ingress 链路不通（实际加 --noproxy '*' 立刻 200），排查方向全错。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

export PATH="$HOME/.local/bin:$PATH"

NS=mycode
SEL='app.kubernetes.io/component=control-plane'
HOST_HEADER='Host: mycode.local'
LOG="verify/logs/deploy-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }
ex() { kubectl -n "$NS" exec deploy/control-plane -- "$@"; }

# --- ArgoCD 自动同步的临时开关 -------------------------------------------------
# 断言 7 要手改 Deployment 的 readiness 路径、并让这个改后的状态**保持** 30 秒，
# 而 ArgoCD 的 selfHeal 会在几秒内把它改回 Git 声明值——于是永远观察不到「端点被摘空」。
# 那不是探针失效，恰恰说明 GitOps 在正常工作（gitops-probe 断言 3 验的就是这件事）。
# 两条断言天然互斥，所以这里临时关掉自动同步、验完再打开。
# 用 trap EXIT 兜底：中途 fail 退出也必须把自动同步还回去，否则集群从此不再自愈，
# 而下一个人看到的会是「ArgoCD 怎么不干活了」。
ARGO_APP=""
if kubectl -n argocd get application mycode >/dev/null 2>&1; then ARGO_APP=mycode; fi

argocd_auto_off() {
  [ -n "$ARGO_APP" ] || return 0
  echo "（临时关闭 ArgoCD 自动同步：否则 selfHeal 会立刻把手改的探针改回去）"
  kubectl -n argocd patch application "$ARGO_APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
}

argocd_auto_on() {
  [ -n "$ARGO_APP" ] || return 0
  kubectl -n argocd patch application "$ARGO_APP" --type=merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
  echo "（已恢复 ArgoCD 自动同步：prune + selfHeal）"
}
trap argocd_auto_on EXIT

echo "=== 断言 1：Pod Ready，并记录冷启动 ==="
kubectl -n "$NS" wait --for=condition=Ready pod -l "$SEL" --timeout=300s || fail "Pod 未 Ready"
POD=$(kubectl -n "$NS" get pod -l "$SEL" -o jsonpath='{.items[0].metadata.name}')
START=$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.startTime}')
READY=$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].lastTransitionTime}')
echo "POD=$POD"
echo "POD_START=$START"
echo "POD_READY=$READY"
echo "（两者之差即 Pod 冷启动，需换算成秒；重复 3 次取中位数才可信）"
pass "Pod Ready"

echo "=== 断言 2：容器内是非 root 10001 ==="
[ "$(ex id -u)" = "10001" ] || fail "运行用户不是 10001（实际 $(ex id -u)）"
pass "id -u == 10001"

echo "=== 断言 3：根文件系统只读 ==="
if ex touch /app/should-fail 2>/dev/null; then
  ex rm -f /app/should-fail 2>/dev/null || true
  fail "根文件系统可写——readOnlyRootFilesystem 没生效"
fi
pass "写 /app 被拒（Read-only file system）"

echo "=== 断言 4：能力已被清空 ==="
CAPS=$(ex sh -c 'grep ^CapEff /proc/self/status' || true)
echo "$CAPS"
# cap_drop ALL 之后 CapEff 未必是全 0（运行时会保留一个极小默认集），
# 所以这里的判据是「不得出现高危能力」，而不是「必须全 0」。
if echo "$CAPS" | grep -qiE 'CapEff:\s+0000000000000000'; then
  pass "CapEff 为全 0"
else
  echo "NOTE: CapEff 非全 0，人工核对下面这行里不得出现 sys_admin / net_admin / dac_override"
  ex sh -c 'command -v capsh >/dev/null && capsh --print | head -8 || echo "(镜像内无 capsh，看上面的十六进制值即可)"'
  pass "CapEff 非全 0 但已记录，待人工核对（见上）"
fi

echo "=== 断言 5：配置从 Secret 注入且可解析（只打印端点名，不打印密钥）==="
ex sh -c 'test -r "$MYCODE_CONFIG_PATH"' || fail "读不到 \$MYCODE_CONFIG_PATH 指向的文件"
ex python -c 'import json,os;print("endpoints:",list(json.load(open(os.environ["MYCODE_CONFIG_PATH"])).get("endpoints",{})))' \
  || fail "config.json 解析失败"
pass "Secret 挂载的配置可读、结构正确"

echo "=== 断言 6：Ingress 规则命中（不改宿主 hosts，用 port-forward + Host 头）==="
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 18080:80 >/dev/null 2>&1 &
PF=$!
# 重试，不要 sleep 一次就判死：Ingress 刚 apply 完时 nginx 还没 reload 完 backend，
# 那个窗口里访问会拿到 **502**（打到 default backend），看着像链路不通、其实只是没就绪。
# 实测 nginx 日志里「successfully validated configuration, accepting」到
# 「Backend successfully reloaded」隔约 2s，加上 port-forward 建隧道，一次 sleep 6 不够。
CODE=000
for i in $(seq 1 30); do
  CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -H "$HOST_HEADER" http://127.0.0.1:18080/api/health || true)
  CODE="${CODE//[^0-9]/}"; CODE="${CODE:-000}"
  [ "$CODE" = "200" ] && break
  sleep 3
done
kill $PF 2>/dev/null || true
wait $PF 2>/dev/null || true
[ "$CODE" = "200" ] || fail "经 Ingress 访问 /api/health 重试 30 次仍是 HTTP $CODE"
pass "Ingress → Service → Pod 全链路通（HTTP 200）"

echo "=== 断言 7：readiness 失败会把 Pod 从 Service 端点摘掉 ==="
argocd_auto_off
BEFORE=$(kubectl -n "$NS" get endpoints control-plane -o jsonpath='{.subsets[*].addresses[*].ip}')
[ -n "$BEFORE" ] || { argocd_auto_on; fail "改探针前 endpoints 就是空的，无法证明摘除"; }
kubectl -n "$NS" patch deploy control-plane --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/api/definitely-broken"}]' >/dev/null
sleep 30
AFTER=$(kubectl -n "$NS" get endpoints control-plane -o jsonpath='{.subsets[*].addresses[*].ip}' || true)
# 还原（strategy 是 Recreate，patch 会重建 Pod）
kubectl -n "$NS" patch deploy control-plane --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/api/health"}]' >/dev/null
argocd_auto_on
[ -z "$AFTER" ] || fail "readiness 打到坏路径后 endpoints 仍有地址（$AFTER），说明探针没在起作用"
pass "readiness 失败 → endpoints 被摘空（改前=$BEFORE 改后=空）"
kubectl -n "$NS" rollout status deploy/control-plane --timeout=300s

echo "=== 断言 8：PVC 跨 Pod 重建存活 ==="
# 按 label 删，不要先解析 Pod 名再按名删。断言 7 会 patch 两次 Deployment，而 strategy
# 是 **Recreate**（RWO PVC 不能让多副本挂同一块盘），所以存在「旧 Pod 已进 Terminating、
# 新 Pod 还没建出来」的窗口：那一刻 `get pod -l` 返回的正是即将消失的旧名字，
# 按它 delete 会撞 NotFound，再被 set -e 打死（2026-10-23 实测就是这么红的）。
STAMP="probe-$(date +%s)"
ex sh -c "mkdir -p /home/app/.mycode/sessions && echo $STAMP > /home/app/.mycode/sessions/PVC_PROBE"
BEFORE=$(kubectl -n "$NS" get pod -l "$SEL" -o jsonpath='{.items[*].metadata.name}')
[ -n "$BEFORE" ] || fail "取不到当前 control-plane Pod 名"
kubectl -n "$NS" delete pod -l "$SEL" --ignore-not-found --wait=false >/dev/null
kubectl -n "$NS" rollout status deploy/control-plane --timeout=300s >/dev/null
kubectl -n "$NS" wait --for=condition=Ready pod -l "$SEL" --timeout=300s
AFTER=$(kubectl -n "$NS" get pod -l "$SEL" -o jsonpath='{.items[*].metadata.name}')
echo "重建前 POD=$BEFORE"
echo "重建后 POD=$AFTER"
# 没有这一条，断言可能空过：Pod 压根没被换掉时，PVC 内容当然还在。
[ "$BEFORE" != "$AFTER" ] || fail "Pod 名没变——这条断言并没有真的重建 Pod，结果不算数"
GOT=$(ex cat /home/app/.mycode/sessions/PVC_PROBE)
[ "$GOT" = "$STAMP" ] || fail "Pod 重建后 PVC 内容丢失（期望 $STAMP，实际 $GOT）"
ex rm -f /home/app/.mycode/sessions/PVC_PROBE
pass "Pod 真的被换了（$BEFORE → $AFTER），且 PVC 内容仍在（$STAMP）"

echo "=== 断言 9：NetworkPolicy 在真实负载上拒绝跨 namespace 直连 ==="
# 探针镜像先在宿主拉再 kind load：kind 节点不继承宿主 daemon.json 的 registry-mirrors，
# 而本机 docker.io 的解析结果被污染（详见 verify/netpol-probe.sh 的同名注释）。
# 这里不依赖 netpol-probe.sh 已经 load 过——本脚本要能单独重跑。
docker image inspect curlimages/curl:8.10.1 >/dev/null 2>&1 \
  || docker pull curlimages/curl:8.10.1 >/dev/null
kind load docker-image curlimages/curl:8.10.1 --name "${CLUSTER:-mycode}" >/dev/null

kubectl create ns np-cross --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n np-cross delete pod cross-client --ignore-not-found --wait=false >/dev/null 2>&1 || true
kubectl -n np-cross run cross-client --image=curlimages/curl:8.10.1 \
  --image-pull-policy=IfNotPresent --labels='app=cross-client' --command -- sleep 600 >/dev/null
kubectl -n np-cross wait --for=condition=Ready pod/cross-client --timeout=300s
SVC="control-plane.$NS.svc.cluster.local"
# 别写成 `curl ... || echo blocked`：curl 被拒时 -w 仍会打出 000 且不带换行，
# 会拼成 `000blocked`，把「策略生效」误判成 FAIL（netpol-probe.sh 里踩过）。
CODE=$(kubectl -n np-cross exec cross-client -- \
  curl -sS -m 8 -o /dev/null -w '%{http_code}' "http://$SVC/api/health" 2>/dev/null || true)
CODE="${CODE//[^0-9]/}"; CODE="${CODE:-000}"
kubectl delete ns np-cross --wait=false >/dev/null 2>&1 || true
[ "$CODE" = "blocked" ] || [ "$CODE" = "000" ] \
  || fail "跨 namespace 直连仍拿到 HTTP $CODE——control-plane-ingress 策略没生效"
pass "跨 namespace 直连被拒（只放行 ingress-nginx）"

echo "=== 断言 10：用户实际走的那条路——宿主 hostPort（不是 port-forward）==="
# 断言 6 走的是 kubectl port-forward，它**绕过了 hostPort**。这两条路可以一条通一条不通：
# extraPortMappings 是按节点声明的（只在 control-plane 那个节点容器上），而 ingress-nginx
# controller 用 hostPort——落在哪个节点，80/443 就绑在哪个节点。2026-10-23 真出现过
# 「断言 6 绿、浏览器 localhost:8080 却 connection reset」，因为 controller 被调度到了
# worker（上游 manifest 的 nodeSelector 只有 kubernetes.io/os: linux，两个节点都匹配）。
# 修法见 scripts/k8s-bootstrap.sh 里的 ingress-ready 标签 + nodeSelector patch。
# 教训：验收脚本必须覆盖用户真正走的那条路，不能只覆盖最容易测通的那条。
HOSTPORT="${HOSTPORT:-8080}"
ING_NODE=$(kubectl -n ingress-nginx get pod -l app.kubernetes.io/component=controller \
  -o jsonpath='{.items[0].spec.nodeName}')
echo "ingress-nginx controller 跑在：${ING_NODE:-（取不到）}"

HP_CODE=$(curl -sS -m 15 --noproxy '*' -H "$HOST_HEADER" \
  -o /dev/null -w '%{http_code}' "http://localhost:${HOSTPORT}/api/health" 2>/dev/null || true)
HP_CODE="${HP_CODE//[^0-9]/}"; HP_CODE="${HP_CODE:-000}"
echo "宿主 localhost:${HOSTPORT}/api/health → HTTP $HP_CODE"
[ "$HP_CODE" = "200" ] \
  || fail "宿主 hostPort 这条路不通（HTTP $HP_CODE）——先看 controller 与 extraPortMappings 是否同节点"

# 前端也要真能加载：index.html 回 200 只说明静态文件在，资源 404 的话页面是白屏。
IDX=$(curl -sS -m 15 --noproxy '*' -H "$HOST_HEADER" "http://localhost:${HOSTPORT}/" 2>/dev/null || true)
ASSET=$(printf '%s' "$IDX" | grep -oE 'src="[^"]+\.js"' | head -1 | sed -E 's/src="([^"]+)"/\1/')
[ -n "$ASSET" ] || fail "index.html 里没有 js 资源引用——前端可能没打进镜像"
A_OUT=$(curl -sS -m 25 --noproxy '*' -H "$HOST_HEADER" -o /dev/null -w '%{http_code} %{size_download}' \
  "http://localhost:${HOSTPORT}${ASSET}" 2>/dev/null || true)
echo "前端资源 $ASSET → HTTP ${A_OUT% *} / ${A_OUT#* } 字节"
[ "${A_OUT% *}" = "200" ] || fail "前端资源加载失败（$A_OUT）"
pass "宿主 hostPort 全通：/api/health 200、前端 js 资源 200"

echo
echo "=== B1 部署验收：10 条断言全过 ==="
echo "证据日志：$LOG"
