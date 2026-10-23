#!/usr/bin/env bash
# 一条命令走完「从开机到能用」。
#
# 之前是 8 步手工活（钉住 WSL 发行版 → 起宿主代理转发 → 建集群 → 生成 Secret →
# 部署 → 预载镜像 → 装 ArgoCD → 改 Windows hosts）。这个脚本把前 7 步按依赖顺序串起来，
# 每一步都幂等，所以「跑一次装好」和「重启后又跑一次」是同一条命令。
# 第 8 步（改 hosts）已经不需要了：deploy/base/ingress.yaml 里加了一条不写 host 的
# 兜底规则，浏览器直接开 http://localhost:8080 就通。
#
# 用法：
#   ./scripts/cloud-up.sh up        # 装/修（默认）
#   ./scripts/cloud-up.sh status    # 只看现状，不改任何东西
#   ./scripts/cloud-up.sh down --yes  # 拆集群（不可逆，必须显式确认）
#
# Windows 侧不用记这些：`mycode cloud-up` / `cloud-status` / `cloud-down`
# 会先钉住发行版再转进来（见 scripts/install-mycode.ps1）。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p logs

CLUSTER="${CLUSTER:-mycode}"
NS="${NS:-mycode}"
HOSTPORT="${HOSTPORT:-8080}"
CLOUD_URL="${CLOUD_URL:-http://localhost:$HOSTPORT}"
# 本地后端（routers/cloud.py）从这里读云端地址与 token，所以「一键起云端」之后
# 前端的云端面板不需要用户再手填。它在 $HOME 下，不在仓库里，不会被 git 看到。
CLOUD_JSON="${MYCODE_CLOUD_JSON:-$HOME/.my-code/cloud.json}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { echo "[$(date '+%H:%M:%S')] ❌ $*" >&2; exit 1; }
step() { echo; log "── $* ──"; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "缺 $1。$2"
}

app_image() {
  # 从 kustomize 渲染结果里抽，不硬编码：deploy/kind 的 newTag 是 CI 会回写的那一行，
  # 写死在这里的话，CI 一推新镜像这个脚本就还在预载旧的。
  kubectl kustomize deploy/kind 2>/dev/null \
    | sed -n 's|^[[:space:]]*image:[[:space:]]*||p' | tr -d '"' | sort -u | head -1
}

cluster_exists() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER"
}

write_cloud_json() {
  local token
  token=$(kubectl -n "$NS" get secret mycode-secrets -o jsonpath='{.data.auth-token}' 2>/dev/null \
    | base64 -d 2>/dev/null || true)
  [ -n "$token" ] || { log "取不到 auth-token，跳过 $CLOUD_JSON（前端云端面板会提示要手填）"; return 0; }
  mkdir -p "$(dirname "$CLOUD_JSON")"
  # 用 python 写而不是拼字符串：token 是 hex 不会有特殊字符，但 url 是用户可覆盖的，
  # 拼 JSON 迟早被一个引号或反斜杠咬到。
  CLOUD_JSON="$CLOUD_JSON" CLOUD_URL="$CLOUD_URL" CLOUD_TOKEN="$token" python3 - <<'PY'
import json, os
path = os.environ["CLOUD_JSON"]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"url": os.environ["CLOUD_URL"], "token": os.environ["CLOUD_TOKEN"]}, f, indent=2)
    f.write("\n")
PY
  chmod 600 "$CLOUD_JSON"
  log "已写 $CLOUD_JSON（含云端 token，权限 600，不在仓库里）"
}

