# 部署与复现

这份文档是「别人（包括换台机器的你）能不能把 B1 复现出来」的唯一依据。
所有数字与断言都能重跑，跑不出来说明环境变了，不是文档过期。

---

## 0. 前置

| 项 | 要求 | 怎么确认 |
|---|---|---|
| Docker | daemon 可用 | `docker info` |
| kind / kubectl / helm | 装在 `~/.local/bin` | `kind version` / `kubectl version --client` / `helm version` |
| 宿主端口 8080 / 8443 | 未被占用、未在 Windows 保留段内 | `netsh interface ipv4 show excludedportrange protocol=tcp`（Windows 侧） |
| 内存 | 至少 8GB 空闲 | 集群 + Cilium + ingress-nginx + ArgoCD 合计约 3–4GB |

本机实测环境：Windows 10.0.28120 / WSL2 内核 6.18.54.1 / 5 vCPU / 11.7 GiB /
docker 29.1.3 / kind v0.33.0 / kubectl v1.37.1 / helm v3.22.1。

---

## 1. 本机网络约束（**换机器要重测，否则会在拉镜像时无声卡死**）

这一节不是环境介绍，是踩坑换来的。2026-10-23 实测：

- `registry-1.docker.io` / `index.docker.io` / `auth.docker.io` 的解析结果落在
  `2a03:2880:...:face:b00c:...`（Meta 的 IPv6 段），即 **DNS 被污染**，直连不可用。
- 直连 docker.io 与走本机代理（`127.0.0.1:7897`）的**吞吐都只有 ~185 KB/s**，
  900MB 的 `kindest/node` 要 80 分钟——所以必须换国内源。
- `quay.io` / `ghcr.io` / `registry.k8s.io` 都健康（`/v2/` 返回 401，0.26–0.83s），
  所以 Cilium、ArgoCD、ingress-nginx 的镜像**不需要**特殊处理。

### 1.1 各镜像源实测结论（这一张表是反复试出来的，别再逐个盲试）

| 源 | library 镜像 | kindest/node（大 blob） | 结论 |
|---|---|---|---|
| `docker.1ms.run` | ✅ 能拉（node 329MB、python 188MB） | ❌ manifest 能解析、blob **零字节卡死** | 只适合中小镜像 |
| `docker.m.daocloud.io` | — | ❌ 同样零字节卡死 | 不可用 |
| `docker.xuanyuan.me` | ❌ 403 Forbidden | ❌ 403 Forbidden | 对 docker.io 完全不服务 |
| `docker.melikeme.cn` | — | ⚠️ **返回 0B 坏记录**（把 HTML 当 blob 发） | **危险**，见下面的教训 |
| `dockerproxy.net` | — | ❌ `unexpected media type text/html` | 不可用 |
| **`swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io`** | ✅ | ✅ **5.6 MB/s**（25s 下 139MB） | **首选** |
| `dockerpull.cn` | ✅ manifest OK | 未测 | 备选 |

### 1.2 拉镜像的正确姿势

`/etc/docker/daemon.json` 里配的 mirror **对大 blob 不可靠**，而且 daemon 的回退链有个坑：
mirror 挂住时它**不会**回退到 docker.io，于是 `docker pull node:22-bookworm-slim` 会**永久卡死**
（实测 520s 内 `rx_bytes` 增量为 0）。对照组：`docker pull alpine:3.20` 能成，
说明 daemon 与下载并发槽没坏，问题只在回退链。

**用 `crane` 直连 registry，绕开 dockerd 的 mirror 配置**（单个静态二进制，装在 `~/.local/bin`，不需要 root）：

```bash
# 装 crane（GitHub 可达，16.5MB）
curl -sSL -o /tmp/crane.tgz https://github.com/google/go-containerregistry/releases/latest/download/go-containerregistry_Linux_x86_64.tar.gz
tar xzf /tmp/crane.tgz -C /tmp crane && mv /tmp/crane ~/.local/bin/ && chmod +x ~/.local/bin/crane

HW=swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io
crane pull --platform linux/amd64 $HW/kindest/node:v1.37.0     ~/kindest.tar
crane pull --platform linux/amd64 $HW/library/node:22-bookworm-slim      ~/node.tar
crane pull --platform linux/amd64 $HW/library/python:3.12-slim-bookworm  ~/python.tar
crane pull --platform linux/amd64 $HW/library/nginx:1.27-alpine          ~/nginx.tar
crane pull --platform linux/amd64 $HW/curlimages/curl:8.10.1             ~/curl.tar

# docker load 后 RepoTags 是华为那串长名字，必须显式 tag 回标准名
for t in kindest node python nginx curl; do docker load -i ~/$t.tar; done
docker tag $HW/kindest/node:v1.37.0            kindest/node:v1.37.0
docker tag $HW/library/node:22-bookworm-slim   node:22-bookworm-slim
docker tag $HW/library/python:3.12-slim-bookworm python:3.12-slim-bookworm
docker tag $HW/library/nginx:1.27-alpine       nginx:1.27-alpine
docker tag $HW/curlimages/curl:8.10.1          curlimages/curl:8.10.1
```

