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
- `quay.io` 与 `ghcr.io` 健康（`/v2/` 返回 401，0.78–0.83s），且**节点内可直连**
  （quay.io 走 AWS CloudFront），所以 Cilium、ArgoCD 的镜像不需要特殊处理。
- ⚠️ **`registry.k8s.io` 是陷阱**：`/v2/` 也返回 401（0.26s，看着最健康），
  但真拉镜像时它会 **302 跳到 `asia-east1-docker.pkg.dev`（Google Artifact Registry）**，
  而本机到 Google 是 `dial tcp 142.250.157.82:443: i/o timeout`。
  **`/v2/` 探活通 ≠ 能拉镜像**，这条把我骗了一轮。ingress-nginx 的镜像因此必须换源，见 1.2。

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

### 1.4 三条必须记住的教训

1. **`docker image inspect` 成功 ≠ 镜像是好的。** `docker.melikeme.cn` 吐的是 HTML，
   daemon 却建了个 **Size=0B** 的镜像记录，`inspect` 照样返回 0。拿这种镜像去 `kind create`
   会在很久之后才以一个莫名其妙的错误失败。
   **判据必须是 `inspect --format '{{.Size}}'` 大于零、且 `{{len .RootFS.Layers}}` 合理。**
2. **kind 节点不继承宿主的 `registry-mirrors`**（节点跑自己的 containerd），
   所以 `verify/*.sh` 里用到的 docker.io 探针镜像必须**宿主拉好再 `kind load`**——
   脚本已经内置了这一步。没有去改节点内 containerd 的 registry 配置：
   containerd 2.x 已移除 `registry.mirrors`、改用 `hosts.toml`，跨版本易碎，而 `kind load` 与版本无关。
3. **换镜像源就必须同时摘掉上游的 `@sha256:` digest 钉。**
   ingress-nginx 的官方 manifest 把镜像写成 `registry.k8s.io/...:v1.15.1@sha256:594ceea7…`，
   那是 **registry.k8s.io 的多架构 index digest**；华为源给的是平台专属 manifest 的 digest
   （实测 `1442d220…`），两者不同。
   症状极隐蔽：镜像明明已经在 containerd 里（`crictl pull` 回 `Image is up to date`），
   kubelet 却一直 `Back-off pulling image`——因为它要的那个 digest 谁都没有。
   代价是不再按内容寻址、改为信任镜像源的 tag。**本地集群可接受，生产要用真源 + digest。**

### 1.5 探针镜像与 `kind load`

`verify/netpol-probe.sh` 与 `verify/deploy-probe.sh` 会自己把 docker.io 的探针镜像
（`nginx:1.27-alpine`、`curlimages/curl:8.10.1`）宿主拉好再 `kind load` 进两个节点，
并用 `imagePullPolicy=IfNotPresent` 阻止 kubelet 再去外网拉。
两个脚本都能独立重跑，不依赖对方已经 load 过。

⚠️ **`kind load` 会失败**：它内部带 `--all-platforms`，而宿主 docker 29 用 containerd image store、
本地不一定有全部平台的 blob，于是报 `ctr: content digest sha256:...: not found`
（`quay.io/cilium/operator-generic` 实测就是这样）。
**改用 `scripts/preload-image.sh`**——它做同一件事，但走手工
`docker save | ctr images import --digests`：只导入本地实际有的内容，
且 `--digests` 保留 repo digest（Pod spec 里钉了 `@sha256:` 时这是必需的，
否则导进去的镜像对不上 spec 要的 digest，kubelet 照样去外网拉）。

### 1.6 Pod 的出网能力与「宿主代理够不着」问题（2026-10-23 实测矩阵）

这一节决定 ArgoCD 能不能用 GitHub 当源，**换机器必须重测**。

| 从哪儿 | 到哪儿 | 结果 |
|---|---|---|
| Pod | `github.com:443` | **超时**（DNS 能解析出 `20.205.243.166`，TCP 连不上） |
| Pod | `api.github.com` | **200 / 0.6s** ← 同一时刻是通的，所以不是"集群没网" |
| Pod | `ghcr.io/v2/` | 401 / 1.5s（API 通） |
| Pod | ghcr 的 blob CDN `185.199.111.154:443` | **`connection reset by peer`** |
| kind 节点（host netns） | `github.com:443` | 超时 |
| kind 节点 | `api.github.com` | 200 / 5.7s |
| WSL 宿主，**不带**代理 | `github.com` | DNS 解析就超时 |
| WSL 宿主，**带**代理 | `github.com` | 200；`git ls-remote` 成功 |
| Pod | 宿主代理 `192.168.64.1:7897` | **connection refused**（代理只绑 127.0.0.1） |

两条结论：

1. **`api.github.com` 通而 `github.com` 不通**，所以「`gh` 命令能用」完全不能推出「集群能 clone」。
   ArgoCD 的 repo-server 是在 **Pod 里** clone 的，走的是 `github.com` 的 smart-http 端点，正好是不通的那个。
