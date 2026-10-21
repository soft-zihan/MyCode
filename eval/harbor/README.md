# Harbor coding-agent benchmark

用 [Harbor](https://github.com/av/harbor)（Terminal-Bench 官方 harness）在 **aider-polyglot**（225 道 Exercism 题，6 种语言）上对比两个 coding agent，模型固定同一个：

| 臂 | agent | 接入方式 |
| --- | --- | --- |
| 对照 | `opencode` | Harbor 内置，零开发 |
| 实验 | MyCode | 本目录自定义 installed agent，容器内进程内嵌入 `run_agent_task`（LOCA 同款，不走 CLI） |

两臂跑**同一批题、同一个模型、同一套容器环境**，只有 agent 不同，所以是配对设计，可以用 McNemar 检验。

> **结论（25 题实测，详见第 5 节）**：通过率 **12/25 vs 11/25，McNemar p = 1.0，打平**；
> 输入 token **持平**（探测批的「省 29%」在 25 题上翻了符号，见 5.3）。
> ⚠️ **「MyCode 快 1.9×」已作废**：那一轮 MyCode 臂发了 `enable_thinking:false`（完全不思考）、
> opencode 臂全程思考，速度差全是思考开关的差，不是 scaffold 的差——归因见 **5.6**，
> 参数实测见 **2.12**。重跑请用 `--thinking default`（现在已是默认）。
> 本文 1–4 节是环境、Harbor 0.24.0 的实测事实与已知限制；第 5 节是数据。

---

## 1. 环境准备（本机已装好，重装按这个来）

全部在 **WSL（Ubuntu-24.04，WSL 3.0.3）** 里跑，Windows 侧只放代码。C 盘零新增占用。
（题目容器里的基础镜像是另一回事：`buildpack-deps:jammy` = Ubuntu 22.04，见 2.7。）

### 1.1 WSL 内装原生 docker engine

不用 Docker Desktop。装完 `docker info` 的 `DockerRootDir` 要落在 D 盘的 VHDX 里。

当前 shell 不在 docker 组里时，所有 docker/harbor 命令都要用 `sg docker -c "..."` 包一层。

### 1.1b compose / buildx 插件必须自己装（踩过的坑，报错很难认）

原生 docker engine **不自带** `docker compose` 和 `docker buildx`，而这两个都是硬需求：
harbor 靠 `docker compose build|up` 起题目容器，镜像预热靠
`docker buildx build --output type=cacheonly`（`runner.py` 的 `prewarm_command`）。

本机曾经是从 **Docker Desktop 的 WSL 挂载**借的软链
（`/usr/local/lib/docker/cli-plugins/*` → `/mnt/wsl/docker-desktop/cli-tools/...`）。
Docker Desktop 一关，16 个软链全成死链，而报错完全指不到根因：

```
docker compose --project-name ... build
→ unknown flag: --project-name          # 返回码 125
```

`docker` 认不出 `compose` 是子命令，就把 `--project-name` 当成自己的 flag 了；
harbor 那头只抛一个 `RuntimeError: Docker compose command failed ... Return code: 125`。
**排查方法**：`docker compose version` 和 `docker buildx version` 各打一次，
再 `find /usr/local/lib/docker/cli-plugins/ -xtype l` 看有没有死链。

修法（`setup_env.sh` 的 0b 步已经做了，手工装也一样）：从 GitHub releases 下官方二进制，
`install -m 0755` 到 `/usr/local/lib/docker/cli-plugins/`，文件名必须是
`docker-compose` / `docker-buildx`。当前装的是 compose v5.6.0、buildx v0.38.0（amd64）。

两个附带的坑：

- **下载别放 `/tmp`**：WSL 的 `/tmp` 会被清，实测过 68 MB 的 buildx 下到一半文件就没了。
  `setup_env.sh` 一律落 `~/.mycode/dl/`。
- **68 MB 的 buildx 直连下不动**（~170 KB/s 还会断），走宿主代理 `127.0.0.1:7897` 稳。
  下完必须 `file` 验一下是不是 ELF——不完整时 `file` 会报 `too large section header offset`。

### 1.2 让 dockerd 能拉镜像（关键坑）

Docker Hub 直连会 DNS 污染超时。dockerd **不继承** shell 的 `HTTP_PROXY`，必须用 systemd drop-in：

`/etc/systemd/system/docker.service.d/http-proxy.conf`

```ini
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:7897"
Environment="HTTPS_PROXY=http://127.0.0.1:7897"
Environment="NO_PROXY=localhost,127.0.0.1,10.*,172.16.*,192.168.*"
```

改完 `sudo systemctl daemon-reload && sudo systemctl restart docker`，`docker info | grep -i proxy` 能看到即生效。

> 这条只解决 **daemon 拉镜像**。容器**运行时**的出网是另一回事，见 1.4。

### 1.2b 配 registry 镜像站（否则基础镜像拉不动）

只靠代理拉 Docker Hub **慢到会卡死**：`buildpack-deps:jammy`（1.04 GB，225 题的共同基镜像）走代理约 250 KB/s，
拉到 400 MB 就停住不动了。

`/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
```

同一个镜像，**16 秒拉完**。

镜像站存活情况实测（`curl <mirror>/v2/`，401 是正常的"活着但要 token"）：

| 镜像站 | 直连 | 实际能用 |
| --- | --- | --- |
| `docker.1ms.run` | 401 / 0.45s | ✅ 16s 拉完 1.04 GB |
| `docker.m.daocloud.io` | 401 / 0.14s | ❌ 拉 blob 时 `502 Bad Gateway` |
| `docker.xuanyuan.me` | 401 / 1.34s | 未测 |
| `docker.1panel.live` | 200 / 1.32s | 走代理超时 |
| `hub.rat.dev` | 302 | 未测 |
| `dockerproxy.net` / `docker.mirrors.ustc.edu.cn` | 连不上 | ❌ |

`/v2/` 探活**不能**证明能拉下来——daocloud 探活最快，真拉就 502。要显式拉一次才算数。

如果 `registry-mirrors` 对某个镜像不生效，可以直接显式拉再打 tag：

```bash
docker pull docker.1ms.run/library/buildpack-deps:jammy
docker tag  docker.1ms.run/library/buildpack-deps:jammy buildpack-deps:jammy
```

**注意**：这个 WSL 里的 dockerd 和 Docker Desktop 是两个独立 daemon（`docker context ls` 里 `default` vs `desktop-linux`）。
上面所有改动只影响 WSL 这个，Docker Desktop 里用户的容器不受影响。改之前先确认：

```bash
sg docker -c "docker ps -a"      # 应该是 0 个容器
sg docker -c "docker info --format '{{.Name}} {{.DockerRootDir}}'"
```

### 1.3 装 harbor（隔离，不污染全局）

`uv tool install` 会建独立 venv，系统 `python3` 和仓库 `.venv` 都看不到 harbor（已实测：两边都 `ModuleNotFoundError`）。

PyPI 走代理会超时，改用清华源 + 不走代理：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  UV_HTTP_TIMEOUT=180 \
  uv tool install harbor
```

装到的是 **harbor 0.24.0**。

### 1.4 让容器能出网（第二个关键坑）

WSL 是 **mirrored 网络模式**：Windows 的 LAN IP（本机 `10.147.17.2`）在 WSL 里同样存在，Clash 开了 Allow LAN，所以**容器里能直接用宿主代理**：

```
容器内 curl -x http://10.147.17.2:7897 https://registry.npmjs.org/  → 200
```

`172.17.0.1`（docker 网桥网关）**不行**，那儿没监听。

#### 容器出网实测（两个时间点，结论不一样）

| 目标 | 直连 | 走宿主代理 |
| --- | --- | --- |
| `registry.npmjs.org` | 200 / 0.9s | 200 |
| `nodejs.org` | 200 / 1.8s | 200 |
| `github.com` | 一度超时，后来 200 / 0.7s | 200 / 1.0s |
| `codeload.github.com`（tarball） | 200 / 0.9s | 200 / 1.8s |
| `raw.githubusercontent.com` | **connection reset** | 200 / 0.4s（16 KB） |
| `mirrors.aliyun.com` / `npmmirror` | 200，InRelease 0.5s | — |
| `pypi.tuna.tsinghua.edu.cn` | **宿主 200 / 0.45s，但容器内 403** | 403 |
| `archive.ubuntu.com` | InRelease 1.9s（干净） | — |
| `model.cherygpt.com`（评测网关） | 通 | **不要走代理**，放 NO_PROXY |

#### 清华源在容器内是 403，在宿主上是 200（2026-10-18 实测）

`PIP_INDEX_URL` 原本指向清华源，结果 agent 安装期直接失败：

```
ERROR: Could not find a version that satisfies the requirement openai>=1.0.0 (from versions: none)
```

`from versions: none` 是 pip 在「索引一个候选都没返回」时的说法，不是「这个包不存在」——
很容易误判成 requirements 写错了。逐层测下来：

| 位置 | `curl https://pypi.tuna.tsinghua.edu.cn/simple/openai/` |
| --- | --- |
| WSL 宿主，直连 | **200** / 0.45s |
| WSL 宿主，走代理 | 403 |
| 容器内，直连 | **403** / 0.22s |
| 容器内，强制 IPv4 | **403** / 0.22s（所以不是 IPv6 的锅，两个域名都有 A 记录） |
| 容器内，阿里源 | **200** / 0.20s |

清华源对容器内这个客户端就是不给（返回 403 而不是超时，说明是策略拒绝）。
**所以 `PIP_INDEX_URL` 已改成 `https://mirrors.aliyun.com/pypi/simple`**
（`PIP_TRUSTED_HOST=mirrors.aliyun.com`）——阿里源本来就在 `NO_PROXY` 里，
而且基础镜像的 apt 源用的也是它，一致。

教训有两层：**这个坑只在容器内出现，在宿主上 curl 是发现不了的**；
以及排查容器网络问题时，`from versions: none` / `BADSIG` / `NOSPLIT` 这类报错
都要先怀疑「索引返回了非预期内容」，而不是先怀疑配置写错。

**这个网络是间歇性的。** 同一台机器，前一轮 apt 全部报
`Clearsigned file isn't valid, got 'NOSPLIT'`、国内镜像报 `BADSIG`（HTTP 响应被中途改写），
后一轮再测同样的 URL 就完全干净、0.5s 拿到完整 255850 字节。
所以**不要把某一次的失败当成永久结论**，也不要指望它一直好。

由此定下的策略：

- **apt 一律走国内镜像**（aliyun）。既是速度考虑（0.5s vs 走代理 4 分钟只跑完 `apt-get update`），也是绕开境外 CDN 的不稳定。
- **只有 GitHub 系必须走代理**（`raw.githubusercontent.com` 直连被 reset，nvm 的安装脚本在那儿）。
- **代理很慢**：9.77 MB 的 tarball 走了 85s（约 115 KB/s）。所以凡是能直连的，都不要塞进代理。
- 评测网关 `model.cherygpt.com` 必须进 NO_PROXY，否则绕到海外节点再回来。

#### 一个反直觉的坑：ubuntu:24.04 里没有 ca-certificates

想用 HTTPS 镜像源绕开 HTTP 改写，**在 `ubuntu:24.04` 上行不通**：

```
W: ...: No system certificates available. Try installing ca-certificates.
Err: ... Certificate verification failed: The certificate is NOT trusted.
```

装 ca-certificates 又需要 apt 先能工作 —— 鸡生蛋。而且 apt 此时 `apt-get update` 的**退出码仍然是 0**，
只在后面 `install` 时报 `Unable to locate package`，很容易误判成"更新成功了"。

`buildpack-deps:jammy`（题目镜像的基镜像）自带 ca-certificates，所以**题目容器里 HTTPS 源可用**，这个坑只影响裸 ubuntu 镜像。

### 1.5 往容器里注环境变量的正确姿势

Harbor 的注入口是 **JobConfig 的 `environment.env`**，写成 YAML 用 `-c` 传：

```yaml
environment:
  env:
    HTTP_PROXY: "http://10.147.17.2:7897"
    HTTPS_PROXY: "http://10.147.17.2:7897"
    NO_PROXY: "localhost,127.0.0.1,cherygpt.com,.cherygpt.com"
```

链路（0.24.0 源码）：`environment.env` → `environments/factory.py:371` 变成 `persistent_env` → `docker.py::_compose_env_vars()` → `_startup_env()` → `write_env_compose_file()` 生成 `{"services":{"main":{"environment":{...}}}}` 的 compose override，**容器一起来就带上**。

**已实测验证**：在跑起来的容器里 `env | grep -i proxy`，六个变量原样可见。

两个容易踩的反直觉点：

- **`--env-file` 不是容器 env**。它是 `load_dotenv(env_file, override=True)`（`cli/jobs.py:1456`），只加载进 harbor **宿主进程**。
- **宿主 env 不会漏进容器**。容器 env 走 `_compose_env_vars(include_os_env=False)`（`docker.py:1480`）；`include_os_env=True` 那处（`docker.py:722`）只用于 compose 子进程的变量插值。所以想给容器东西，只能显式走 `environment.env` 或 `--ae`。

> LAN IP 是 DHCP 的，会变。runner 每次运行时重新探测，不写死。

### 1.5b 跑通了的完整配方

容器 env（`-c` 传的 JobConfig）：

```yaml
environment:
  env:
    HTTP_PROXY: "http://<LAN IP>:7897"
    HTTPS_PROXY: "http://<LAN IP>:7897"
    http_proxy: "http://<LAN IP>:7897"
    https_proxy: "http://<LAN IP>:7897"
    NO_PROXY: "localhost,127.0.0.1,::1,10.*,172.16.*,172.17.*,172.18.*,192.168.*,cherygpt.com,.cherygpt.com,aliyuncs.com,.aliyuncs.com,mirrors.aliyun.com,mirrors.tuna.tsinghua.edu.cn,pypi.tuna.tsinghua.edu.cn,registry.npmmirror.com,archive.ubuntu.com,security.ubuntu.com,registry.npmjs.org,nodejs.org,deb.nodesource.com,npmjs.org,.npmjs.org"
    no_proxy: "<同上>"
    PIP_INDEX_URL: "https://mirrors.aliyun.com/pypi/simple"
    PIP_TRUSTED_HOST: "mirrors.aliyun.com"
    UV_DEFAULT_INDEX: "https://mirrors.aliyun.com/pypi/simple"
    NVM_NODEJS_ORG_MIRROR: "https://mirrors.aliyun.com/nodejs-release"
```

每一项都是实测逼出来的：

| 变量 | 为什么 |
| --- | --- |
| `HTTP(S)_PROXY` | 只有 GitHub 系必须走代理：`raw.githubusercontent.com` 直连被 reset（nvm 的安装脚本在那儿） |
| `NO_PROXY` 里放 npmjs/nodejs | **直连比代理快 40 倍**：node 31 MB 直连 6.9s（4.5 MB/s），走代理 60s 只下了 6.3 MB（105 KB/s）。npmjs 元数据同理：直连 4.3 MB/s vs 代理 139 KB/s |
| `NO_PROXY` 里放 `cherygpt.com` | 评测网关在国内（阿里云上海），走代理等于绕地球一圈 |
| `NO_PROXY` 里放 launchpad 系 | python 题的 `sources.list` 里有 deadsnakes PPA，**agent 安装期的 `apt-get update` 每道题都会打 `ppa.launchpadcontent.net`**。走代理实测吃到 `503 Service Unavailable [IP: <宿主> 7897]`，apt 失败 → `ensure_system_dependencies` 抛异常 → 整个 trial 记 exception，agent 一行代码都没跑到。直连 200 / 7.2s，走代理 200 / 11s（还不保证下次不 503） |
| `NVM_NODEJS_ORG_MIRROR` | nvm 认这个变量（harbor 自己的 codex/eve adapter 就在用）。指向 aliyun，`index.tab` 0.33s、`SHASUMS256.txt` 0.18s，内容与 nodejs.org 完全一致（字节数相同），nvm 会校验 checksum |
| `PIP_INDEX_URL` / `UV_DEFAULT_INDEX` | 题目 verifier 自己要 `pip3 install pytest`，PyPI 直连不稳；用阿里源（清华源在容器内是 403，见 1.4） |

**实测结果**（`polyglot_python_two-bucket`，opencode 臂，`--install-only`）：

```
environment_setup    97.8s   # 首次构建题目镜像（含 deadsnakes PPA + python3.11）
environment_setup     6.0s   # 镜像已缓存
agent_setup         185.4s   # apt 装 nodejs+npm → nvm 装 node22 → npm i -g opencode-ai
exception            None    # 全通
```

对比没配这些的时候：`apt-get update` 单独就要 4 分钟，`agent_setup` 撞 360s 默认超时。

> **不要**把 `nodejs.org` 之外的东西随手加进 NO_PROXY 或反之。这里每个域名都是量过的：
> 直连快就直连，直连不通才走代理。代理只有 ~110 KB/s，是整条链路最窄的地方。

### 1.6 超时：默认 360s 装不完 agent

`agents/base.py:59` 的 `DEFAULT_SETUP_TIMEOUT_SEC = 360.0`。opencode 的安装（apt → nvm → npm）在慢网络下**远超**这个值，实测 `apt-get update` 单独就吃掉 4 分钟。

超时报 `AgentSetupTimeoutError`，而且 `result.json` 里 `agent_setup.finished_at` 会是 `started_at + 360s`，看不出真实进度。

用 `--agent-setup-timeout-multiplier <float>` 放大（乘在 360s 上）。**但正确的解法是把网络弄快**（国内镜像 + 只对 GitHub 用代理），而不是无限放大超时。

---

## 2. Harbor 0.24.0 实测事实（文档比这个版本超前，别信文档）

### 2.1 凭据

0.24.0 **没有** `--base-url` / `--api-key-name` / `--api-format`，也**没有** `harbor agent auth` 子命令（`harbor agent -h` 里查不到）。

provider 从 `-m provider/model` 的 slug 推出来，然后读**宿主环境变量**。openai 系就是 `OPENAI_API_KEY` + `OPENAI_BASE_URL`（或 `OPENAI_API_BASE`）。自定义网关能通，因为 opencode adapter 在 `agents/installed/opencode.py:546-553` 会把 `provider.openai.options.baseURL` 设成解析到的 base_url。

`-m` 必须写 `provider/model` 两段式，只写 model 名不行。

#### 但本网关不能用 `-m openai/...`

cherygpt 网关的实测能力（直接 POST 验证过）：

| 端点 | 结果 |
| --- | --- |
| `/chat/completions` | **200**，`qwen3.6-27b` 正常返回 |
| `/responses` | **400** `Unsupported model: 'qwen3.6-27b'` |
| `/models` | 404（没暴露） |

而 opencode 对 `openai` 这个 provider 走的是 **Responses API**，于是必然 400。
把 `provider.openai.npm` 改成 `@ai-sdk/openai-compatible` 也没用——opencode 内部
仍按 openai 的路径调 `.responses()`，报：

```
UnknownError: Q.responses is not a function. (In 'Q.responses(Y)', 'Q.responses' is undefined)
```

**解法：用 `openrouter` 这个 provider 槽位承载本网关。** opencode 对 openrouter
用的正是 `@ai-sdk/openai-compatible`（chat completions），而 harbor 的
`PROVIDERS["openrouter"]` 是 `ProviderAccess(("OPENROUTER_API_KEY",), ..., ("OPENROUTER_BASE_URL",))`，
凭据能正常透传进容器。

```yaml
agents:
  - name: opencode
    model_name: openrouter/qwen3.6-27b     # ← 不是 openai/
    kwargs:
      opencode_config:
        provider:
          openrouter:
            options:
              baseURL: "https://model.cherygpt.com/ada/v1"
              apiKey: "{env:OPENROUTER_API_KEY}"   # 占位符，不落明文
            models:
              qwen3.6-27b: { name: qwen3.6-27b }
        small_model: openrouter/qwen3.6-27b        # 否则它拿 gpt-5.4-nano-flex 生成标题 → 404
```

宿主侧导出 `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` 即可。

三个必须注意的点：

1. **`opencode_config` 一旦设置，harbor 就不再自动注入 baseURL**
   （`opencode.py::model_connection` 里 `if self._opencode_config: return without_inferred_base_url(access)`），
   必须自己在 overlay 里写 `options.baseURL`。
2. **`opencode_config` 是最后合并的，优先级最高**
   （`deep_merge(deep_merge(_DEFAULT_CONFIG, auto), opencode_config)`），所以能覆盖 harbor 自动生成的 provider 段。
3. **`apiKey` 用 `{env:VAR}` 占位符**，不要写死。harbor 是靠
   `echo '<整个 json>' > ~/.config/opencode/opencode.json` 落配置的，
   明文密钥会同时进容器磁盘和 `trial.log`。实测日志里只有 `"apiKey": "{env:OPENROUTER_API_KEY}"`。

另外：`--ak opencode_config={...}` 这种在命令行传 JSON 的写法**行不通**——
命令要经过 `sg docker -c "..."`，嵌套引号会被吃掉，harbor 收到的是残缺字符串，
报 `opencode_config: Input should be a valid dictionary`。统一走 `-c` 的 YAML。

#### opencode 臂实测通过

`polyglot_python_two-bucket`，完整跑含判分：

```
environment_setup    5.4s
agent_setup        122.2s
agent_execution     37.8s
verifier             2.8s
exception           None
verifier_result     {"rewards": {"reward": 1.0}}      ← 做对了
agent_result        n_input_tokens=53093  n_output_tokens=762  n_cache_tokens=0
                    model_usage={"openrouter/qwen3.6-27b": {...}}
```

### 2.2 常用 flag

```
-o, --jobs-dir <path>        结果目录（默认 jobs）
-n, --n-concurrent <int>     并发 trial 数（默认 4）
-k, --n-attempts <int>       每题重复次数（默认 1）
-a, --agent <name|module:Class>
-m, --model <provider/model> 可重复
    --ak, --agent-kwarg k=v  可重复
    --ae, --agent-env K=V    可重复，agent 执行时的 env
    --ek, --environment-kwarg k=v
-c, --config <yaml|json>     JobConfig，可重复，后者覆盖前者
-t, --task <org/name>        跑注册表里的单个 task
-d, --dataset <name@version>
-i, --include-task-name <glob>   从 dataset 里筛
-x, --exclude-task-name <glob>
-l, --n-tasks <int>          筛完之后再截断
    --artifact <容器内路径>   trial 结束后把文件捞出来，可重复
    --install-only           只装 agent 就退出（验环境用）
    --dry-run                只校验配置和 task 元数据，不下载不跑
    --print-config           打印解析后的 JobConfig JSON 就退出
    --force-build / --no-force-build
    --delete / --no-delete   跑完是否删容器（默认删）
    --extra-docker-compose <path>  额外 compose overlay，可重复
    --mounts <json array>
-y, --yes                    自动确认
```

完整表见 `harbor run --help`。

### 2.3 数据集 ID

| 写法 | 结果 |
| --- | --- |
| `aider_polyglot` | ❌ `Dataset aider_polyglot not found` |
| `aider-polyglot` | ✅ `Dry run OK — 225 trial(s)` |
| `terminal-bench@2.0` | ✅ `Dry run OK — 89 trial(s)` |

`harbor dataset list` 只打印一个网址，不列内容。task 缓存在 `~/.cache/harbor/tasks/packages/<org>/<name>/<sha256>/`。

### 2.4 aider-polyglot 实测清单

用 harbor 自己的 venv 直接调注册表客户端，**只取元数据、不下载任何 task 包**：

```bash
~/.local/share/uv/tools/harbor/bin/python - <<'EOF'
import asyncio
from harbor.registry.client.factory import RegistryClientFactory
async def main():
    c = RegistryClientFactory.create(registry_url=None, registry_path=None)
    md = await c.get_dataset_metadata("aider-polyglot")
    print(md.name, md.version, len(md.task_ids))
    for t in md.task_ids: print(t.get_name())
asyncio.run(main())
EOF
```

实测结果：

- `name=aider-polyglot`，`version=1.0`，**225 个 task**，`files=[]`、`metrics=[]`。
- 全部是 `GitTaskId`，指向 `https://github.com/laude-institute/harbor-datasets.git`，pin 在 commit `f30b14415dd733c83627204bad0af69a89ceb46f`，路径 `datasets/aider_polyglot/<task>`。
  → harbor 会在**宿主**上 clone 这个仓库（宿主有代理，能通），一次 clone 供全部 task 复用。
- 命名规则：`polyglot_<lang>_<exercise>`，约 100 个不同 exercise。
- 语言分布（**不均匀**，抽样要按语言分层）：

| 语言 | 题数 |
| --- | --- |
| javascript | 49 |
| java | 47 |
| go | 39 |
| python | 34 |
| rust | 30 |
| cpp | 26 |
| **合计** | **225** |

### 2.5 选题过滤规则

`models/job/config.py::_filter_task_ids()`：

1. `-i/--include-task-name` → `DatasetConfig.task_names`（**可重复**，实测传 3 个 pattern 得到 `Dry run OK — 3 trial(s)`），用 `fnmatch(task_id.get_name(), pattern)` 匹配；一个都不匹配时抛错并**回显前 5 个可用名**。
2. `-x/--exclude-task-name` 同理，取反。
3. `-l/--n-tasks` 在过滤**之后**做 `filtered_ids[:n_tasks]` 截断。

顺序保持注册表返回的顺序，所以**显式传 task 名就能完全控制选哪些、什么顺序**——嵌套抽样（6 题是 25 题的子集）靠这个实现，不依赖 `-l`。

### 2.6 数据集落地：不走 git，改用本地目录

注册表给出的 225 个 task 全是 `GitTaskId`，harbor 的下载流程（`tasks/client.py:233-340`）是：

```
git clone --filter=blob:none --depth 1 --no-checkout <url> <tmp>
git sparse-checkout set --no-cone --stdin     # 只取需要的 task 路径
git fetch --depth 1 origin <commit>
git checkout <commit>
```

**这条链在本机跑不通**：`git clone` 走宿主代理稳定报
`curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet` / `early EOF`，
直连则 `Failed to connect to github.com port 443`。加 `http.version=HTTP/1.1`、重试都救不回来。

绕法：**用 curl 拉 tarball，再让 harbor 吃本地目录**。curl 用 OpenSSL，比 git 的 gnutls 稳得多。

```bash
curl -sS --retry 3 -x http://127.0.0.1:7897 -o /tmp/hd.tar.gz \
  https://codeload.github.com/laude-institute/harbor-datasets/tar.gz/f30b14415dd733c83627204bad0af69a89ceb46f
# 实测 9.77 MB / 85s
mkdir -p ~/.mycode/harbor-datasets
tar -xzf /tmp/hd.tar.gz -C ~/.mycode/harbor-datasets --strip-components=1
```

然后 `-p` 指向本地数据集目录（`DatasetConfig.path`，走 `_get_local_task_configs()`，逐个 `iterdir()` 判 `TaskModel.is_valid_dir`）：

```bash
harbor run -p ~/.mycode/harbor-datasets/datasets/aider_polyglot \
           -i polyglot_python_two-bucket -a opencode -m openai/qwen3.6-27b --dry-run
# → Dry run OK — 1 trial(s)
```

`--print-config` 验证解析结果：

```json
"datasets": [{"path": ".../aider_polyglot", "task_names": ["polyglot_python_two-bucket"]}]
```

好处不只是绕开 git：内容 pin 死在那个 commit，**每次跑都是同一份题**，不受 GitHub 可用性和仓库后续变更影响。

### 2.7 aider-polyglot 的题目镜像

225 题的 `environment/Dockerfile` **全部** `FROM buildpack-deps:jammy`（Ubuntu 22.04），先装 `tmux asciinema curl`，再按语言装工具链：

| 语言 | 题数 | 额外安装 | 境外依赖 |
| --- | --- | --- | --- |
| javascript | 49 | nodesource `setup_20.x` + nodejs，全局 jest | `deb.nodesource.com` |
| java | 47 | `ca-certificates-java` + `openjdk-21-jdk` | apt |
| go | 39 | `golang.org/dl/go1.21.5.linux-$GOARCH.tar.gz` | **`golang.org`（直连不通）** |
| python | 34 | deadsnakes PPA + python3.11/venv/dev + pip3 | **`launchpad.net` PPA** |
| rust | 30 | `sh.rustup.rs` + build-essential/pkg-config/libssl-dev | **`rustup.rs` / `static.rust-lang.org`** |
| cpp | 26 | cmake + `libboost-all-dev` + libtbb-dev + build-essential | apt（体积大） |

`task.toml` 统一是：`cpus = 1`、`memory = 4G`、`storage = 10G`、agent 超时 1800s、verifier 超时 1800s、build 超时 1800s。判分入口是 `tests/test.sh`。

**含义**：每题的镜像都要**构建一次**（按 task checksum 缓存，两臂共用）。这是整个 benchmark 最大的一次性成本。

#### 但「构建走 dockerd 的代理」是错的（实测踩到）

1.2 那个 systemd drop-in 只作用于 **dockerd 自己拉镜像**，管不到 Dockerfile 里的 `RUN` 步骤。
所以构建期能不能出网，完全取决于目标域名在国内直连是否可达：

| 语言 | 构建期要访问 | 实测 |
| --- | --- | --- |
| cpp / java / javascript | apt（已换成 aliyun HTTPS 源）、`deb.nodesource.com` | 直连可达，构建通过 |
| python | `ppa.launchpadcontent.net`（deadsnakes） | 直连可达，构建通过（首次 97.8s） |
| rust | `sh.rustup.rs` / `static.rust-lang.org` | 直连可达，构建通过 |
| **go** | `golang.org/dl/go1.21.5.linux-amd64.tar.gz`（66 MB） | **直连不通 → `curl` 退出码 28（超时）→ 构建失败** |

注意这张表只讲**构建期**。verifier 跑起来还会再出一次网（java 题要用 Gradle wrapper
下发行版），那是**运行期**，走的是 1.5 注入的容器 env，跟构建期是两条路：
java 题就是构建期一切正常、运行期挂在「JVM 不读 `HTTPS_PROXY`」上（见 4 节）。

go 失败时长这样，trial 直接记 `RuntimeError`（不是 agent 的问题，`exception.txt` 里是
`Docker compose command failed ... build. Return code: 1`）：

```
#13 RUN ARCH=$(uname -m) && ... curl -L "https://golang.org/dl/go1.21.5.linux-$GOARCH.tar.gz" ...
failed to solve: process "/bin/sh -c ..." did not complete successfully: exit code: 28
Exception RuntimeError is not in include_exceptions, not retrying
```

**解法：runner 在调 harbor 之前先「预热」镜像**（`prewarm_images()`，`--no-prewarm` 可关）：

```bash
docker buildx build --output type=cacheonly \
  --build-arg HTTP_PROXY=http://<LAN IP>:7897 --build-arg HTTPS_PROXY=... \
  --build-arg http_proxy=... --build-arg https_proxy=... \
  --build-arg NO_PROXY=<国内镜像站列表> --build-arg no_proxy=... \
  <数据集>/<题目>/environment
```

三个点都是实测确认过的，不是照文档猜的：

1. **代理 build-arg 能让 `RUN` 出网**：`curl https://sh.rustup.rs/` → 200 / 1.03s，`https://golang.org/dl/` → 301 / 0.37s。
2. **预定义代理 arg 不进 BuildKit 的缓存键**：带 `--build-arg HTTPS_PROXY=...` 构建一次之后，
   再不带任何参数 `docker build` 同一个 Dockerfile，那一步直接 `CACHED`。
   所以 harbor 之后的 `docker compose build`（不带代理）能白捡这次构建的成果——
   这一点在真跑里验证过：预热 `polyglot_go_poker` 之后，opencode 臂那道题的容器正常起来了。
   > 只有 `HTTP_PROXY`/`HTTPS_PROXY`/`FTP_PROXY`/`NO_PROXY` 这几个**预定义** ARG 有这个待遇；
   > 自定义 ARG 会进缓存键，换了就得重建。
3. **`--output type=cacheonly` 只落缓存、不产镜像**：25 道题就是 25 个镜像（cpp 带 boost 好几个 GB），
   不留 tag 的话每个都变成悬空镜像，磁盘会被吃光。

`NO_PROXY` 里必须放国内镜像站：构建期的 apt / pip 走 aliyun、tuna 直连，
比挤那条 ~110 KB/s 的代理快 40 倍（1.5b 的表）。

#### 语言层是跨题共享的，所以「225 次构建」其实是「6 次」

225 道题的 Dockerfile 只有 `COPY workspace/ ./` 那一行按题不同，前面
`FROM buildpack-deps:jammy` → `apt install tmux asciinema curl` → 装语言工具链
这几层**逐字节相同**，BuildKit 按内容寻址，所以一层只构建一次。

实测：go 题的 66 MB 工具链下载只在 `polyglot_go_poker` 上发生过一次，
之后预热 `polyglot_go_wordy` / `polyglot_go_bowling` 各只花不到 1s；
把 final 的 25 道题全部预热一遍总共 **7.0s**（六类语言层此前都已缓存）。

**含义**：整个 benchmark 的一次性成本是 6 次语言层构建（其中只有 go 那次要挤代理，约 10 分钟），
不是 225 次。剩下的按题构建都只是 COPY 一层，秒级。

### 2.8 结果目录结构

```
<jobs-dir>/<YYYY-MM-DD__HH-MM-SS>/
├── job.log
├── config.json          # job_name / jobs_dir / n_concurrent_trials / agents[] / tasks[]
├── result.json          # job 级汇总
├── lock.json
└── <task>__<hash>/
    ├── config.json      # trial 级配置
    ├── trial.log
    ├── result.json      # trial 级结果（判分在这里）
    ├── exception.txt    # 只有失败时才有
    ├── lock.json
    ├── agent/setup/     # agent 安装日志
    ├── verifier/
    └── artifacts/manifest.json
```

trial 级 `result.json` 关键字段：`task_name`、`trial_name`、`task_id{org,name,ref}`、`task_checksum`、`agent_info{name,version,model_info{name,provider}}`、`agent_result`、`verifier_result`、`exception_info{exception_type,exception_message,exception_traceback,occurred_at}`、`started_at`/`finished_at`，以及分段耗时 `environment_setup{started_at,finished_at}` / `agent_setup{...}` / `agent_execution` / `verifier` / `step_results`。

job 级 `result.json`：`n_total_trials` + `stats{n_completed_trials, n_errored_trials, ..., evals{"<agent>__<model>__<dataset>": {n_trials, n_errors, metrics[{mean}], pass_at_k, reward_stats, exception_stats}}, n_input_tokens, n_cache_tokens, n_output_tokens, cost_usd}`。

**注意**：出异常的 trial 不计入 `n_trials`（上例是 `n_trials: 0, n_errors: 1`），`metrics[0].mean` 会是 `0.0`。统计时要区分「跑了没通过」和「根本没跑起来」。

### 2.8b 数据集自己的一个判分坑：cpp 构建失败时不写 reward 文件

cpp 题的 `tests/test.sh` 开头是 `set -Eeuo pipefail`，中间那段构建逻辑是这样：

```bash
if cmake .. && make; then
    find . -name "*test*" -executable -type f -exec {} \; || ... || echo "No test executables found"
else
    echo "CMake build failed"
    exit 1                      # ← 直接退出整个脚本
fi
...
exit_code=$?
if [ $exit_code -eq 0 ]; then echo 1 > /logs/verifier/reward.txt
else                          echo 0 > /logs/verifier/reward.txt; fi
```

**构建一失败就 `exit 1`，跳过了末尾写 reward.txt 的那一段**，harbor 于是抛
`RewardFileNotFoundError`（`verifier/verifier.py:262`），trial 记成异常。

而 C++ 里「构建不过」几乎总是因为 agent 给的 API 对不上测试文件（测试里写
`kindergarten_garden::Plants`，agent 没定义这个 enum，就是一片编译错误）——
**那是做错了，不是基础设施故障**，必须按 reward 0 计分。

为什么这条不能含糊：如果把这类 trial 当异常剔出分母，那么**做得差的那一臂反而会因为
「难题被剔除」而通过率虚高**，两臂的偏差方向还不一致（谁的 cpp 更容易编译不过，谁就被抬得更多）。

`collect_trials()` 因此把 `RewardFileNotFoundError` 映射成 `reward=0.0` 并打上
`no_reward_file` 标记，报告里显示成 `0.0（构建失败）`；`exception_type` 原样保留当证据。
判分口径改成**只认 reward**：`reward is None` 才是「没有可用判定」，其余一律计入分母。

> 顺带记一个**没**修的观察：上面那串 `find ... || find ... || ctest ... || echo "No test executables found"`
> 以 `echo` 收尾，理论上「构建通过但测试失败」也可能被吞成 `exit_code=0` → reward 1。
> 实测没遇到：cpp 题的错误解法基本都停在编译阶段，跑不到这一步。
> 已核对所有 reward=1 的 cpp trial，verifier 输出里都有 `All tests passed`。

### 2.8c 判分口径改了之后不用重跑

```bash
.venv/bin/python -m eval.harbor.runner --arm mycode --stage final \
    --run-dir ~/.mycode/harbor_jobs/<时间戳>
```

`--run-dir` 只把已有的 job 目录按**当前**判分口径重新出报告，不碰 docker、不需要凭据。
一轮 25 题两臂要跑一个多小时，改一次统计口径就重跑一次是不必要的。

### 2.9 容器网络与资源

- 默认 `NetworkPolicy.network_mode = PUBLIC`，容器有完整出网能力。
- 本机 5 CPU / 11 GB 可用，并发定 `-n 2`。
- hello-world 单题实测：总 5m1s，其中 `environment_setup`（镜像构建）占 **4m48s**，`agent_setup` 2s（那次是失败的）。镜像构建有缓存后第二次会快很多。

### 2.10 opencode 安装做了什么

`agents/installed/opencode.py::install()`：

1. `ensure_system_dependencies(("curl","bash","coreutils","nodejs","npm"))` → root 执行 `apt-get update && apt-get install -y curl bash coreutils nodejs npm`，env 只有 `DEBIAN_FRONTEND=noninteractive`。
2. musl 镜像直接用包管理的 node；否则跑 nvm 装 node 的片段（要访问 github）。
3. `npm i -g opencode-ai@latest && opencode --version`。

三步全都依赖出网，这就是 1.4 必须解决的原因。

### 2.11 自定义 agent 可用的 API（已从 0.24.0 源码核实）

```python
class BaseInstalledAgent:
    async def exec_as_agent(environment, command, env=None, cwd=None, timeout_sec=None)
    async def exec_as_root(environment, command, env=None, cwd=None, timeout_sec=None)
    async def ensure_system_dependencies(environment, deps)   # 只认 SYSTEM_PACKAGES 里的键
    async def _upload_config_text(environment, *, content, remote_path, filename)  # chmod 600
    def render_instruction(...)
    @with_prompt_template
```

```python
class BaseEnvironment:
    async def upload_file / upload_dir / download_file / download_dir / exec
```

- `ModelConnectionSpec(default_provider, api_key_envs, base_url_envs, passthrough)` → `ResolvedModelConnection(provider, api_key, base_url, configured_base_url, env)`。
- `AgentContext` 只有 token/成本字段：`n_input_tokens`、`n_cache_tokens`、`n_output_tokens`、`cost_usd`、`model_usage`、`metadata`。**没有** workdir / task_id，工作目录要自己 `pwd` 拿。

### 2.12 `enable_thinking: false` 是真的生效（**旧结论是错的，已推翻**）

> 本节原先写的是「网关会无视 `enable_thinking: false`，两臂都在思考，所以不含混淆变量」。
> **那是错的**，而且错得很贵：正因为信了它，首轮 25 题让 MyCode 臂全程不思考、
> opencode 臂全程思考，测出一个假的「MyCode 快 1.9×」。下面是重测结果。

同一个 prompt、同一个模型，各打 3 次，只改 `enable_thinking`。**两种传输方式都测了**
（MyCode 走 `stream=True`，非流式是另一种可能行为，必须分开验）：

流式（`stream=True`，即 MyCode 的真实路径）：

| 条件 | 思考 token | 思考字符（流里实收） | 可见字符 | completion | 墙钟 |
| --- | --- | --- | --- | --- | --- |
| 不传该参数 | 4375 | 16 468 | 1178 | 4687 | **70.5s** |
| `false` | **0** | **0** | 1968 | 513 | **7.4s** |
| `true` | 3495 | 13 092 | 1224 | 3831 | 56.0s |

非流式（`stream=False`）：

| 条件 | 思考 token | completion | 墙钟 |
| --- | --- | --- | --- |
| 不传该参数 | 3780 | 4125 | 66.7s |
| `false` | **0** | 548 | **7.8s** |
| `true` | 6136 | 6495 | 99.1s |

四条结论：

1. **`enable_thinking: false` 完全生效**，流式非流式都一样：思考 token 归 0、
   流里连一个 `reasoning_content` 字符都没有、墙钟快 **9.5×**、生成量少 7.5×，
   而可见输出反而更长（1968 vs 1178 字）。
2. 关思考时网关**整个 `completion_tokens_details` 字段都不返回**（不是返回 0）。
   所以「`reasoning_tokens` 是 null」和「思考开着但没思考」在数据上是同一个形状——
   `payload.thinking_misalignment` 这个哨兵就是为此存在的。
3. **两臂的默认行为不同，必须显式对齐**：MyCode 的 `agents/core/model_caller.py:319`
   在 `thinking is not None` 且模型名含 `qwen` 时发 `extra_body={"enable_thinking": ...}`；
   opencode 的配置（`runner.py` 的 `opencode_config`）里没有任何思考相关字段 → 走模型默认（开）。
   所以 runner 现在**不再继承**宿主端点配置的 `thinking`，而是由 `--thinking default|on|off`
   显式决定，默认 `default`（不注入 → 跟随模型默认 → 与 opencode 对齐）。
4. **输出 token 两臂口径不同**：网关的 `completion_tokens` **含** reasoning，
   而 opencode 把两者分开报（trajectory 的 `metrics.completion_tokens` 与
   `metrics.extra.reasoning_tokens`）。所以 opencode 的「真实生成量」要把两个加起来，
   MyCode 的 `n_output_tokens` 已经是合计值。现在 MyCode 侧也单列了
   `metadata.mycode_reasoning_tokens`，两边可以对齐比。

**与 benchmark 无关但更要紧的一条**：MyCode 的端点配置里 qwen3.6-27b 是 `thinking: false`，
也就是说**日常使用这个模型时它完全不思考**。这是 9.5× 的延迟差和未知的质量差，
不是"配置惰性"。这个默认值是否有意如此，值得单独确认。


---

## 3. 怎么跑

一次性准备（本机已做过；换机或重装按这个来）：

```bash
wsl
cd /mnt/d/Projects/MyCode
bash eval/harbor/setup_env.sh   # docker 代理 + 镜像站 + 基础镜像换源 + 数据集落地 + 自检
```

之后每次评测都只用 runner（WSL 里，仓库根目录）：

```bash
export PATH="$HOME/.local/bin:$PATH"        # harbor 是 uv tool 装的

# 先看选题和配置对不对（不起容器，几秒）
.venv/bin/python -m eval.harbor.runner --arm both --stage probe --dry-run

# 单题连通性：MyCode 臂第一次跑必做，验证容器内安装
.venv/bin/python -m eval.harbor.runner --arm mycode --tasks polyglot_python_two-bucket --n-concurrent 1

# 阶段 1：探测 6 题，两臂
.venv/bin/python -m eval.harbor.runner --arm both --stage probe

# 阶段 2：正式 25 题（探测那 6 题是它的前缀，两批可以合并统计）
.venv/bin/python -m eval.harbor.runner --arm both --stage final
```

runner 每次做的事：

| 步骤 | 说明 |
| --- | --- |
| 选题 | `select_tasks(225 个题名, n, seed=20261017)`，按语言均衡轮转；`--stage probe/final` = 6/25 题 |
| 凭据 | `resolve_model_config(model)` 从 `~/.my-code/config.json` 取 base_url + api_key，只经进程 env 交给 harbor |
| 容器 env | 探测宿主 LAN IP，拼出 1.5b 那张表里的代理 / 镜像配置 |
| **预热镜像** | 带代理 build-arg 把选中题目的镜像构建进 BuildKit 缓存（否则 go 题构建必失败，见 2.7）；`--no-prewarm` 可关，`--prewarm-jobs` 调并发 |
| 生成 job 配置 | 落在 `~/.mycode/harbor_configs/<时间戳>-<臂>-<阶段>.json` |
| 调 harbor | `harbor run -c <配置> -o ~/.mycode/harbor_jobs -n <并发> --max-retries 1 --retry-include NetworkConnectionError -y` |
| 收结果 | 摊平每个 trial 的 `result.json` → `eval/reports/harbor_<臂>_<阶段>_<时间戳>.json` + `.md` |

常用参数：`--arm mycode|opencode|both`、`--stage probe|final`、`--tasks a,b,c`（显式题目，给了就忽略 stage）、
`--model`（换模型；落地板时换 deepseek-v4-flash 就是改这个）、`--n-concurrent`（默认 2，本机 5 CPU / 11 GB）、
`--install-only`、`--dry-run`、`--host-ip`（覆盖自动探测）、`--no-report`。

### 两臂分别怎么接进去

**opencode**：harbor 自带 adapter，零开发。靠 `opencode_config` 把网关塞进 openrouter 这个 provider 槽位（见 2.1）。

**MyCode**：`eval/harbor/mycode_agent.py`，harbor 用 `-a eval.harbor.mycode_agent:MyCodeAgent` 加载
（runner 会把仓库根塞进 harbor 进程的 `PYTHONPATH`）。

```
install()  apt 装 python3-venv（cpp/java/go/rust/js 五类镜像缺它，venv 会建出一个没有 pip 的空壳）
           → 宿主页出 payload（162 文件 / 1.35 MB：agents/ + eval/common + eval/harbor + requirements.txt）
           → upload_dir 到 /opt/mycode → python3 -m venv → pip install -r requirements.txt
run()      pwd 拿工作目录 → 题目正文写成 /tmp/mycode_instruction.md
           → 容器内 .venv/bin/python -m eval.harbor.agent_main → 进程内调 run_agent_task
           → 结果 JSON download 回 trial 的 agent/ 目录 → 填 AgentContext
```

三个刻意的选择：

- **不走 MyCode 的 CLI**，和 LOCA 一样进程内调 `run_agent_task`：两臂的差异就只剩 agent 本身，
  不掺进 CLI 的参数解析和输出格式。
- **凭据走 per-exec env**（`MYCODE_API_KEY` / `MYCODE_BASE_URL`），既不写进 job 配置也不上命令行。
  命令行会被 harbor 原样印进 trial.log；env 里名字带 KEY 的值 harbor 会脱敏。
- **行为开关从宿主端点配置搬进容器**（`MYCODE_THINKING` / `MYCODE_THINKING_FEEDBACK` /
  `MYCODE_CONTEXT_WINDOW`，走 `agents[].env`，harbor 用 `scoped_exec_env` 叠进 agent 阶段的每一次 exec）。
  容器里没有 `~/.my-code/config.json`，不搬就按内置默认跑；今天默认值和 qwen3.6-27b 的端点配置
  恰好一致（thinking 的 `None` 和 `False` 都判成 disabled、窗口都是 1M），但那是巧合，换模型就不成立。

### 「没跑起来」和「跑了没做对」分开记

`payload.hard_failure()`：有 error 且一个字都没产出 → 适配器抛 `NonZeroAgentExitCodeError`，
trial 落 `exception.txt`，可按 `--max-retries` 重试；有 error 但已有产出（例如中途超时）→
保留这次结果交给 verifier 判分，error 记进 `agent_result.metadata.mycode_error`。
`summarize()` 算通过率时分母只含判过分的 trial，出异常的单独计数。

不这么做的话，网关抖动和「模型真不会做」在通过率里会变成同一件事。

### 两臂对比

```bash
.venv/bin/python -m eval.harbor.compare --stage probe     # 自动找两臂各自最新的那份报告
.venv/bin/python -m eval.harbor.compare --mycode eval/reports/a.json --opencode eval/reports/b.json
```

输出 `eval/reports/harbor_compare_<阶段>_<时间戳>.md` + `.json`：逐题配对、按语言分层、
token / 时长均值，以及 **McNemar 精确检验**的双侧 p 值。

用 McNemar 而不是两个独立比例的 z 检验，因为这是**配对设计**：同一批题、同一个模型、
同一套判分，差异只在 agent。配对数据里只有「两臂判定不一致」的题携带信息，
两臂都过或都没过的题不参与检验——所以 25 题里可能只有几题进入检验，p 值自然大，
**那是样本量的事实，不等于没差别**。报告里把 `only_mycode` / `only_opencode` 的题目名
列出来就是为了这个：先看是哪几道题不一样，再看 p 值。

出异常的题（`exception_type` 非空）既不算通过也不算失败，整对剔出配对统计并单独列出，
理由同 2.8：基础设施故障不能冒充模型能力差。

### 结果在哪看

- 每题的原始记录：`~/.mycode/harbor_jobs/<时间戳>/<题目>__<hash>/result.json`
  （同目录还有 `trial.log`、`agent/mycode_result.json`、`agent/mycode_stdout.txt`）
- 汇总：`eval/reports/harbor_<臂>_<阶段>_<时间戳>.md`（人看）+ `.json`（算统计用）
- 对比：`eval/reports/harbor_compare_<阶段>_<时间戳>.md`
- harbor 自己的视图：`harbor view ~/.mycode/harbor_jobs`

---

## 4. 已知限制

- **java 题曾经全军覆没，已修好（修法在 runner 里，不改数据集）**：症状是 verifier 用
  Gradle wrapper 下 `https://services.gradle.org/distributions/gradle-8.7-bin.zip` 时报
  `PKIX path building failed: unable to find valid certification path`，4/4 题两臂都是 0。
  已排除的假设：`docker run` 不给任何代理 env 也复现（**不是代理配错**）；
  同一个镜像里 `curl -I` 和 `openssl s_client -CAfile /etc/ssl/certs/ca-certificates.crt`
  对同一域名都是 `Verify return code: 0 (ok)`（**不是 CA 缺失，也不是网络不通**）；
  `/etc/ssl/certs/java/cacerts` 有 121 条、含 `gts_root_r4.pem`，而链子正是
  `gradle.org ← GTS WE1 ← GTS Root R4`（**不是 keystore 空的**）。
  **真因**：JVM 根本不读 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，它只认
  `-Dhttps.proxyHost` 这类系统属性。所以别的语言的构建都走通了代理，只有 JVM 在裸连，
  而裸连出去撞上的正是那条验不过证书的路径（系统信任库能验、JVM 验不过这一点仍未查到底，
  但既然走代理就通，就没必要再挖）。
  **修法**：runner 的 `container_env` 额外注入
  `JAVA_TOOL_OPTIONS=-Dhttps.proxyHost=<LAN IP> -Dhttps.proxyPort=7897 -Dhttp.proxyHost=… -Dhttp.nonProxyHosts=…`
  （`JAVA_NON_PROXY_HOSTS` 用竖线分隔、带 `*.` 前缀，语法跟 `NO_PROXY` 不一样）。
  验证：容器内 `./gradlew --version` → Gradle 8.7；补跑后两臂 java 各 2/4、`tls=0`。
  这是**宿主侧环境变量**，两臂同一份，不改题目内容，所以不算数据集偏差。
  报告里仍同时给「25 题原始」和「剔除 java 后 21 题」两组数，方便和修好之前的批次对齐。
- LAN IP 由 DHCP 分配，变了要重探测；Clash 必须开着 Allow LAN。
- 容器 DNS 是 `8.8.8.8` 兜底，境外域名直连不可靠，一律走代理。
- **镜像构建期没有代理**（1.2 的 drop-in 只管 dockerd 拉镜像），所以 go 题必须靠预热才构建得出来
  （2.7）。预热只是把同一次构建提前做，不改题目内容，两臂共用同一份缓存。
- harbor 文档（在线版）描述的是比 0.24.0 更新的接口，凡是本文和文档冲突，以本文的源码核实结果为准。
- **能比和不能比的指标**（细见 2.12；225 题基线上的实测数字见 **6.4**）：
  - 可比：通过率 / reward、`n_input_tokens`（两边都是累计 prompt token，含 cache）。
  - **墙钟时长原则上可比、但那一次不可比**：`agent_setup` 直接吃 CPU/IO，
    所以跑批时机器上还有别的负载（例如 SDD 子智能体）就会把它拉长，
    而两臂承受的争用程度不同。实测同一条跑批 5.5 → 8.2 → 12.9 分钟/题。
    要用这一格，必须两臂都在空机器上跑（6.4 第 1 条）。
  - **不可比**：`n_output_tokens`（网关的 `completion_tokens` 含 reasoning，opencode 把 reasoning 单列，
    且 harbor 聚合时把 opencode 的 reasoning 丢了 → 只能比「可见 completion」，见 6.4 第 2 条）、
    `n_cache_tokens`（两臂都恒为 0，网关不报 `cached_tokens`）、`cost_usd`（两臂都是 null）。
    所以「省了多少钱」这个说法在本 benchmark 里**给不出证据**，只能说「少用了多少输入 token」。
- MyCode 臂的 `thinking` / `context_window` 由 runner 从宿主端点配置搬进容器（见 3 节）。
  容器里没有 `~/.my-code/config.json`，忘了搬就按内置默认跑，而且**指标上看不出来**。
  （对 qwen3.6-27b 而言 `thinking` 恰好是惰性的，见 2.12；换个认这个参数的模型就不是了。）
- 每个 trial 都要重装一遍 agent（harbor 的设计，两臂一样）。25 题实测均值：
  MyCode 58.9s（27.9–123.7）、opencode 124.0s（54.1–193.1）。
  这段时间不计入 `agent_execution`，但计入总墙钟，而且**是 harness 开销、不是 agent 优劣**
  ——MyCode 这头短，只是因为 pip 装 1.35 MB 的本地依赖比 npm 装 opencode 快。
- **一处没修的不对称**：opencode 的配置里 `small_model` 也被设成同一个模型，它每开一个
  session 会额外打一次模型去生成会话标题，MyCode 没有这一步。方向上是**给 opencode 加负担**
  （多一次调用、多一点 token 和墙钟），所以不会让 MyCode 的结论变好看；但严格说两臂的
  「每题模型调用次数」并不完全对齐，token 差里含这一项。
- **上下文压缩在这次评测里一次都没触发**：25 题的 `mycode_compression_events` 累计全是 0
  （`tool_folded` / `session_folded` / `events_hidden`）。qwen3.6-27b 的窗口是 1 M，
  单题最多也就 31.5 万输入 token，压根够不着阈值。
  **所以这套 benchmark 测不出压缩带来的收益**，要测得换小窗口或换更长的题。

---

## 5. 实测结果（2026-10-17，最终数据）

> **看结论请直接跳第 6 节**（2026-10-22，aider-polyglot **全量 225 题**两臂基线）。
> 本节是 25 题那一轮：5.7 是它的结论，5.1–5.6 是过程记录，包含两次自我推翻
> （5.3 的 token、5.6 的速度）。**5.7 的头条数字（13/20 vs 8/20，p = 0.0625）已被第 6 节推翻**
> ——题量提到 225 之后差异消失（p = 1.0）。留着是因为「怎么错的」比「错的那个数」有用，
> 而且 5.7 末尾对 token 口径的那段保留意见，在 225 题上被逐字证实了。

### 5.1 跑了什么

- 数据集：aider-polyglot 本地副本，**随机 25 题**（`seed=20261017`，按语言分层抽样，
  cpp/go/java 各 4、python/rust 各 4、javascript 5）。
- 两臂、**同一个模型 `qwen3.6-27b`**、同一份网关凭据、同一批题目镜像、每题 `k=1`。
  - MyCode：`eval.harbor.mycode_agent:MyCodeAgent`，容器内进程直调 `run_agent_task`（不走 CLI）。
  - opencode：harbor 内置 adapter。
- 25 题**全部判分成功**（两臂各 0 个 unscored）。其中 cpp 有 2 题（MyCode）/ 3 题（opencode）
  是「verifier 没写出 reward 文件」（构建不过），按 0 分计入（口径见 2.8b）。
- 报告：`eval/reports/harbor_{mycode,opencode}_final_20261017__232016.{md,json}`、
  对比 `harbor_compare_final_20261017__232041.md`、剔 java 版 `harbor_compare_final-nojava_…`。

复现（把中间补跑的批次一起合并，后写的目录覆盖同题）：

```bash
.venv/bin/python -m eval.harbor.runner --arm mycode --stage final \
  --run-dir ~/.mycode/harbor_jobs/2026-10-17__03-33-18 \
  --run-dir ~/.mycode/harbor_jobs/2026-10-17__05-01-23
.venv/bin/python -m eval.harbor.runner --arm opencode --stage final \
  --run-dir ~/.mycode/harbor_jobs/2026-10-17__04-03-44 \
  --run-dir ~/.mycode/harbor_jobs/2026-10-17__05-08-51 \
  --run-dir ~/.mycode/harbor_jobs/2026-10-17__05-18-34
.venv/bin/python -m eval.harbor.compare --stage final
.venv/bin/python -m eval.harbor.compare --stage final --exclude-language java
```

### 5.2 结论一句话

> ⚠️ 本节是**变量没对齐的那一轮**（MyCode 思考关、opencode 思考开）。
> 对齐后重跑的结论在 **5.7**：通过率 13/20 vs 8/20（剔 java）、不一致对 5:0、p = 0.0625，
> 速度差从 1.86× 收窄到 1.17×。下面的数字保留只为记录当时测到了什么。

**通过率上两臂打平，token 上没差别。**
（原来这里还写着「速度上 MyCode 快约 1.9 倍」——**已作废，见 5.6**：
那一轮 MyCode 臂完全不思考、opencode 臂全程思考，速度差是思考开关的差。）

⚠️ 下表「平均 agent 执行」「单题总墙钟」两行因此**不可用作 scaffold 效率的证据**，
保留只是为了记录当时测到了什么。

| 指标 | MyCode | opencode | 差 |
| --- | --- | --- | --- |
| 通过率 | **12/25（0.48）** | **11/25（0.44）** | +1 题，McNemar 精确双侧 **p = 1.0** |
| 不一致对 | 只有 MyCode 过 4 题 | 只有 opencode 过 3 题 | 4:3 |
| 平均 agent 执行 | 52.9s（中位 43.1） | 98.4s（中位 84.3） | ⚠️ 1.86×，**变量没对齐，见 5.6** |
| 平均输入 token | 98 879.5（中位 81 915） | 92 291.2（中位 80 093） | +7.1%，14/25 题 MyCode 更低 |
| 平均输出 token | 1 768.6 | 1 675.0 | 不可比（见 4 节） |
| 平均装 agent | 58.9s | 124.0s | harness 开销，不算 agent 优劣 |
| 单题总墙钟 | 141.0s | 249.0s | ⚠️ 含上面那两行同样的问题 |

剔除 java 后（21 题，java 曾因 JVM 不读代理 env 而恒 0，现已修好，见 4 节）：
**10/21（0.476）vs 9/21（0.429），p = 1.0**；输入 97 043 vs 90 764。
方向和 25 题一致。（执行时长 49.5s vs 99.2s 同样受 5.6 的变量污染，不可用。）

按语言（MyCode 过 / opencode 过）：cpp 2/1、go **0/2**、java 2/2、javascript 4/3、python 1/1、rust 3/2。
go 是 MyCode 唯一明显落后的语言（4 题全挂，opencode 过 2 题），但 4 题的样本量支撑不了任何结论。

**「只有 MyCode 过」那 4 题里有 2 题是 opencode 的 C++ 没编译过**，得说清楚：
`cpp_circular-buffer` 和 `cpp_robot-name` 在 opencode 臂是 `RewardFileNotFoundError`（构建失败 → 记 0），
在 MyCode 臂是 reward 1.0。「交出来的代码编译不过」本来就是该判失败，所以这 2 分算数；
但它们**不是**「MyCode 写得更对」的证据，只是「MyCode 那次写的能编译」。
另外 `cpp_kindergarten-garden` **两臂都构建失败**，这题的 0 分可能不完全是 agent 的错
（也可能是该题的 CMake 配置本身有问题），不过两臂同向、不产生不一致对，不影响配对结论。

### 5.3 探测批（6 题）的「省 29% token」在 25 题上**没站住**

这是本轮最重要的一次自我推翻，记下来防止以后又拿小样本说事：

| 批次 | 配对题数 | MyCode 平均输入 | opencode 平均输入 | MyCode 相对 |
| --- | --- | --- | --- | --- |
| 探测（probe） | 5（go_poker 未判分） | 65 224.4 | 91 648.8 | **−28.8%** |
| 正式（final） | 25 | 98 879.5 | 92 291.2 | **+7.1%** |

探测那 5 题为什么显得省，已经不重要了——**扩到 25 题后符号直接翻了**。
中位数（81 915 vs 80 093）和「14/25 题 MyCode 更低」都说明这是持平，
不是某一题把均值拉爆——**就是没差别**。
唯一从探测批活到正式批的结论是**墙钟**：探测 37.0s vs 98.7s（2.7×），正式 52.9s vs 98.4s（1.9×）。

### 5.4 单题结果是噪声，只有总体通过率有意义（实测）

同一份代码、同一批题、同一个模型，MyCode 连跑两遍（run `02-51-21` 与 `03-33-18`，
中间只改了容器安装相关的两处——launchpad 加进 `NO_PROXY`、python 题跳过 apt——都不影响解题）：

- 24 个共同可判分题里 **21 题结果一致（0.88）**，**3 题翻面**：
  `go_wordy`、`javascript_bottle-song`、`javascript_ledger`。
- 两遍的总通过数是 **11 vs 10**。

也就是说 `k=1` 时的自噪声就有 ±3 题量级，而两臂最终只差 1 题。
**所以「MyCode 12/25 比 opencode 11/25 高」不能读成 MyCode 更强**，p = 1.0 就是这个意思。
同理，任何单题的 pass/fail 都不能拿来定位问题，要看就得 `k≥3` 取多数。

顺带一个合并正确性的旁证：java 补跑前后对比，25 题里只有 `java_tree-building`、
`java_two-bucket` 两题翻面（0→1），其余 23 题完全一致 ⇒ `--run-dir` 合并没引入别的漂移。

### 5.5 这个数能写进简历、不能写进简历的部分

- **能写**：搭了一套两臂配对对比的 coding-agent benchmark（Harbor + aider-polyglot，
  容器内进程直调、不走 CLI），25 题同模型对比，通过率打平（p = 1.0）；
  用「同代码连跑两遍」量出了 k=1 的自噪声（0.88 一致率），据此拒绝把 1 题的差当结论；
  并且**自查出了自己首轮的一个变量污染**（两臂思考开关不一致）、定位到根因、
  作废了错误结论、补上了哨兵和变量落盘（5.6）。
  这条比任何好看的数字都更能说明评测能力。
- **不能写**：「快 1.9×」。变量没对齐，见 5.6。
- **不能写**：「省 token / 省钱」。token 持平，而且 4 节说了 `cost_usd` 两臂都是 null、
  `n_cache_tokens` 两臂都是 0，**这个 benchmark 根本给不出成本证据**。
- **不能写**：「MyCode 通过率更高」。p = 1.0。
- **样本量的诚实边界（算出来的，不是拍的）**：n=25 配对、精确 McNemar 双侧 α=0.05 时，
  **能通过检验的最小通过率差是 24 个百分点**（不一致对 6:0，p=0.031；5:0 只有 p=0.0625）。
  也就是说这套 25 题的设计**天生只能查出「大幅退化」或「大幅提升」**，
  5～10 个百分点级别的差异它一律报 p=1.0。
  当初定 25 题是为了省时间，代价就是**最好情况也只能得出「打平」**——
  这本身是个有效结论（说明没退化），但不是「更好」。
  要证 5 个百分点的差，得上百题 × k≥3。

### 5.6 归因：那 1.9× 的速度差**不是 scaffold 的功劳**（结论已作废）

5.2 表里「平均 agent 执行 52.9s vs 98.4s，快 1.86×」这一行**不能再用**。
逐题拆完发现两臂的调用结构几乎一样，差的全是「模型每次生成多少 token」：

| 指标（25 题合计） | MyCode | opencode | 比值 |
| --- | --- | --- | --- |
| 模型调用次数 | 222 | 235 | 1.06× |
| 每次调用的上下文 | 8 968 | 9 495 | 1.06× |
| 每次调用生成的 token | 199 | **392** | **1.97×** |
| 每生成 1k token 的墙钟 | 29.9s | 26.7s | **0.89×** |
| 墙钟合计 | 1 324s | 2 460s | 1.86× |

关键在第四行：**两臂的生成吞吐是一样的**（opencode 甚至略快）。
所以速度差 100% 来自「opencode 每次让模型多生成一倍的 token」，
而其中 **54.6% 是看不见的思考**（opencode 25 题合计：可见 41 875 + 思考 50 279）。
逐题也一致：21/25 题 opencode 生成更多，比值中位 1.98。

再往下追一层就是 2.12 的那个变量：**MyCode 臂发了 `enable_thinking: false`（完全不思考），
opencode 臂没发（全程思考）**。两臂的可见输出量其实差不多（199 vs 178 token/次），
差的纯粹是 opencode 的思考部分。

所以这一轮真正成立的结论只有一条，而且方向和原来相反：

> **MyCode 在完全不思考的情况下，通过率与全程思考的 opencode 打平（12/25 vs 11/25，p = 1.0）。**

这句话比「快 1.9×」有意思得多，但它同样受 5.4 的自噪声限制（±3 题），
所以严格说只能讲成「没测出差别」，不能讲成「MyCode 不思考也一样强」。

**修好的三处，防止再犯：**

1. runner 不再继承宿主端点配置的 `thinking`，改由 `--thinking default|on|off` 显式决定，
   默认 `default` = 不注入 = 跟随模型默认 = 与 opencode 对齐（`mycode_settings_env`）。
2. 每轮报告写入 `summary.variables`（模型、seed、思考开关的**实际生效值**、
   注入的全部 env、`git rev-parse HEAD` 带 `-dirty` 后缀）。
   没有它，两次运行没法当基线对比，结论也没法复核——这次就是报告里没写「思考是关的」，
   假结论才一路活到了汇报环节。
3. MyCode 现在把思考 token 单列出来（`Agent.total_reasoning_tokens` →
   `result["tokens"]["reasoning"]` → trial 的 `metadata.mycode_reasoning_tokens`），
   并且 `payload.thinking_misalignment` 会在「以为思考开着、实际一个思考 token 都没有」时
   直接往 stderr 打警告。

### 5.7 变量对齐后重跑 MyCode 臂（2026-10-18，~~结论以这一节为准~~ → **已被第 6 节推翻**）

> ⚠️ 本节的通过率结论（15/24 vs 10/24、13/20 vs 8/20、p = 0.0625、不一致对 5:0）
> 是 **n = 25** 的结果，2026-10-22 跑满 225 题后差异**完全消失**（113/217 vs 112/217，p = 1.0，
> 不一致对 24:23）。本节自己也写了「差一次翻面就到 0.05」——那正是小样本的样子，
> 而 5.5 的可检测下限早就说过 n = 20 只能检出 25pp 的差。**看结论去第 6 节。**
> 本节仍然有效的部分：5.6 的速度归因（1.9× 几乎全是思考开关的差）、
> 思考开关的自我消融（同一臂自己跟自己比，净 +3 题、代价生成量 1.9×）、
> 以及末尾那段 token 口径不对等的保留意见（第 6 节用 225 题证实了它）。

5.6 修完之后只重跑了 MyCode 臂——opencode 那一轮本来就是全程思考，是合格的对照，不用重跑：

```bash
.venv/bin/python -m eval.harbor.runner --arm mycode --stage final   # --thinking 默认 default
.venv/bin/python -m eval.harbor.compare --stage final --exclude-language java
```

- 报告 `harbor_mycode_final_20261018__025351.{json,md}`，job 目录
  `~/.mycode/harbor_jobs/2026-10-18__02-53-52`，总墙钟 45m15s，`git_revision: ab6b631-dirty`。
- `summary.variables.thinking_mode = "default"`（不注入 `MYCODE_THINKING`，跟随网关默认）。
  思考确实开着：每题思考 token 均值 **1 712**，占该臂输出 token 的 **50.6%**。
- 25 题里 24 题判分成功；`polyglot_go_scale-generator` 是 `NonZeroAgentExitCodeError`
  （未判分，不进分母）。

**对齐前 vs 对齐后**（同一批题、同一份 opencode 报告，只有 MyCode 臂变了）：

| 指标 | 对齐前（MyCode 不思考） | 对齐后（两臂都思考） |
| --- | --- | --- |
| 通过率（全部语言） | 12/25 vs 11/25，p = 1.0 | **15/24 vs 10/24，p = 0.0625** |
| 通过率（剔 java） | 10/21 vs 9/21，p = 1.0 | **13/20 vs 8/20，p = 0.0625** |
| 不一致对 | 4:3 | **5:0**（全在 MyCode 一侧） |
| 平均 agent 执行 | 52.9s vs 98.4s（1.86×，已作废） | **82.5s vs 96.1s（1.17×）** |
| 平均输入 token | 98 880 vs 92 291 | 88 613 vs 87 286（1.02×） |
| 平均输出 token | 1 769 vs 1 675 | 3 384 vs 1 561（口径不对等，见本节末） |

这一次重跑同时证实了两件事：

1. **5.6 的归因是对的。** 把思考打开，MyCode 单题执行时长从 52.9s 涨到 82.5s，
   与 opencode 的差距从 1.86× 收窄到 **1.17×**。原来那 1.9× 几乎全是思考开关的差，
   剩下的一小截才是「MyCode 每题少喂约 8% 上下文」。
2. **通过率的方向变了。** 不一致对从 4:3 变成 5:0，p 从 1.0 降到 0.0625。

只有 MyCode 过的 5 题：`cpp_circular-buffer`、`cpp_robot-name`、`go_bowling`、
`javascript_bottle-song`、`javascript_ledger`。
按语言（MyCode 过 / opencode 过，剔 java 后 20 题）：cpp 3/1、go 2/1、javascript 5/3、
python 1/1、rust 2/2 —— 5.2 里「go 是 MyCode 唯一明显落后的语言」在这一版**不再成立**（2/1）。

**但这 5 题不能全算数**，两条折扣必须一起讲：

- `javascript_bottle-song` 和 `javascript_ledger` 正是 5.4 自噪声实测里**会翻面**的题
  （同一份代码连跑两遍，24 题里 3 题判定不一致，就是这两题加 `go_wordy`）。
  剔掉这 2 题只剩 **3:0 → p = 0.25**。
- MyCode 那 1 题未判分（`go_scale-generator`）opencode 是过的。
  最保守口径把它算成 MyCode 失败，就是 **5:1 → p = 0.23**。

所以能写下来的只有这一句：

> **变量对齐后，在 25 题 k=1 的口径下没测出 opencode 更强；MyCode 侧多过 5 题
> （其中 3 题在重复跑里稳定），McNemar 精确双侧 p = 0.0625，差一次翻面就到 0.05。**

不能写「MyCode 通过率显著高于 opencode」：n = 20 的配对设计下 5:0 就是 p = 0.0625，
6:0 才到 0.031（可检测下限见 5.5），而这 5 题里有 2 题本身不稳。
要把它做成能上简历的结论，得把 k 提到 3 或把题量提到 50 以上。

**顺带白拿的一个消融**（思考开关对 MyCode 自己的影响：同一批题、同一份代码，只差 `--thinking`）：

| | 思考关（`20261017__232016`） | 思考开（`20261018__025351`） |
| --- | --- | --- |
| 通过 | 12/25 | 15/24 |
| 多过的题 | — | `cpp_yacht`、`go_bowling`、`go_wordy`、`javascript_ledger` |
| 少过的题 | — | `rust_dot-dsl` |
| 平均输出 token/题 | 1 769 | 3 384（其中思考 1 712，占 50.6%） |
| 平均 agent 执行 | 52.9s | 82.5s |

净 +3 题，代价是生成量 1.9×、单题时长 1.56×。这条比两臂对比更硬——
它是**同一个臂自己跟自己比**，没有跨 scaffold 的口径问题。

**token 口径不对等（这一版的已知局限；225 题基线已在 6.4 第 2 条查到底并给了实测数字）**：
MyCode 的 `n_output_tokens` 取自网关的
`completion_tokens`，**含**思考（同一份数据里 `mycode_reasoning_tokens` 是它的子集）；
opencode 臂的 `agent_result.metadata` 是 `null`，harbor 没留下它的思考量，
无法确认它的 `n_output_tokens` 含不含思考。所以「MyCode 输出是 opencode 的 2.17×」**不是结论**。
能说的只是：MyCode 含思考的总生成 3 384/题，扣掉思考后的可见部分 1 672/题，
与 opencode 报出的 1 561/题同量级——这和 5.6 里「两臂可见输出差不多（199 vs 178 token/次）」一致。
要补上这一格，得让 opencode 臂也落一份思考量：harbor 的 opencode adapter 不给，
只能从容器内 opencode 自己的 session 存储里捞。

---

## 6. 全量 225 题两臂基线（2026-10-22，**结论以这一节为准**）

### 6.1 跑了什么

aider-polyglot **全部 225 题**（6 门语言：cpp 26、go 38、java 47、javascript 49、python 28、rust 29），
两臂、同一个模型 `qwen3.6-27b`、同一份网关凭据、同一批题目镜像、`seed=20261017`、每题 `k=1`。

| 控制变量 | 两臂取值 |
| --- | --- |
| model / provider | `qwen3.6-27b` / openrouter |
| `--thinking` | `default`（不注入 `MYCODE_THINKING`，跟随网关默认；两臂都真在思考） |
| seed / stage | `20261017` / `full` |
| `--n-concurrent` | 2 |
| `--agent-setup-timeout-multiplier` | 3.0（逐份 `result.json` 的 config 核过，225/225 与 178/178 全是 3.0） |

- MyCode 臂：一次跑完，job 目录 `~/.mycode/harbor_jobs/2026-10-18__22-43-17`，
  225 个 trial 全有 `result.json`，**223 题判分成功**（117 过），2 题未判分
  （1 个 `AgentSetupTimeoutError` @2700s、1 个 `VerifierTimeoutError` @1800s）。
- opencode 臂：被 Windows/WSL 重启打断**四次**，跨 **9 个 job 目录**跑完
  （`07-18-46`→`13-33-43`→`21-36-26`→`20-52-49`→`21-44-30`→`03-01-10`
  →`06-29-01`→`08-13-51`→`16-39-41`），**219 题判分成功**（112 过），6 题未判分。

报告与对比（都在 `eval/reports/`）：

```
harbor_mycode_full_20261021__080949.{json,md}      # MyCode 臂，当前判分口径重出
harbor_opencode_full_20261022__030529.{json,md}    # opencode 臂，九个目录合并重出
harbor_compare_full_20261022__034107.{json,md}     # 217 题配对
harbor_compare_full-nojava_20261022__034107.{json,md}
```

复现（**不用重跑**，判分口径变了就照 2.8c 重出报告）：

```bash
J=~/.mycode/harbor_jobs
.venv/bin/python -m eval.harbor.runner --arm mycode --stage full \
  --model qwen3.6-27b --seed 20261017 --run-dir $J/2026-10-18__22-43-17
.venv/bin/python -m eval.harbor.runner --arm opencode --stage full \
  --model qwen3.6-27b --seed 20261017 \
  --run-dir $J/2026-10-19__07-18-46  --run-dir $J/2026-10-19__13-33-43 \
  --run-dir $J/2026-10-19__21-36-26  --run-dir $J/2026-10-20__20-52-49 \
  --run-dir $J/2026-10-20__21-44-30  --run-dir $J/2026-10-21__03-01-10 \
  --run-dir $J/2026-10-21__06-29-01  --run-dir $J/2026-10-21__08-13-51 \
  --run-dir $J/2026-10-21__16-39-41
.venv/bin/python -m eval.harbor.compare --stage full
.venv/bin/python -m eval.harbor.compare --stage full --exclude-language java
```

（`--run-dir` 是**后给的为准**，所以目录必须按时间顺序列。合并规则见 6.5 那条修复：
有判分的不会被没判分的盖掉。）

### 6.2 结论一句话

> **同模型、变量对齐、225 题 k=1 的口径下，MyCode 与 opencode 的通过率统计上无法区分。**
> 217 道两臂都有判分的题上 113 vs 112（52.1% vs 51.6%），McNemar 精确双侧 **p = 1.0**，
> 不一致对 **24 : 23**；剔掉 java 后 170 题 **101 : 101 逐题全平**，p 同样 1.0。

三种口径都摆出来，因为它们指向同一个结论：

| 口径 | MyCode | opencode | p |
| --- | --- | --- | --- |
| 两臂都有判分（**217** 题，主口径） | 113 = **52.1%** | 112 = **51.6%** | **1.0** |
| 剔掉 java（170 题） | 101 = 59.4% | 101 = 59.4% | 1.0 |
| 未判分一律算失败（225 题） | 117 = 52.0% | 112 = 49.8% | — |

第三个口径那 2.2pp 的差**全部来自 opencode 臂多出的 4 题未判分**（6 vs 2），
是 agent 装不进容器的基础设施差，不是做题能力差；而 n = 225 的配对设计要检出 2.2pp
远超其能力（可检测下限见 5.5：n = 225 约 5.8–7.6pp），所以它不构成结论。

按语言（MyCode 过 / opencode 过）：cpp **13:11**、go **19:19**、java **12:11**、
javascript **36:38**、python **12:12**、rust **21:21** —— 六门全在 ±2 以内，
没有任何一门出现 5.2 里那种「go 是 MyCode 唯一明显落后的语言」的形状。

**5.7 的头条到此作废**：n = 25 时的 13/20 vs 8/20、不一致对 5:0、p = 0.0625，
在 n = 225 上变成 24:23、p = 1.0。5.7 自己写过「差一次翻面就到 0.05」，
那正是小样本的样子——**这一次是 5.4「单题结果是噪声，只有总体通过率有意义」
和 5.5 的可检测下限一起被证实**：n = 20 只能检出 25pp 的差，测出 5:0 说明不了什么。

### 6.3 归因：不一致的 47 题，两臂的失败形状是**对称**的

对 24 题「只有 MyCode 过」与 23 题「只有 opencode 过」，逐题读失败臂的
`verifier/test-stdout.txt` 尾 6 行分类（`.scratch/attrib-225.py`，
分类规则做过阳性对照：三段造出来的编译失败/断言失败/空跑文本都正确触发，
真数据命中的是 `FAILED aider_polyglot_test` + `Exit code: 1/101` 那一行）：

| 失败臂的失败形状 | opencode 失败（24 题） | MyCode 失败（23 题） |
| --- | --- | --- |
| 跑起来了但测试断言失败 | **19** | **20** |
| 编译/构建不过（`CMake build failed`） | 5 | 3 |
| 测试根本没跑到 / 其他 | 0 | 0 |

**这个对称性是本次基线最有用的一条结论**：n = 48 的中期数据曾让人以为
「opencode 特有不擅长满足测试文件写死的接口契约」（当时 7 个 MyCode 独占通过里
6 个是编译不过），跑满之后**这个类差不存在了**——编译不过是 5 : 3，
两臂都会犯，只是犯在不同的题上。剩下 89 题两臂都过、81 题两臂都没过，
配对的 217 题里有 **78% 是同向的**。

诚实边界：分类只到「测试跑没跑起来 / 构建过没过」这一层，**没有**再往下分
「规则理解错 / 边界条件漏 / 接口签名错」。要做到那一层得逐题读 47 份 diff，
本轮没做。

### 6.4 三个「看着可比、其实不可比」的指标（都已实测确认）

这一节是本次基线最该被记住的部分：**三个指标都印在 compare 生成的表格里，
其中两个直接照抄会得出错误结论。**

1. **`duration_s`（96.5s vs 120.2s）——弃用。** 两臂都在 SDD 子智能体活跃期间跑的，
   且承受的争用程度不同。同一条跑批、同样 `n_concurrent=2`，实测：
   没有子智能体时 **5.5 分钟/题**，评审在跑时 **8.2**，实现者+复审同时在跑时 **12.9**。
   瓶颈是 `agent_setup`（每题在全新容器里重装依赖，均值 202.8s、最长 4688s），
   它直接吃 CPU/IO。**通过率不受影响**（判分是 verifier 跑测试，确定性的）。
2. **`tokens.output`（3204 vs 1562 = 2.05×）——口径不对等，对齐后是 1.04×。**
   MyCode 的 `n_output_tokens` 取自网关 `completion_tokens`，**含思考**
   （`agents/agent.py:413`「output 的子集，不是额外的一份；单独给是为了能算
   『可见输出 = output − reasoning』」，`model_caller.py:181` 就是
   `output_visible_tokens = max(0, output_tokens - reasoning_tokens)`）；
   opencode 的**不含**，这一条直接读 harbor 0.24.0 源码核实
   （`harbor/agents/installed/opencode.py:379-388`）：

   ```python
   output_tok    = tokens.get("output", 0) or 0
   reasoning_tok = tokens.get("reasoning", 0) or 0   # ← 单独一个字段
   total_output_tokens += output_tok                 # ← 只加 output，不加 reasoning
   ```

   实测对齐后的数字：

   | | MyCode | opencode | 倍数 |
   | --- | --- | --- | --- |
   | `n_output_tokens` 原始（217 题均值） | 3204 | 1562 | **2.05×** |
   | 其中思考（`mycode_reasoning_tokens`） | 1575（占 **49.2%**） | **拿不到**（见下） | — |
   | 可见 completion（原始 − 思考） | **1629** | **1562** | **1.04×** |
   | 只在 89 道两臂都通过的题上 | 1718 | 1484 | 1.16×（原始 2.18×） |

   **所以「MyCode 话多一倍」是单位不一致造成的假象**，两臂可见输出同量级——
   与 5.6 的「199 vs 178 token/次」和 5.7 的预测一致。

   **opencode 的思考量为什么拿不到**（5.7 当时只写到「metadata 是 null」，这里查到底了）：
   harbor 确实读了 `reasoning_tok`，但只把它塞进**逐 step** 的
   `Metrics.extra["reasoning_tokens"]`（`opencode.py:398-403`），
   而 trial 级的 `FinalMetrics` 只有 `total_prompt/completion/cached/cost/steps`
   ——**没有 total_reasoning 这一项**，聚合时就丢了。实测本仓库的 trial
   `result.json` 里 `step_results` 也是 `null`、`agent_result.metadata` 与
   `rollout_details` 同样是 `null`，所以逐 step 那份也没落盘。
   容器每题跑完就销毁，opencode 自己的 session 存储随之消失 →
   **「含思考的总生成量」这一格两臂无法比，只能空着**，
   除非改 harbor 的 adapter 把 `total_reasoning_tokens` 加进 `FinalMetrics`。
   取数脚本自带探针自检（`reasoning > 0` 的题数 217/217，
   若为 0 就说明字段没取到、"对齐后"数字会等于原始数字）。
3. **判分口径本身也会漂——而且变量守卫抓不到。** mycode 臂的报告最初是
   `f627034-dirty`（10-18）生成的、opencode 臂是 `9d6fb36-dirty`（10-21）生成的，
   中间 `NO_REWARD_FILE_EXCEPTIONS` 那条规则落地了：**同一个 `RewardFileNotFoundError`，
   旧报告记 `passed=False`、新报告记 `None`**。两份报告一比，opencode 有一批
   「构建不过 = 做错了」的题从分母里溜走，第一版对比因此得出 189 题 p = 0.53。
   守卫沉默是因为它比的是 `meta.variables`（model/provider/seed/stage/thinking，两臂确实全等），
   而 `meta.git_revision` 被 `ARM_KEYS` 主动剔除了（两臂各算自己的 revision，必然不同，
   留着守卫每次都开火）。**剔除是对的，但剔掉之后没有任何东西在看守判分口径。**
   处置：按 2.8c 用当前代码把**两臂都重出**，本节所有数字都出自重出后的报告。

`tokens.input`（88201 vs 89534，中位数 66430 vs 72358）是**可比**的：
`payload.py:219-220` 明确两边都是「含 cache 的总输入」。
两臂均值差 1.5%、中位数 MyCode 反而低 8%。
`n_cache_tokens` 两臂全是 **0**，所以首页那句「prefix cache 命中率 >96%」
**在这个 benchmark 里既不能证实也不能证伪**——harbor 这条链路上网关不回传 cache 计数。

### 6.5 这次基线顺带查出来的四个真缺陷

跑满 225 题的价值有一半在这里：这四个都是**只有跑满、并且被打断过很多次**才会露出来的。

1. **`merge_trials` 让「没判分的补跑」盖掉有效判分**（已修，`74c6b8c`）。
   原本无条件「后面的目录覆盖前面的」，于是补跑时 `AgentSetupTimeoutError` 产出的
   `reward=None` trial 会顶掉先前真跑出来的结果。实测咬到 **4 道 cpp 题**，
   其中 `polyglot_cpp_dnd-character` 是 `reward=1.0` 的**通过**被盖成未判分
   （opencode 臂因此少算 1 个通过、分母少 4 题：`111/206` → `112/219`）。
   修法：有判分的不能被没判分的覆盖；两边都没判分时仍取后一个
   （否则报告里的 exception 会停在旧那次，看不出基础设施修好没有）。
   两条测试，其中钉住新规则的那条**先跑红过**（`assert [None] == [1.0]`）。
2. **`completed_task_names` 把「没判分」当「已完成」→ 永久跳过。** 这是
   `runner.py:652-654` 写明的**既定设计**（「重跑失败的题目等于挑数据：一次失败的测量
   也是一次测量。真要重跑某几道用 `--tasks` 显式点名」），立场是对的，本次三轮补跑
   用的正是它给的 `--tasks` 逃生口。但后果要记着：**一次装环境挂死会让那道题在
   `--resume-from` 下永远不再被尝试**，所以补跑必须显式点名，不能指望续跑自己发现。
3. **`--resume-from` 是单值的**（`runner.py:743-750`，没有 `action="append"`），
   而看门狗只能取「最后一个 job 目录」。10-20 21:44 那次自动重起因此只看到 4 题已完成、
   起了 **221 题**，把已经跑完的 174 题重跑一遍，5 小时里真正推进 4 题。
   眼下用 `.scratch/harbor-merge-resume.sh`（硬链接合并 + 显式列目录 + 跑批活着就拒绝）
   和看门狗的 `refresh_merge_base()` 绕过，**没根治**。
4. **一条测试从 Task 13 起一直红，而 main 被推成红的**（已修，`5980fe2`）。
   `tests/integration/test_api.py::test_eval_benchmarks` 写死
   `["gaia","hle","smoke"]`，而 `f25314a` 把 loca/harbor 加进了路由的 `BENCHMARKS`。
   两轮评审都报「全量 1312 passed / 0 failed」，真跑是 `1 failed, 1307 passed`；
   而「1312」这个条数**复现不出来**（实际收集 1308）。改成对着真源断言
   `list(BENCHMARK_MODULES)`（`eval/common/jobs.py:507`），复跑 **1308 passed / 0 failed**。

### 6.6 剩下 6 题不再补跑（裁定与依据）

`polyglot_python_beer-song`、`list-ops`、`paasio`、`phone-number`、`robot-name`、
`polyglot_rust_accumulate`。九轮里每题已试 2–3 次，`agent_setup` **精确卡在
1080–1110 秒**（= 360s 基准 × 倍率 3 的天花板），而第 3 轮是在**空机器**上跑的
（SDD 循环全程暂停）——**争用假设到此排除，是挂死不是慢**。再跑第 4 轮没有信息量。

MyCode 臂这 6 题**全有判分**（4 过 2 不过），所以这是一条真实的不对称：
**在本环境里 MyCode 的 agent 225/225 装得进 setup 预算，opencode 是 219/225。**
它是 scaffold 的安装开销差，不是做题能力差，写结论时要这么限定。

### 6.7 能写进简历的、和不能写的

**能写**（都有 225 题的数据撑着）：

- 自建了一套可重复的两臂对照 coding-agent 基线：aider-polyglot 全量 225 题 / 6 语言，
  控制变量对齐（同模型、同 seed、同超时倍率、同并发），断点续跑、
  判分口径变更后可不重跑而复算，报告带**变量守卫**。
- 结论是**诚实的负结果**：同模型下 MyCode 与 opencode 通过率统计上无法区分
  （113/217 vs 112/217，p = 1.0）。能说清「为什么这个负结果可信」
  （配对设计、可检测下限、失败形状对称、三个不可比指标各自怎么处理）
  比说「我赢了」更值钱。
- 一条硬的、跨 scaffold 无关的自我消融仍在 5.7：思考开关对 MyCode 自己
  净 +3 题、代价生成量 1.9×、单题时长 1.56×。

**不能写**：

- 「pass@1 48.9% vs OpenCode 31.8%」——n = 48 的中期数据，跑满后作废。
- 「快 1.9×」/「1.86×」——5.6 已归因为思考开关差；225 题上 `duration_s` 因争用不对称直接弃用。
- 「输出 token 是 opencode 的 2.05×」——含思考 vs 不含思考，对齐后 1.04×（6.4 第 2 条）。
- 「通过率显著高于 opencode」——p = 1.0，方向都不成立。
- 「prefix cache 命中率 >96%」在这一节里**不能引用**：本 benchmark 两臂 `n_cache_tokens` 全 0。