### 1.3 宿主代理会被 kind 传进节点，导致**所有**镜像拉取失败（最隐蔽的一个坑）

**症状**：`kind create cluster` 成功，但 Cilium 的 Pod 全部 `Init:ImagePullBackOff`，
node 永远 `NotReady`（`disableDefaultCNI` 下没 CNI 就不会 Ready），`helm install` 最后
`context deadline exceeded`。看着像镜像源问题，其实不是。

**真因**（`crictl pull` 才拿得到，`kubectl describe` 只给你一句 Back-off）：

```
proxyconnect tcp: dial tcp 127.0.0.1:7897: connect: connection refused
```

kind 会把**调用环境**里的 `HTTP(S)_PROXY` 原样传进节点、写进 containerd 的进程环境。
而那个代理跑在 Windows 侧，节点内的 `127.0.0.1:7897` 什么都没有，于是每次拉取都连不上代理。

**实测对比**（同一个 `quay.io/cilium/operator-generic:v1.20.3`，35MB）：

| 路径 | 速度 |
|---|---|
| 走代理 `127.0.0.1:7897` | ~190 KB/s |
| 清空代理变量直连 | **3.0 MB/s（11s 拉完）** |

所以清空代理不是「绕过限制」，是本来就该直连——代理反而是慢的那条路。

**修法**：`scripts/k8s-bootstrap.sh` 顶部已经 `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy`。
换机器复现时如果又见 ImagePullBackOff，先跑这条确认：

```bash
docker exec mycode-control-plane sh -c 'tr "\0" "\n" < /proc/$(pidof containerd)/environ | grep -i proxy'
# 有输出 = 中招了，删掉集群重新 bootstrap
```

顺带一个反直觉的点：**节点内 DNS 是好的**。`getent hosts quay.io` 在节点里只返回 IPv6，
看着像「WSL2 没有 IPv6 默认路由所以拉不动」，但 `getent ahostsv4 quay.io` 能正常返回
`34.192.167.37`。**别被 AAAA 记录优先这条线索带跑**——我在这儿浪费了一轮。

### 1.4 两条必须记住的教训

1. **`docker image inspect` 成功 ≠ 镜像是好的。** `docker.melikeme.cn` 吐的是 HTML，
   daemon 却建了个 **Size=0B** 的镜像记录，`inspect` 照样返回 0。拿这种镜像去 `kind create`
   会在很久之后才以一个莫名其妙的错误失败。
   **判据必须是 `inspect --format '{{.Size}}'` 大于零、且 `{{len .RootFS.Layers}}` 合理。**
2. **kind 节点不继承宿主的 `registry-mirrors`**（节点跑自己的 containerd），
   所以 `verify/*.sh` 里用到的 docker.io 探针镜像必须**宿主拉好再 `kind load`**——
   脚本已经内置了这一步。没有去改节点内 containerd 的 registry 配置：
   containerd 2.x 已移除 `registry.mirrors`、改用 `hosts.toml`，跨版本易碎，而 `kind load` 与版本无关。

---

## 2. 起集群

```bash
./scripts/k8s-bootstrap.sh
```

做的事：建 kind 集群（1 control-plane + 1 worker，`disableDefaultCNI: true`）→
装 Cilium 1.20.3（`kubeProxyReplacement` + Hubble）→ 等 node Ready → 装 ingress-nginx。
幂等，重复跑会跳过已完成的部分。

版本都钉死了，换机器可复现：`CILIUM_VERSION=1.20.3`、`NODE_IMAGE=kindest/node:v1.37.0`、
ingress-nginx `controller-v1.15.1`。要升级就改 `scripts/k8s-bootstrap.sh` 顶部那几行并重新验证。

**预期现象（不是故障）**：`disableDefaultCNI: true` 之后，装完 Cilium 之前 node 会一直 `NotReady`；
`kind create cluster` 可能因等待超时而非零退出，脚本会忽略它继续装 CNI。

### 2.1 `k8sServiceHost` 不能用官方文档里的 `localhost`（多节点 kind 必踩）