2. **宿主代理绑在 loopback，Pod 够不着**——这是唯一的堵点，而不是"没有代理可用"。

**解法：`scripts/host-proxy-relay.sh`。** 它在 kind 网桥的网关地址（`192.168.64.1`）上开一个
TCP 转发口（默认 7899），转到宿主的 `127.0.0.1:7897`。开了之后实测：

```
Pod -x http://192.168.64.1:7899 → github.com                    200 / 1.8s
Pod -x http://192.168.64.1:7899 → .../info/refs?service=git-upload-pack   200 / 587 bytes
```

第二条就是 git clone 真正打的请求，拿到 refs 广告说明 clone 能成。
`scripts/argocd-bootstrap.sh` 会自动起这个转发并给 `argocd-repo-server` 注入
`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（用 strategic merge patch，`env` 的合并键是 `name`，
所以重跑不会堆出三份）。

⚠️ **`NO_PROXY` 必须覆盖集群内所有地址**（`.svc`、`.svc.cluster.local`、`10.0.0.0/8` 等），
否则 repo-server 连自己的 apiserver 与 redis 也会被塞给代理，
表现为 Application 一直 `Unknown` 而日志里全是代理连接错误——这个症状会把人往"仓库权限"方向带。

⚠️ 转发进程活在被 WSL 拆发行版的那条规则下（§8.3），发行版被拆它就没了：
开工先钉住发行版，或者重新 `./scripts/host-proxy-relay.sh start`。

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
./verify/deploy-probe.sh    # 10 条断言：B1 部署验收
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

⚠️ **方式 A 能用的前提是 controller 跑在 control-plane 节点上**：`extraPortMappings`
是**按节点**声明的（只在 `mycode-control-plane` 那个容器上），而 controller 用 hostPort——
它落在哪个节点，80/443 就绑在哪个节点。上游 manifest 的 `nodeSelector` 只有
`kubernetes.io/os: linux`，两个节点都匹配，调度器会挑 worker，于是宿主 `localhost:8080`
是 `connection reset by peer`，而**集群内 port-forward 一切正常**（方式 B/C 都通）——
这个组合极容易误判成 CNI 或 Ingress 规则坏了。

`scripts/k8s-bootstrap.sh` 已经处理：给 control-plane 打 `ingress-ready=true` 标签，
再 patch controller 的 `nodeSelector`（manifest 本来就 tolerate 了 control-plane 的
NoSchedule 污点，所以只缺这一个 selector）。实测之后：

```
GET http://localhost:8080/api/health   → 200  {"status":"ok"}
GET http://localhost:8080/             → 200  text/html, 418 B
GET http://localhost:8080/assets/index-DH9LBTtN.js → 200  text/javascript, 1163804 B
```

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

## 8. 四个会咬人的坑

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

### 8.3 WSL 发行版会被拆掉，dockerd 与 kind 节点跟着一起死（本批最贵的一个坑）

**根因**：本机的 docker 是**原生装在 WSL 发行版里的**（`/usr/bin/dockerd -H fd://`，`docker context` 里的
`desktop-linux` 是 Docker Desktop 的、那个发行版处于 Stopped）。而 **WSL2 会在发行版内没有前台进程时把它整个关掉**。
从 Windows 侧跑 `wsl -e bash -lc '...'`：命令启动 → 发行版被唤醒 → systemd 拉起 docker.service →
docker 按 `on-failure` 把 kind 节点容器启起来 → 命令退出 → **发行版被拆 → dockerd 死 → 节点容器全停**。
下一条命令再来一遍。集群的寿命等于单条命令的寿命。

**一行就能确诊**（在 WSL 里）：

```bash
systemctl status docker --no-pager | head -6 ; cat /proc/uptime
```

看到 `Active: activating (start) since ...; 7s ago` 且 `/proc/uptime` 只有几十秒 —— 发行版刚开机，
dockerd PID 是个三位数（实测 509）。**集群没坏，是地基每几十秒被抽走一次。**

**修法：钉一个常驻进程把发行版留住。**

```bash
wsl -e bash -c 'setsid nohup sleep infinity >/dev/null 2>&1 </dev/null &'
```

钉住之后 `uptime` 会持续增长、`systemctl is-active docker` 从 `activating` 变 `active`、
`docker ps` 里节点的 `Up` 时长开始正常累加。**这一步没做，后面所有的 bootstrap 与验证都是白跑**
（本批为此浪费了一整轮 10 分钟的集群重建）。

**症状链**（不知道根因时你会按这个顺序被带偏，实测走过的弯路）：

1. `kubectl` 报 `Error from server (Forbidden): pods is forbidden: User "kubernetes-admin" cannot list resource "pods"`
   —— 但 `kubectl auth can-i list pods -n mycode` 回 `yes`，而且同一个命令过一会儿又能跑。
   **这不是 RBAC 配错**：apiserver 每次随发行版重启，重启后十几秒内 RBAC informer 还没同步完，授权一律拒。
   `kubectl get pods` 报 Forbidden 而 `-o wide` 正常，看着像某种诡异权限规则，其实只是抖动窗口撞上了不同请求。
   **别去翻 RBAC，先数 apiserver 的重启次数。**
