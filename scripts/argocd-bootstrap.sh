#!/usr/bin/env bash
# 装 ArgoCD 并创建指向本仓库 deploy/kind 的 Application。
# 幂等：namespace 与 Application 用 apply，ArgoCD 已装则跳过。
#
# 用法：
#   ./scripts/argocd-bootstrap.sh
#   REPO_URL=... TARGET_REVISION=... ./scripts/argocd-bootstrap.sh
#
# ArgoCD 全家桶约占 500MB-1GB 内存；宿主只有 11.7GB，装完要盯一下余量。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p verify/logs

export PATH="$HOME/.local/bin:$PATH"

NS=argocd
# 必须钉版本：stable 分支会随上游移动，换台机器就复现不出同一套行为。
# v3.5.6 = 2026-10-23 `gh api repos/argoproj/argo-cd/releases/latest --jq .tag_name`。
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.6}"
# 默认取当前仓库的 origin 与当前分支，这样换 fork 也不用改脚本。
REPO_URL="${REPO_URL:-$(git remote get-url origin 2>/dev/null | sed 's#^git@github.com:#https://github.com/#')}"
TARGET_REVISION="${TARGET_REVISION:-$(git rev-parse --abbrev-ref HEAD)}"

for bin in kubectl envsubst git; do
  command -v "$bin" >/dev/null || { echo "缺 $bin"; exit 1; }
done
[ -n "$REPO_URL" ] || { echo "拿不到 origin 地址，请显式 export REPO_URL="; exit 1; }

echo "[argocd] 版本=$ARGOCD_VERSION  仓库=$REPO_URL  分支=$TARGET_REVISION"

kubectl create ns "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

MANIFEST_FILE="verify/logs/argocd-${ARGOCD_VERSION}.yaml"

# 刻意**不做**「已装就跳过」的短路判断。上一版用 `kubectl get deploy argocd-server` 判断，
# 结果一次 apply 中途失败（CRD 撞注解上限）已经把 argocd-server 建出来了，
# 重跑时判定为"已装"→ 跳过预载与 apply → 镜像永远不在节点上、CRD 永远缺失，
# 而脚本只报一个看不懂的 rc=1（2026-10-23 实测）。
# 下面两步本身都是幂等的（preload 按精确镜像名跳过、server-side apply 可重复执行），
# 所以每次都跑才是最简单也最可靠的做法。

# 先下载到本地再 apply，与 scripts/k8s-bootstrap.sh 的 ingress-nginx 段同一个套路：
# 1.9MB 的 manifest 不该每次 apply 都重抓，留一份在 verify/logs/ 也便于事后核对。
if [ ! -f "$MANIFEST_FILE" ]; then
  echo "[argocd] 下载 install.yaml（$ARGOCD_VERSION）"
  curl -fsSL --retry 3 -m 180 -o "$MANIFEST_FILE" \
    "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
fi

# 必须先把镜像塞进节点。ArgoCD 的镜像分散在 quay.io / ghcr.io / public.ecr.aws，
# 前两个从节点直连都不可用：quay.io 只有 ~0.55 MB/s 且会选到 IPv6 然后把整个
# containerd 挂死（journal 停止推进），ghcr.io 的 blob CDN 直连 connection reset。
# 镜像清单直接从 manifest 里抽，不硬编码——否则升 ArgoCD 版本时这里会静默过期。
echo "[argocd] 从 manifest 抽出镜像清单并预载进节点"
IMAGES=$(awk '/^[[:space:]]*image:[[:space:]]*/ {print $2}' "$MANIFEST_FILE" | tr -d '"' | sort -u)
echo "$IMAGES" | sed 's/^/[argocd]   /'
# 刻意不加引号：这里需要按空白拆成多个参数
./scripts/preload-image.sh $IMAGES

echo "[argocd] 装 $ARGOCD_VERSION"
# 必须 `--server-side`：ArgoCD 的 applicationsets CRD schema 太大，客户端 apply 会把它
# 塞进 `kubectl.kubernetes.io/last-applied-configuration` 注解，直接撞 256KB 上限——
#   The CustomResourceDefinition "applicationsets.argoproj.io" is invalid:
#   metadata.annotations: Too long: may not be more than 262144 bytes
# 服务端 apply 不写那个注解，所以没这个限制。--force-conflicts 是让重跑时能接管
# 上一次客户端 apply 留下的字段所有权，不加会在第二次运行时报冲突。
# 这一步**不能**写成 `... >/dev/null`：它失败过（CRD 注解撞 256KB 上限），
# 把输出吞掉之后日志里只剩一个孤零零的 rc=1，一点线索都没有。
# 但也不能直接 `| grep -v`：全部资源 unchanged 时 grep 匹配不到任何行会返回 1，
# 那恰恰是成功的情形。所以先接住输出与退出码，再滤噪声。
APPLY_RC=0
APPLY_OUT=$(kubectl -n "$NS" apply --server-side --force-conflicts -f "$MANIFEST_FILE" 2>&1) || APPLY_RC=$?
echo "$APPLY_OUT" | grep -vE ' (unchanged|configured|created|serverside-applied)$' || true
[ "$APPLY_RC" = 0 ] || { echo "[argocd] install.yaml apply 失败（rc=$APPLY_RC）"; exit 1; }

