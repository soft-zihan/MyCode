#!/usr/bin/env bash
# 证明本机集群的 NetworkPolicy 被真实执行。
#
# 为什么必须有这个脚本：Docker Desktop 内置 K8s 的 NetworkPolicy 会**静默失效**
# （策略能创建、kubectl get 看着正常、但流量照过；上游 docker/for-mac#4626 被
# stale-bot 关闭未修）。B2 的「沙箱断网 + 只放行模型端点」整条安全叙事都压在
# 「断网真的断了」这个前提上，所以前提本身要有可重跑的证据。
#
# 关键设计是**双向断言**：先证明「没有策略时通」，再证明「加了策略后不通」。
# 只测后半段没有意义——一个网络本来就全断的集群也会「通过」。
set -euo pipefail

# 清空代理：本脚本的 curl 都跑在集群内的探针 Pod 里、不经宿主代理，但 docker pull
# 与 kubectl 会读这些变量。宿主的 HTTP_PROXY 指向 Windows 侧的 127.0.0.1:7897，
# 在别的上下文里会造成假 502（详见 verify/deploy-probe.sh 顶部的同一段说明）。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

export PATH="$HOME/.local/bin:$PATH"

NS="netpol-probe"
CLUSTER="${CLUSTER:-mycode}"
LOG="verify/logs/netpol-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

cleanup() { kubectl delete ns "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 探针镜像先在宿主拉、再 kind load 进节点，不让节点自己去 docker.io 拉。
# 原因是 kind 节点跑自己的 containerd，**不继承**宿主 /etc/docker/daemon.json 的
# registry-mirrors；而本机对 index.docker.io / auth.docker.io 的解析结果是
# 2a03:2880:...:face:b00c:...（Meta 的 IPv6 段，即 DNS 污染），节点内直连极慢甚至挂死。
# 宿主有 mirror 所以拉得动，kind load 是把镜像塞进节点最省事的办法（幂等）。
# 不去改节点内 containerd 的 registry 配置：containerd 2.x 已移除 registry.mirrors、
# 改用 hosts.toml，跨版本易碎，而 kind load 与版本无关。
PROBE_IMAGES=(nginx:1.27-alpine curlimages/curl:8.10.1)
echo "=== 把探针镜像送进 kind 节点（宿主拉 → kind load）==="
for img in "${PROBE_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img" >/dev/null
  kind load docker-image "$img" --name "$CLUSTER" >/dev/null
  echo "  loaded: $img"
done

echo "=== 准备探针命名空间 $NS ==="
cleanup
kubectl create ns "$NS" >/dev/null

# imagePullPolicy=IfNotPresent：镜像已 kind load 进节点，别让 kubelet 再去 docker.io 拉
kubectl -n "$NS" run probe-server --image=nginx:1.27-alpine --port=80 \
  --image-pull-policy=IfNotPresent --labels='app=probe-server' >/dev/null
kubectl -n "$NS" run probe-client --image=curlimages/curl:8.10.1 \
  --image-pull-policy=IfNotPresent --labels='app=probe-client' --command -- sleep 3600 >/dev/null
kubectl -n "$NS" wait --for=condition=Ready pod/probe-server pod/probe-client --timeout=300s

SERVER_IP=$(kubectl -n "$NS" get pod probe-server -o jsonpath='{.status.podIP}')
echo "probe-server podIP = $SERVER_IP"

curl_from_client() {  # $1=目标 URL  $2=超时秒；永远只回三位数字，失败即 000
  # ⚠️ 别写成 `curl ... || echo blocked`：curl 被拒时 `-w '%{http_code}'` **仍会打出 000
  # 且不带换行**，紧跟的 echo 会拼成 `000blocked`，于是「策略生效」反被判成 FAIL。
  # 这个坑真踩过一次，报错信息还会把人引向「CNI 不执行策略」这个完全错误的方向。
  local out
  out=$(kubectl -n "$NS" exec probe-client -- \
    curl -sS -m "$2" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null) || true
  out="${out//[^0-9]/}"          # 只留数字，剥掉 kubectl 可能夹带的杂讯
  echo "${out:-000}"
}
dns_from_client() {
  kubectl -n "$NS" exec probe-client -- nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1
}

echo "=== 断言 1：无任何策略时，跨 Pod 访问必须通 ==="
CODE=$(curl_from_client "http://$SERVER_IP" 10)
[ "$CODE" = "200" ] \
  || fail "没有策略时就不通（得到 $CODE）——集群网络本身有问题，后面的「不通」不能归因于 NetworkPolicy"
pass "无策略时 client → server 200"

echo "=== 断言 2：无策略时 DNS 必须能解析 ==="
dns_from_client || fail "无策略时 DNS 就解析不了"
pass "无策略时 DNS 正常"

echo "=== 施加 default-deny ingress（namespace 级，空 podSelector 波及全部 Pod）==="
cat <<'YAML' | kubectl apply -f - >/dev/null
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: netpol-probe
spec:
  podSelector: {}
  policyTypes: ["Ingress"]
YAML
sleep 8   # 给 CNI 一点时间把策略编程进数据面

echo "=== 断言 3：加策略后，跨 Pod 访问必须被拒（本脚本最关键的一条）==="
CODE=$(curl_from_client "http://$SERVER_IP" 8)
[ "$CODE" = "blocked" ] || [ "$CODE" = "000" ] \
  || fail "NetworkPolicy 没有生效！仍然拿到 HTTP $CODE —— 这台机器上的 CNI 不执行策略，B2 的断网设计不成立"
pass "default-deny 生效：client → server 被拒"

echo "=== 断言 4：只限 Ingress 的策略不该影响 DNS（egress 未被限制）==="
dns_from_client || fail "只加了 Ingress 策略却把 DNS 弄坏了，说明 CNI 行为异常"
pass "Ingress 策略未波及 DNS"

echo
echo "=== NetworkPolicy 在本机集群被真实执行 ==="
echo "证据日志：$LOG"
echo "这条断言是 B2「沙箱 default-deny + FQDN 白名单」的前提，换机器/换 CNI 都要重跑。"
