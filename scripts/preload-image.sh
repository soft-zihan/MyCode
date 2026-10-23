#!/usr/bin/env bash
# 把镜像在宿主拉好，再塞进 kind 各节点的 containerd。
#
# 为什么需要它（本机实测，2026-10-23）：节点自己拉镜像有两条死路——
#   · quay.io：吞吐只有 ~0.55 MB/s（cilium:v1.20.3 单个 247MB 拉了 7m47s），
#     而且容器会优先选到 IPv6 地址然后**无限挂住**，挂住的那次 read 没有超时，
#     会把整个 containerd 卡死（journal 停止推进、Pod 卡在 Terminating 40 分钟），
#     只能 `systemctl restart containerd` 救。
#   · ghcr.io：blob CDN（185.199.111.154）直连 `connection reset by peer`，
#     与「不加代理 git clone github.com 会 reset」是同一个故障面。
# 宿主的 docker 走代理，两条都正常，所以「宿主拉 → 塞进节点」是本机唯一可靠的路。
#
# 为什么不用 `kind load docker-image`：它内部带 `--all-platforms`，而宿主 docker 29
# 用 containerd image store，本地不一定有全部平台的 blob，于是报
# `ctr: content digest sha256:...: not found`（cilium/operator-generic 实测就是这样）。
# 这里改成手工 `docker save | ctr images import --digests`：只导入本地实际有的内容，
# 且 `--digests` 会保留 repo digest —— Pod spec 里钉了 `@sha256:...` 时这一点是必需的，
# 否则导进去的镜像对不上 spec 要求的 digest，kubelet 照样去拉。
#
# 用法：
#   ./scripts/preload-image.sh quay.io/argoproj/argocd:v3.5.6 ghcr.io/dexidp/dex:v2.45.1
#   CLUSTER=mycode ./scripts/preload-image.sh <image>...
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
CLUSTER="${CLUSTER:-mycode}"

[ $# -ge 1 ] || { echo "用法: $0 <image> [image...]"; exit 2; }

# 按 kind 打的 label 找节点，别硬编码 "<cluster>-control-plane / -worker"：
# 节点数量是 deploy/kind-cluster.yaml 说了算的，硬编码会在改成 2 worker 时静默漏一个。
# ⚠️ filter 必须写成 `label=<key>=<value>`：docker ps 不认裸的 `io.x-k8s.kind.cluster=x`，
#    会直接报 `invalid filter 'io.x-k8s.kind.cluster'`（2026-10-23 实测踩到）。
NODES=$(docker ps --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{.Names}}')
[ -n "$NODES" ] || { echo "找不到集群 $CLUSTER 的节点容器（集群起了吗？）"; exit 1; }

for img in "$@"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img" >/dev/null
  for n in $NODES; do
    # ⚠️ 别用 `crictl images -q "$img"` 判断镜像在不在：它**忽略名字过滤**，
    #    对任何输入（包括 example.invalid/nope:v0）都吐出节点上全部镜像 ID，
    #    于是"已有，跳过"永远是假阳性、镜像压根没被导入（2026-10-23 实测踩到）。
    #    `ctr images ls -q name==<精确名>` 才是真过滤：在就回名字，不在就回空。
    if [ -n "$(docker exec "$n" ctr --namespace=k8s.io images ls -q "name==$img" 2>/dev/null)" ]; then
      echo "[preload] $n 已有 $img，跳过"
      continue
    fi
    echo "[preload] $img → $n"
    docker save "$img" \
      | docker exec -i "$n" ctr --namespace=k8s.io images import \
          --digests --snapshotter=overlayfs - >/dev/null
  done
done

echo "[preload] 完成。注意：如果某个 Pod 的拉取已经**在飞行中**并挂死了，"
echo "[preload] 导入镜像不会打断它——kubelet 的 serializeImagePulls 队列仍被占着。"
echo "[preload] 这时要删掉那个 Pod 让它重新 sync；containerd 已经卡死的话先重启它。"