# repo-server 是唯一需要在 Pod 里访问 GitHub 的组件（clone 仓库），而本机
# Pod 直连 github.com:443 会超时（同一时刻 api.github.com 却是 200）。
# 宿主代理绑在 127.0.0.1、Pod 够不着，所以走 host-proxy-relay 在网桥网关上开的口。
# 设 ARGOCD_PROXY="" 可跳过（例如换到一台能直连 GitHub 的机器）。
# relay 的输出**不要**重定向掉：它失败时的理由只在自己嘴里（例如端口被别的进程占了），
# 吞掉之后这里就只剩一个没有线索的 rc=1。
if [ -z "${ARGOCD_PROXY+x}" ]; then
  ./scripts/host-proxy-relay.sh start
  ARGOCD_PROXY=$(./scripts/host-proxy-relay.sh url)
fi
if [ -n "$ARGOCD_PROXY" ]; then
  GW_HOST=$(sed -E 's#^https?://([^:/]+).*#\1#' <<<"$ARGOCD_PROXY")
  # 容器名动态取，不硬编码：上游改名字时这里不会静默失效。
  C=$(kubectl -n "$NS" get deploy argocd-repo-server -o jsonpath='{.spec.template.spec.containers[0].name}')
  echo "[argocd] 给 repo-server（容器 $C）注入代理 $ARGOCD_PROXY"
  # 用 strategic merge 而不是 json patch 的 `/env/-` 追加：env 的合并键是 name，
  # 所以这个 patch 幂等——重跑不会堆出三份 HTTP_PROXY。
  # NO_PROXY 必须覆盖集群内所有地址，否则 repo-server 连自己的 apiserver 与 redis
  # 也会被塞给代理，表现为 Application 一直 Unknown 而日志里全是代理连接错误。
  kubectl -n "$NS" patch deploy argocd-repo-server --type=strategic -p "{
    \"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$C\",\"env\":[
      {\"name\":\"HTTP_PROXY\",\"value\":\"$ARGOCD_PROXY\"},
      {\"name\":\"HTTPS_PROXY\",\"value\":\"$ARGOCD_PROXY\"},
      {\"name\":\"NO_PROXY\",\"value\":\"localhost,127.0.0.1,.svc,.svc.cluster.local,.cluster.local,$GW_HOST,10.0.0.0/8,192.168.0.0/16,172.16.0.0/12\"}
    ]}]}}}
  }" >/dev/null
  kubectl -n "$NS" rollout status deploy/argocd-repo-server --timeout=300s
else
  echo "[argocd] ARGOCD_PROXY 为空，跳过代理注入（假定本机能直连 GitHub）"
fi

echo "[argocd] 等 argocd-server 可用"
kubectl -n "$NS" rollout status deploy/argocd-server --timeout=600s

# Application 里的 repoURL 与 targetRevision 用 envsubst 注入，避免把仓库地址写死两份
export REPO_URL TARGET_REVISION
envsubst < deploy/argocd/application.yaml | kubectl apply -f -

echo "[argocd] 等 Application 首次同步（最多 5 分钟）"
for i in $(seq 1 60); do
  S=$(kubectl -n "$NS" get application mycode -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "")
  H=$(kubectl -n "$NS" get application mycode -o jsonpath='{.status.health.status}' 2>/dev/null || echo "")
  [ "$S" = "Synced" ] && [ "$H" = "Healthy" ] && break
  sleep 5
done

echo "=== ArgoCD 就绪 ==="
kubectl -n "$NS" get application mycode
echo
echo "取初始管理员密码："
echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo "开 UI（账号 admin）："
echo "  kubectl -n argocd port-forward svc/argocd-server 8081:443   # 然后开 https://localhost:8081"
echo
echo "⚠️ ArgoCD 读的是 **GitHub 上的分支**，不是你本地的工作树。"
echo "   本地未 push 的 manifests 改动它看不见——本地调试用 kubectl apply -k deploy/kind，两条路并存。"
echo "下一步：./verify/gitops-probe.sh"