Cilium 官方给 kind 的安装命令里写的是 `--set k8sServiceHost=localhost --set k8sServicePort=6443`。
**那份文档是按单节点 kind 写的**——只有 control-plane 节点上跑着 apiserver。

本集群是 1 control-plane + 1 worker，于是 worker 上的 `cilium-agent` 起不来：

```
init 容器 config: Message: server" subsys=cilium-dbg module=k8s-client ipAddr=https://localhost:6443
                  Exit Code: 255
→ 其余 4 个 init 容器全部 PodInitializing 卡住 → node 永远 NotReady
```

**也不能改用 `kubernetes.default.svc`**：Cilium 自己就是 CNI，Service 网络在它跑起来之前根本不存在。
唯一正确的值是 **control-plane 节点的 InternalIP**（本机实测 `192.168.64.3`），
脚本已经改成动态取：

```bash
API_HOST=$(kubectl get node "$CLUSTER-control-plane" \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
```

另外脚本用的是 `helm upgrade --install` 而不是 `install`：这个值改了之后重跑就能生效，
值没变时它是幂等的、不会无谓重启 Pod。用 `install` 的话第二次跑会直接报「已存在」，
而「已装就跳过」的分支又会让错误的值永远留着。

### 2.2 排查镜像拉取失败要看 `crictl`，不是 `kubectl describe`

`kubectl describe pod` 只会告诉你 `Back-off pulling image`，把真因藏起来。
进节点用 `crictl pull <image>` 才拿得到 containerd 的原始错误：

```bash
docker exec mycode-control-plane crictl pull quay.io/cilium/cilium:v1.20.3
```

上面那个代理坑（1.3 节）就是靠这条命令才看见 `proxyconnect tcp: ... connection refused` 的。

验收：`kubectl get nodes` 两个 node 都 `Ready`；`kubectl -n kube-system get pods -l k8s-app=cilium` 全 Running。

---

## 3. 建 Secret（**不入库，换机器要重跑**）

```bash
./scripts/k8s-secret.sh
```

从 `$HOME/.my-code/config.json` 生成 `mycode-config`（整份配置挂到 Pod 的 `/etc/mycode/config.json`），
并生成 `mycode-secrets`（`auth-token` 取 `$MYCODE_AUTH_TOKEN`，没设就 `openssl rand -hex 32`）。

> ⚠️ K8s Secret 只是 base64，**不是加密**。任何能 `kubectl get secret -o yaml` 的人都能还原。
> 真加密要靠外部 KMS 或 Sealed Secrets，本项目没做（本地集群，边界见第 7 节）。

`deploy/` 下**不存在也不该存在** `secret.yaml`——`tests/unit/test_deploy_assets.py` 有一条断言专门守这个，
另有一条断言扫全目录不许出现 `api_key` 的字面值。

---

## 4. 部署

**Task 6 之前（本地手动喂镜像）**：

```bash
docker build -t mycode/control-plane:b1 .
docker tag mycode/control-plane:b1 ghcr.io/soft-zihan/mycode:b1-local
kind load docker-image ghcr.io/soft-zihan/mycode:b1-local --name mycode

kubectl apply -k deploy/base     # 先建 namespace，Secret 才有地方放
./scripts/k8s-secret.sh
kubectl apply -k deploy/kind
kubectl -n mycode rollout status deploy/control-plane --timeout=180s
```

`imagePullPolicy: IfNotPresent` + tag 与 manifests 一致，节点就会用 `kind load` 进去的本地镜像，不去 GHCR 拉。

**Task 6/7 之后（CI + GitOps）**：push → GitHub Actions 构建推 GHCR（tag = commit SHA）→
自动回写 `deploy/kind/kustomization.yaml` 的 `newTag` → ArgoCD 检测到 Git 变化自动同步。
回滚 = `git revert` 那次回写提交。

镜像 tag 的两个约定，都是硬理由：
- **base 里不写 `images` transformer**，tag 只在 `deploy/kind` 决定——这样 CI 只有一个地方要改。
- **绝不用 `latest`**：① ArgoCD 比对的是 Git 内容，`latest` 不变则它认为「无变化」而永不同步；
  ② `imagePullPolicy: IfNotPresent` 遇到同名 `latest` 会静默用节点上的旧镜像。

---

## 5. 验证

```bash
./verify/netpol-probe.sh    # 4 条断言：证明 NetworkPolicy 在本机集群真的被执行
./verify/deploy-probe.sh    # 9 条断言：B1 部署验收
./scripts/container-smoke.sh  # 7 条断言：镜像层（不需要集群）
```

