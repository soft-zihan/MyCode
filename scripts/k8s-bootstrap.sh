#!/usr/bin/env bash
# 建 kind 集群 + 装 Cilium + 装 ingress-nginx。
# 幂等：集群已存在时跳过创建，组件已装时跳过安装。
#
# 用法：
#   ./scripts/k8s-bootstrap.sh
# Cilium 版本已钉死在下方（换机器也能复现）；要升级就改这一行并重新验证。
set -euo pipefail

# ⚠️ 代理变量的处理是**分裂**的，两边要求相反，别图省事全局 unset：
#
# - **节点内不能有代理**：kind 会把调用环境的 HTTP(S)_PROXY 原样传进节点、写进 containerd
#   的进程环境，而那个代理跑在 Windows 侧，节点内的 127.0.0.1:7897 什么都没有，于是每次拉取
#   都是 `proxyconnect tcp: dial tcp 127.0.0.1:7897: connect: connection refused`
#   → Pod 全部 ImagePullBackOff、node 永远 NotReady。
#   实测：走代理拉 quay.io 约 190 KB/s，不走代理直连 3 MB/s（快 15 倍）。
#   → 所以只在 `kind create cluster` 那一步用子 shell 清空。
#
# - **宿主必须有代理**：`raw.githubusercontent.com` 只解析出 IPv6（2606:50c0::/64），
#   而 WSL2 没有 IPv6 默认路由，不走代理就是 code=000 秒失败；走代理是 200。
#   全局 unset 的后果很隐蔽——`kubectl apply -f <url>` 会报
#   `x509: certificate signed by unknown authority`，看着像证书问题，其实是连不上。
#   → 所以宿主侧保留代理，并且**用 curl 先把 manifest 下到本地文件再 apply**，
#     这样失败时错误信息是 curl 的、能看懂，而不是被 kubectl 包装成证书错误。

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

CLUSTER="${CLUSTER:-mycode}"
# 钉版本理由：Cilium 的 CNI 行为直接决定 B2「沙箱断网」成不成立，
# 换版本等于换安全边界，必须显式升级而不是顺手飘上去。
# 1.20.3 = 2026-10-23 `helm search repo cilium/cilium --versions` 的最新稳定版。
CILIUM_VERSION="${CILIUM_VERSION:-1.20.3}"
# 节点镜像同样钉死：kind 的默认值随 kind 版本走（v0.33.0 的默认就是 v1.37.0，
# 与本机 kubectl v1.37.1 对齐）。不钉的话换台机器就是另一套 k8s 版本，
# deploy/ 里那些 manifests 的 apiVersion 兼容性也就无从复现。
NODE_IMAGE="${NODE_IMAGE:-kindest/node:v1.37.0}"
# ingress-nginx 也钉到 release tag 而不是 main：main 会随上游移动，
# 换台机器就装出另一个版本，deploy/ 里 Ingress 的注解兼容性也就无从复现。
# controller-v1.15.1 = 2026-10-23 `gh api repos/kubernetes/ingress-nginx/releases/latest`。
INGRESS_VERSION="${INGRESS_VERSION:-controller-v1.15.1}"
INGRESS_MANIFEST="https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_VERSION}/deploy/static/provider/kind/deploy.yaml"
# registry.k8s.io 的国内可达镜像前缀（理由见下面装 ingress-nginx 那段的注释）。
# 用 ${VAR-default} 而不是 ${VAR:-default}，这样 `export K8S_REGISTRY_MIRROR=` 能显式关掉改写。
K8S_REGISTRY_MIRROR="${K8S_REGISTRY_MIRROR-swr.cn-north-4.myhuaweicloud.com/ddn-k8s}"

log() { echo "[bootstrap] $*"; }

export PATH="$HOME/.local/bin:$PATH"
for bin in docker kind kubectl helm; do
  command -v "$bin" >/dev/null || { echo "缺 $bin（kind/kubectl/helm 装在 ~/.local/bin）"; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "docker daemon 不可用"; exit 1; }

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "集群 $CLUSTER 已存在，跳过创建"
else
  log "创建集群 $CLUSTER（disableDefaultCNI=true，装完 Cilium 前 node 会 NotReady）"
  # disableDefaultCNI 下 kind 的就绪等待可能超时并非零退出——那不是失败，
  # 控制面静态 Pod 已经起来了，装完 CNI 后 node 自然转 Ready。所以这里不让它中断脚本。
  # 子 shell 里清空代理：只影响 kind 这一次调用，宿主的代理配置保持原样
  # （理由见文件顶部那段——节点内不能有代理，宿主必须有代理）。
  (
    unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    kind create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --config deploy/kind-cluster.yaml
  ) || log "kind create 非零退出（多半是等 node Ready 超时，属预期），继续装 CNI"