do_status() {
  step "集群"
  cluster_exists || { echo "集群 $CLUSTER 不存在。跑：./scripts/cloud-up.sh up"; return 0; }
  kubectl get nodes 2>&1 | sed 's/^/  /'
  echo
  step "mycode 命名空间"
  kubectl -n "$NS" get pods,deploy,svc,ingress 2>&1 | sed 's/^/  /'
  echo
  step "ArgoCD"
  kubectl -n argocd get application 2>&1 | sed 's/^/  /' || echo "  （未安装）"
  echo
  step "跑的镜像"
  kubectl -n "$NS" get deploy control-plane \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | sed 's/^/  /' || true
  echo
  step "Git 声明的镜像"
  echo "  $(app_image)"
  echo
  step "浏览器那条路（宿主 localhost:$HOSTPORT，不带 Host 头）"
  local code
  code=$(curl -sS -m 8 --noproxy '*' -o /dev/null -w '%{http_code}' "$CLOUD_URL/api/health" 2>/dev/null || echo 000)
  echo "  $CLOUD_URL/api/health → HTTP $code"
  if [ "$code" = 200 ]; then
    echo "  ✅ 直接开 $CLOUD_URL 就能用（不用改 Windows hosts）"
  elif [ "$code" = 404 ]; then
    # 404 = 连上了 ingress-nginx，但没有规则命中这个 Host。
    # base/ingress.yaml 里那条不写 host 的兜底规则就是为它准备的；出现 404 说明
    # 集群里跑的还是旧版 Ingress（apply 一下 deploy/kind，或让 ArgoCD 同步）。
    echo "  ❌ 404：ingress 没有规则命中 Host=localhost。集群里跑的可能是加兜底规则之前的 Ingress。"
    echo "     修：kubectl apply -k deploy/kind   （或等 ArgoCD 同步）"
  else
    echo "  ❌ HTTP $code，连不上。先看 controller 落在哪个节点：kubectl -n ingress-nginx get pod -o wide"
    echo "     extraPortMappings 只在 control-plane 节点上，controller 落到 worker 就是 connection reset"
  fi
  echo
  step "本地后端读的云端配置"
  if [ -f "$CLOUD_JSON" ]; then
    echo "  $CLOUD_JSON 存在（url=$(python3 -c 'import json,os;print(json.load(open(os.environ["CLOUD_JSON"]))["url"])' CLOUD_JSON="$CLOUD_JSON" 2>/dev/null || echo '?')）"
  else
    echo "  $CLOUD_JSON 不存在——前端云端面板会用缺省地址 $CLOUD_URL 且没有 token"
  fi
}

do_up() {
  need docker "本机的 docker 必须原生装在 WSL 发行版里（不是 Docker Desktop），见 deploy/README.md §8.3"
  need kind   "安装：go install sigs.k8s.io/kind@latest 或见 https://kind.sigs.k8s.io/docs/user/quick-start/"
  need kubectl "安装：https://kubernetes.io/docs/tasks/tools/"
  need curl   ""
  need python3 ""

  # 不钉住的话本条命令一返回，WSL 就把发行版拆了，集群活不过几十秒（§8.3）。
  ./scripts/pin-wsl.sh

  step "1/7 集群 + Cilium + ingress-nginx（幂等）"
  ./scripts/k8s-bootstrap.sh

  step "2/7 宿主代理转发（Pod 出网唯一的路）"
  ./scripts/host-proxy-relay.sh start
  RELAY_URL="$(./scripts/host-proxy-relay.sh url)"
  log "Pod 侧代理地址：$RELAY_URL"

  step "3/7 namespace 与 ConfigMap mycode-net"
  # 只先建 namespace，不 apply 整个 base：base 里 Deployment 的镜像是占位 tag
  # b1-local，节点上没有它，先建出来只会白撞一轮 ImagePullBackOff。
  kubectl apply -f deploy/base/namespace.yaml
  # git 专用代理（不是全局 HTTPS_PROXY）：Pod 直连 github.com:443 会超时，
  # 而模型端点是直连可达的，全局改道会平白多一个故障面。
  # 网关地址每台机器都不同，所以走 ConfigMap 而不是写进 base 的 manifest。
  kubectl -n "$NS" create configmap mycode-net \
    --from-literal=git-proxy="$RELAY_URL" \
    --dry-run=client -o yaml | kubectl apply -f -

  step "4/7 Secret（模型 api_key + 访问令牌，不入库）"
  ./scripts/k8s-secret.sh

  step "5/7 预载应用镜像"
  IMAGE="$(app_image)"
  [ -n "$IMAGE" ] || die "从 deploy/kind 渲染不出镜像名"
  log "镜像：$IMAGE"
  # 节点自己拉 ghcr.io 的 blob CDN 会 connection reset（deploy/README.md §1.1），
  # 所以宿主拉好再塞进节点的 containerd。
  ./scripts/preload-image.sh "$IMAGE"

  step "6/7 部署并等就绪"
  kubectl apply -k deploy/kind
  kubectl -n "$NS" rollout status deploy/control-plane --timeout=300s

  step "7/7 ArgoCD 接管（GitOps）"
  ./scripts/argocd-bootstrap.sh

  write_cloud_json
  do_status
  echo
  log "✅ 完事。浏览器开 $CLOUD_URL"
}

do_down() {
  [ "${1:-}" = "--yes" ] || die "拆集群不可逆（PVC 里的会话数据一起没）。确认请加 --yes"
  cluster_exists || { log "集群 $CLUSTER 本来就不存在"; return 0; }
  kind delete cluster --name "$CLUSTER"
  ./scripts/host-proxy-relay.sh stop || true
  log "已拆除集群 $CLUSTER"
}

case "${1:-up}" in
  up)     do_up ;;
  status) do_status ;;
  down)   shift; do_down "${1:-}" ;;
  *)
    echo "用法: $0 {up|status|down --yes}"
    exit 2
    ;;
esac