证据落在 `verify/logs/*.log`（已 gitignore）。

**为什么 netpol-probe 是双向断言**：先证明「没有策略时通」，再证明「加了策略后不通」。
只测后半段没有意义——一个网络本来就全断的集群也会「通过」。
这条必须有，因为 Docker Desktop 内置 K8s 的 NetworkPolicy 会**静默失效**
（策略能创建、`kubectl get` 看着正常、但流量照过；上游 `docker/for-mac#4626` 被 stale-bot 关闭未修），
而 B2 的「沙箱断网 + 只放行模型端点」整条安全叙事都压在「断网真的断了」这个前提上。

---

## 6. 浏览器访问

`deploy/base/ingress.yaml` 的 host 是 `mycode.local`，ingress-nginx 走 hostPort，宿主映射到 **8080/8443**。

**方式 A（改 hosts）**：Windows 侧以管理员身份编辑 `C:\Windows\System32\drivers\etc\hosts`，加一行

```
127.0.0.1 mycode.local
```

然后开 <http://mycode.local:8080>。

**方式 B（不改 hosts）**：

```bash
kubectl -n mycode port-forward svc/control-plane 5556:80
```

开 <http://localhost:5556>。（**别用 5555**，见下面的坑。）

**方式 C（走 Ingress 但用 Host 头，不改 hosts）**：

```bash
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 18080:80
curl -H 'Host: mycode.local' http://127.0.0.1:18080/api/health
```

`verify/deploy-probe.sh` 的断言 6 就是用方式 C 验全链路的。

---

## 7. 已知边界（照抄，别美化）

- 集群是本机 **WSL2 上的 kind**，单宿主 5 vCPU / 11.7 GiB，1 control-plane + 1 worker。
  **多 worker 共享同一宿主内核与网卡，故障不隔离**，所以本项目**不宣称高可用**。
- 控制面**单副本**，`strategy: Recreate`——因为状态在 **RWO PVC** 上，不能多副本挂同一块盘。
  滚动更新不断流要等 C 批把会话后端换成 PostgreSQL、控制面无状态化。
- 镜像**不含 `eval/`**，云端不提供评测端点（`main.py` 对它的导入是 non-fatal try，启动日志会打一行失败，属预期）。
- 宿主无 `/dev/kvm`，**Kata 级硬件虚拟化隔离不可行**；gVisor 的 systrap 平台理论可行但**未验证**，
  `runtimeClassName` 只在 Job spec 里预留了位置，尚未启用。
- 隔离强度是**容器级加固 + 网络策略**，不是硬件虚拟化级。
- Secret 只是 base64，不是加密。
- 没有真实公网入站、没有云 LB、没有真实证书链、没有托管 etcd、没有 Cluster Autoscaler。

**主动交代边界比被追问出来强**——它把「你没做过生产」转换成「你清楚自己做到了哪、还差什么」。

---

## 8. 两个会咬人的坑

### 8.1 `start.sh` 会顺手杀掉集群里的 Pod 进程

`start.sh` 的 `kill_port()` 用 `lsof -ti:5555 -sTCP:LISTEN` 找进程然后 `kill -9`。
而 **lsof 从 WSL 宿主能看见容器内的进程**——容器有独立 netns，但与宿主共享 PID 命名空间，
且 lsof 按端口号匹配时不区分 netns。控制面 Pod 里的 uvicorn 正好听 5555，于是会被匹配到并杀掉。

**症状**：跑一次 `./start.sh`，Pod 无声重启（K8s 会自愈，所以不容易注意到）；
`docker run` 起的容器则直接 `Exited (0)`（uvicorn 收 SIGTERM 优雅退出）。

**规避**：跑 `start.sh` 前后别指望容器还活着；port-forward 用 5556 而不是 5555。

### 8.2 `pkill -f` 会匹配到执行它的那个 shell 自己

要杀后台进程：模式必须避开自己命令行里会出现的字符串，或者先 `pgrep` 拿 pid 再 `kill <pid>`。
本项目一个会话里被这条咬过三次。

---

## 9. 拆除

```bash
kind delete cluster --name mycode     # 删集群（PVC 与 Secret 一起没）
docker rmi ghcr.io/soft-zihan/mycode:b1-local mycode/control-plane:b1   # 可选，删镜像
docker volume rm mycode-try-data      # 可选，删本地试跑容器留下的卷
```

`kind delete cluster` 不动宿主上的镜像与 buildkit 缓存，重建集群时不用重新拉基础镜像。