fi

kubectl config use-context "kind-$CLUSTER" >/dev/null
log "当前 context: $(kubectl config current-context)"

log "装 Cilium $CILIUM_VERSION（kubeProxyReplacement + Hubble）"
helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium >/dev/null 2>&1

# k8sServiceHost 必须是 control-plane 节点的 InternalIP，**不能用 Cilium 官方 kind 文档里的
# `localhost`**：那份文档是按单节点 kind 写的，只有 control-plane 上跑着 apiserver。
# 本集群有 1 个 worker，worker 上的 cilium-agent 连 localhost:6443 直接失败——
# init 容器 `config` 报 `ipAddr=https://localhost:6443`、Exit Code 255，node 永远 NotReady。
# 也不能用 kubernetes.default.svc：Cilium 自己就是 CNI，Service 网络在它起来之前还不存在。
# 分两步取，别写成 "${API_HOST:-$(kubectl ...)}"：jsonpath 里的双引号会被外层
# 参数扩展的引号截断，报 `error parsing jsonpath ... unclosed action`。
if [ -z "${API_HOST:-}" ]; then
  API_HOST=$(kubectl get node "$CLUSTER-control-plane" \
    -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
fi
[ -n "$API_HOST" ] || { echo "取不到 $CLUSTER-control-plane 的 InternalIP"; exit 1; }
log "apiserver 地址（Cilium 用）：$API_HOST:6443"

# 用 upgrade --install 而不是 install：值改了（比如上面这个 API_HOST）重跑就能生效，
# 值没变时它是幂等的，不会无谓重启 Pod。
# cgroup.autoMount.enabled=false + hostRoot=/sys/fs/cgroup 是因为 kind 节点
# 已经把 cgroup 挂好了，再自动挂一次会冲突。
helm -n kube-system upgrade --install cilium cilium/cilium --version "$CILIUM_VERSION" \
  --set ipam.mode=kubernetes \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="$API_HOST" \
  --set k8sServicePort=6443 \
  --set cgroup.autoMount.enabled=false \
  --set cgroup.hostRoot=/sys/fs/cgroup \
  --set securityContext.capabilities.ciliumAgent="{CHOWN,KILL,NET_ADMIN,NET_RAW,IPC_LOCK,SYS_ADMIN,SYS_RESOURCE,DAC_OVERRIDE,FOWNER,SETGID,SETUID}" \
  --set securityContext.capabilities.cleanCiliumState="{NET_ADMIN,SYS_ADMIN,SYS_RESOURCE}" \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --wait --timeout 10m

# helm upgrade 之后 cilium agent 会重建 eBPF 的 socket-LB 映射，紧接着发起的新连接
# 可能被短暂误路由到别的 TLS 端点，kubectl 于是报
# `Unable to connect to the server: tls: failed to verify certificate: x509: certificate signed by unknown authority`。
# 实测这个窗口只有几秒（隔一分钟同一条命令返回的是正常的 NotFound），
# 所以这里等它稳定，而不是去查证书——证书本身没问题。
log "等 apiserver 连接稳定（cilium 重建 eBPF 映射的窗口）"
for i in $(seq 1 24); do
  if kubectl get ns >/dev/null 2>&1; then log "apiserver 连接正常（第 $i 次探测）"; break; fi
  [ "$i" = 24 ] && { echo "120s 内 apiserver 仍连不上，请手工检查"; exit 1; }
  sleep 5
done

log "等 node Ready（Cilium 装好后才会转 Ready）"
# 超时给到 15 分钟，别照抄文档里的 300s。本机到 quay.io 的**吞吐**很差（延迟正常）：
# 实测 cilium:v1.20.3 单个镜像 247MB 拉了 7m47s ≈ 0.55 MB/s，300s 必然超时。
# 超时的后果很隐蔽——脚本 exit 1，但节点其实几分钟后就 Ready 了，
# 于是「集群看着是好的，只有 ingress-nginx 莫名其妙没装」。
kubectl wait --for=condition=Ready node --all --timeout=900s

if [ "$(kubectl -n ingress-nginx get deploy ingress-nginx-controller \
        -o jsonpath='{.status.availableReplicas}' 2>/dev/null)" = "1" ]; then
  log "ingress-nginx 已就绪，跳过"
else
  log "装 ingress-nginx（kind 专用 manifest，controller 走 hostPort，不需要 LB provider）"
  # 先用 curl 下载再 apply，不让 kubectl 自己去 fetch URL：
  # raw.githubusercontent.com 只有 IPv6 记录、必须走宿主代理，而 kubectl fetch 失败时
  # 会把它包装成 `x509: certificate signed by unknown authority`，看着像证书问题、
  # 实际是连不上，排查方向会被整个带偏（我就被带偏了一轮）。
  MANIFEST_FILE="verify/logs/ingress-nginx-${INGRESS_VERSION}.yaml"
  curl -fsSL --retry 3 -o "$MANIFEST_FILE" "$INGRESS_MANIFEST" \
    || { echo "下载 ingress manifest 失败：$INGRESS_MANIFEST（检查宿主代理是否可用）"; exit 1; }

  # registry.k8s.io 会 302 跳到 asia-east1-docker.pkg.dev（Google Artifact Registry），
  # 而本机到 Google 是 `dial tcp 142.250.157.82:443: i/o timeout`，节点内根本拉不动
  # （quay.io 能用是因为它走 AWS CloudFront，不是 Google）。
  # 改写成华为 SWR 的 ddn-k8s 镜像路径：节点可直连、实测 5.6MB/s。
  # 换机器时若 registry.k8s.io 可直连，`export K8S_REGISTRY_MIRROR=` 即可关掉这次改写。
  if [ -n "$K8S_REGISTRY_MIRROR" ]; then
    sed -i "s#image: registry.k8s.io/#image: ${K8S_REGISTRY_MIRROR}/registry.k8s.io/#g" "$MANIFEST_FILE"
    # 还必须摘掉上游的 @sha256 digest 钉：那是 registry.k8s.io 的**多架构 index digest**，
    # 镜像源给的是平台专属 manifest 的 digest，两者不同。留着钉的后果很隐蔽——
    # containerd 里镜像明明已经存在（`crictl pull` 回 "Image is up to date"），
    # kubelet 却一直 `Back-off pulling image`，因为它要的那个 digest 谁都没有。
    # 实测：钉的是 01038e7d…，华为源实际是 1442d220…。
    # 代价是不再按内容寻址、改为信任镜像源的 tag——本地集群可接受，生产要用真源 + digest。
    sed -i -E "s#(image: ${K8S_REGISTRY_MIRROR//./\\.}/registry\.k8s\.io/[^ \"']+)(@sha256:[0-9a-f]+)#\1#g" "$MANIFEST_FILE"
    log "registry.k8s.io → ${K8S_REGISTRY_MIRROR}/registry.k8s.io（并摘掉 digest 钉）"
  fi

  # admission webhook 是 Job，而 Job 的 spec.template 不可变——改镜像后 apply 会失败，
  # 所以重跑前先把旧 Job 删掉。
  kubectl -n ingress-nginx delete job -l app.kubernetes.io/component=admission-webhook \
    --ignore-not-found >/dev/null 2>&1 || true

  log "manifest $(wc -l < "$MANIFEST_FILE") 行 → $MANIFEST_FILE"
  kubectl apply -f "$MANIFEST_FILE" >/dev/null
fi
kubectl -n ingress-nginx wait --for=condition=Available deploy/ingress-nginx-controller --timeout=300s

log "=== 集群就绪 ==="
kubectl get nodes -o wide
echo
kubectl -n kube-system get pods -l k8s-app=cilium --no-headers | head -5
echo
kubectl version -o json 2>/dev/null | grep -oP '"gitVersion":\s*"\K[^"]+' | head -2 | sed 's/^/k8s 版本: /'
echo
echo "下一步：./verify/netpol-probe.sh   # 证明 NetworkPolicy 真的生效"
echo "Hubble UI：kubectl -n kube-system port-forward svc/hubble-ui 12000:80 后开 http://localhost:12000"