2. Pod 状态一片 `Unknown`，事件里刷 `Pod sandbox changed, it will be killed and re-created`；
   `RESTARTS` 涨到 10+，然后**突然归零**——归零是容器被重建（而非重启）的指纹。
3. `docker events` 里只有 `start`、**一条 `die` 都没有**，而 `docker inspect` 的 `State.FinishedAt` 有值、
   `RestartCount=0`。事件流随 daemon 一起没了 ＝ daemon 重启过，不是容器崩了。
4. 有时 control-plane 容器直接 exit 1，`docker logs` 打出
   `INFO: detected IPv4 address:`（**空**）+ `ERROR: have an old IPv4 address but no current IPv4 address (!)`，
   `docker network inspect kind` 的 `Containers` 里根本没有这个节点。这是第 3 层的连带损伤，不是独立故障。

**为什么钉住之后还要重建集群**：kind 节点的 IP 由 docker IPAM 在容器启动时分配，
每轮重启都可能**互换**（实测 control-plane 在 `.2`/`.3` 之间来回）。而这个 IP 被三处钉死：
apiserver 服务证书的 SAN、Cilium 的 `k8sServiceHost`（bootstrap 按当时的 InternalIP 算出来写进 helm values）、
kubelet 的 `--node-ip`。IP 一漂，Cilium 连不上 apiserver，节点永远 NotReady。手工逐个改不如重来：

```bash
kind delete cluster --name mycode
./scripts/k8s-bootstrap.sh          # 重新解析 InternalIP，证书与 Cilium 配置一起重生
kubectl apply -k deploy/base && ./scripts/k8s-secret.sh && kubectl apply -k deploy/kind
```

**通用判据**：`kubectl` 的每一次抽风都只是在转述控制面的抽风，控制面的抽风又只是在转述宿主的抽风。
排查顺序永远是 **`/proc/uptime` → `docker ps` 的 `Up` 时长 → 节点内 kubelet → 集群内对象**，
反过来做会在 RBAC 和 CNI 上浪费一小时。

### 8.4 一次没有超时的 IPv6 拉取能把整个 containerd 卡死

**症状**：节点上一批 Pod 卡在 `ContainerCreating`，`journalctl -u containerd` 的最后一行时间戳
**十几分钟不动**，同时有 Pod 卡在 `Terminating` 四十分钟删不掉。
看着像"镜像源慢"，实际是 containerd 已经**不响应**了。

**根因**：`quay.io` 会返回 AAAA 记录，containerd 选了 IPv6 地址去连，
而 WSL2 **没有 IPv6 默认路由**——这条 TCP read 就那么挂着，`read: connection timed out`
要等内核自己的超时（实测约 5 分钟一次），期间整个拉取流水线停摆。
kubelet 默认 `serializeImagePulls: true`，所以**一个挂死的拉取会堵住该节点上所有镜像拉取**，
连带 ingress-nginx、业务镜像全都排在后面动不了。

```
level=error msg="PullImage \"quay.io/cilium/operator-generic:v1.20.3@sha256:efdd8d89…\" failed"
  error="… failed to copy: read tcp [fc00:f853:ccd:e793::3]:39344->[2600:140f:5e00:38::17df:2aeb]:443:
  read: connection timed out"
```

**修法**：

```bash
docker exec mycode-worker systemctl restart containerd   # shim 会保住正在跑的容器
./scripts/preload-image.sh <那些拉不动的镜像>              # 宿主拉好再塞进节点
kubectl -n <ns> delete pod <卡住的 pod>                     # 让 kubelet 重新 sync
```

实测重启 containerd 后 3 分钟内全集群 Pod 归位（`非Running` 计数从 4 掉到 0）。

**两个由此定下的做法**：

1. **导入镜像不会打断已在飞行中的拉取。** `preload-image.sh` 把镜像塞进 containerd 之后，
   kubelet 那个挂死的 pull 请求仍占着串行队列——必须**删掉 Pod** 让它重新发起 sync，
   新 sync 才会看到镜像已在本地。这一步漏了会以为"导入了没用"。
2. **能预载就预载，别让节点自己去 quay.io / ghcr.io 拉。** 见 §1.5 与 §1.6。

---

## 9. 拆除

```bash
kind delete cluster --name mycode     # 删集群（PVC 与 Secret 一起没）
docker rmi ghcr.io/soft-zihan/mycode:b1-local mycode/control-plane:b1   # 可选，删镜像
docker volume rm mycode-try-data      # 可选，删本地试跑容器留下的卷
```

`kind delete cluster` 不动宿主上的镜像与 buildkit 缓存，重建集群时不用重新拉基础镜像。
