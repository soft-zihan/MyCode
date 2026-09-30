# MyCode

[![CI](https://github.com/soft-zihan/MyCode/actions/workflows/ci.yml/badge.svg)](https://github.com/soft-zihan/MyCode/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

编译式自进化的可观测 Coding Agent，支持任意 OpenAI 兼容端点。

核心特点：

* **Event-Sourcing 架构**：append-only 事件日志为唯一数据源（JSONL/SQLite 双后端，统一 Protocol 抽象，双份测试），对话、压缩、rewind/fork、崩溃恢复（Torn Tail 修复 + write-ahead claim + 启动 interrupted 扫描，覆盖子会话）、wiki 知识提取、前端重建全部派生于 Event；索引降低 message 构造开销，checkpoint + tail replay 快速冷启动，前端懒加载分页，WebSocket 单连接多路复用，断连续传，交互和可视化良好。
* **上下文工程**：上下文组装时系统提示词冻结，易变更内容后置，长任务 prefix cache 命中率平均**高于96%**. **双层可逆无感压缩**，工具折叠（可逆、按 key 取回）→ 会话折叠（hidden agent 编译会话笔记带校验 + 项目知识回写 wiki）渐进升级；压缩前编辑内容可查询，(跨)会话内容可检索。 LOCA-128k×100k 压力窗四臂消融（qwen3.6-27b）：**Pass@1 66.7%，高于仅工具折叠  20.0pp、朴素截断33.4pp、仅会话折叠 13.4pp**. steer 消息支持排队/插入。task_list 列表渐进式披露。
* **LLM-wiki 自进化**：从 Event 中自动提取会话笔记、结构化 memory. 写入前预检索去重 + 8 信号加权评分，元索引 + Embedding + bm25 混合召回 + drop-rung ladder；**高频 workflow pattern 编译为 skill**，验证门禁严格门控。
* **Git 快照机制**：独立 git 仓库+ alternates 机制共享原项目 objects 节省空间；git tree 对象捕获文件状态，文件编辑三阶段回退（stage → commit → clear），wiki 可差分编译、版本可追溯。
* **多 Agent 协同**：主 Agent 根据模式隔离工具，子 Agent 可自定义（独立 Tools+MCP+Skills 分担上下文压力，**前台阻塞可转后台，可二次调用**），后台 hidden Agent 防阻塞，模型配置差异化.
* **工具系统**：ToolRegister（链式披露、出错重试、超时保护、先读后写、权限检查、完整保护、防注入） + MCP（Stdio + Streamable HTTP） 工具输出定界符防御间接提示注入。
* **全链路观测和评估**：OTel + Langfuse 搭建 Trace，建立单元测试和端到端评估回归，覆盖 Agent Loop、Wiki 编译和召回、Skill 生成等，实现修改效果可观测、可验证，badcase 可定位、可修复。

## 架构

```
浏览器 ──> Vite 前端 (React 18 + TS, :8090, /api 代理到 :5555)
              │
              ▼
       FastAPI 后端 (frontend/server/, :5555)
              │  WebSocket + REST
              ▼
       agents/  Agent Loop（ReAct 编排、压缩、记忆、技能、计划）
              │
              ▼
       ~/.mycode/sessions/*.events.jsonl   会话事件（唯一数据源）
       ~/.my-code/config.json              模型端点配置
```


| 目录        | 说明                                                                                  |
| ------------- | --------------------------------------------------------------------------------------- |
| `agents/`   | Agent Loop、上下文压缩、会话存储、工具、Skill 进化、Wiki 记忆、Plan 系统、提示词      |
| `frontend/` | React + Vite 前端与 FastAPI 后端（`frontend/server/`），openapi.json 为前后端契约单源 |
| `eval/`     | smoke 全链评测、Langfuse judge、LOCA / GAIA / HLE 基准                                |
| `mycode-eval-skill/` | 评测与实验系统操作手册（套件命令、实验设计铁律、失败定位、token 成本分析）  |
| `tests/`    | unit / integration 两层测试（`tests/e2e/` 目前为占位；端到端回归走 `eval.smoke --suite chain`） |
| `start.sh`  | 一键启停脚本                                                                          |

## 环境要求

- Python 3.11+
- Node.js 18+
- macOS / Linux
- `lsof`（`start.sh` 用它做端口探活与清理；精简的 Linux 容器镜像常缺，需自行安装）

## 安装

```bash
git clone https://github.com/soft-zihan/MyCode.git
cd MyCode

./install.sh          # 校验前置依赖 → 建 venv → 装 Python + 前端依赖 → 生成 .env
./install.sh --dev    # 追加 pytest / requests（跑测试与评测需要）
```

`install.sh` 会校验 Python ≥ 3.11、Node ≥ 18、git 是否就位，缺 `lsof` 时给出警告（不致命，但 `start.sh` 的端口探活与清理会退化）。

偏好手动安装的话，等价步骤：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # 含 requirements.txt；只跑服务可用 requirements.txt
cd frontend && npm install && cd ..
```

## 模型配置

支持任意 OpenAI 兼容端点，多端点 + 路由（主模型 / 辅模型 / Plan 各阶段可分别绑定）。

**方式一（推荐）**：启动服务后在 Web UI 的 Agents 设置页添加 Provider（base_url / model / api_key），并设为主端点。

**方式二**：直接编辑 `~/.my-code/config.json`：

```json
{
  "endpoints": {
    "my-provider": {
      "model": "your-model-name",
      "base_url": "https://your-endpoint/v1",
      "api_key": "sk-...",
      "context_window": 1000000,
      "thinking": null,
      "thinking_feedback": false,
      "provider_name": "my-provider"
    }
  },
  "routing": { "primary": "my-provider" }
}
```

Wiki 语义召回需要 embedding 配置，同样可在 UI 中配置。

`routing.primary` 可省略，省略时静默回退到 `endpoints` 中的第一个端点——建议显式写明，避免调整端点顺序时主模型被意外改变。

**embedding 需要独立端点**：模型网关通常不提供 `/embeddings`（实测会 404），需单独准备，例如 SiliconFlow：

```json
{
  "embedding": {
    "backend": "openai",
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": "sk-...",
    "model": "BAAI/bge-large-zh-v1.5"
  }
}
```

未配置或调用失败时，召回自动降级为关键词打分（`recall.py` 中 embedding 异常被捕获后回退 `_keyword_relevance`），服务仍正常运行，只是语义召回能力下降。

## 环境变量（可选）

复制模板后按需填写，`start.sh` 会自动加载 `.env`（已 gitignore）：

```bash
cp .env.example .env
```

| 变量                                          | 默认                         | 说明                              |
| ----------------------------------------------- | ------------------------------ | ----------------------------------- |
| `MYCODE_TRACING`                              | `1`（经 `start.sh`）         | Langfuse tracing 开关；直接跑后端时默认关 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 空                           | Langfuse 密钥（不填则仅本地日志） |
| `LANGFUSE_BASE_URL`                           | `https://cloud.langfuse.com` | Langfuse 服务地址                 |
| `MYCODE_SESSION_BACKEND`                      | `jsonl`                      | 会话后端：`jsonl` / `sqlite`      |
| `MYCODE_HOST`                                 | 空（仅本机）                 | 远程部署监听地址，如`0.0.0.0`     |
| `MYCODE_AUTH_TOKEN`                           | 空                           | 暴露服务时的 Bearer 鉴权 token    |
| `MYCODE_SIDE_MODEL`                           | 空                           | 压缩 side query 使用的辅助端点    |
| `MYCODE_SKILL_AUTO_ACTIVATE`                  | **关**（观察模式）           | Skill champion 自动激活，需显式开启 |
| `MYCODE_AUTO_SKILL_EVOLUTION`                 | 开                           | Skill 在线自进化开关              |

## MCP 配置（可选）

MCP server 配置读 `<项目根>/.mcp.json`，该文件已 gitignore。仓库提供 `.mcp.json.example` 作为模板：

```bash
cp .mcp.json.example .mcp.json   # 然后删掉用不到的 server 条目
```

不配置也完全可用（后端启动时 MCP 为 0 tools）。**注意**：模板里的 server 需要各自的可执行文件（`npx`、`code-review-graph`），保留用不到的条目会让后端启动时逐个尝试连接直至超时，显著拖慢冷启动。

另可配置全局 `~/.mycode/settings.json` 与项目级 `.mycode/settings.json`，三者合并，后读取的覆盖同名 server。

## 启动

```bash
./start.sh            # 开发模式（默认）：后端 :5555 + Vite dev server :8090
./start.sh prod       # 生产模式：构建前端并由后端直接托管 dist，只占 :5555
./start.sh status     # 查看状态
./start.sh restart    # 重启（回到开发模式）
./start.sh stop       # 停止
```

开发模式访问 http://localhost:8090 （Vite 把 `/api` 代理到 :5555）；生产模式访问 http://localhost:5555 。日志在 `logs/.backend.log` 与 `logs/.frontend.log`（`logs/` 由脚本自动创建）。

两种模式的取舍：dev server 有 HMR、改前端即时生效，但产物未压缩，且 `frontend/vite.config.ts` 固定绑 `0.0.0.0`（`:8090` 默认即对局域网可达）。生产模式由 FastAPI 托管 `frontend/dist`（含 SPA 路由回退与目录穿越防护），单端口、没有 dev server 暴露面；代价是前端改动后需重新 `npm run build`。`prod` 模式下 `status` 会显示前端未运行，属正常。

`stop` / `restart` 会等后端优雅退出（最多 10s）再强制清理端口——lifespan 的关闭段需要 flush Langfuse trace 并给活跃会话合成 `turn/end{shutdown}`。

远程访问推荐 SSH 隧道（无需鉴权）：

```bash
ssh -L 8090:localhost:8090 -L 5555:localhost:5555 user@server
```

直接暴露公网需显式设置 `MYCODE_HOST=0.0.0.0` 与 `MYCODE_AUTH_TOKEN`。注意这只约束后端：开发模式下 `:8090` 的 dev server 不受其约束（静态界面无鉴权，`/api` 经代理仍受 token 保护）。对外部署请用 `prod` 模式，或把构建产物交给反向代理。

## 命令行入口

除 Web UI 外，仓库根目录提供两个可执行入口：

```bash
./scripts/install-mycode.sh   # 把 mycode 软链到 /usr/local/bin（需写权限，否则按提示 sudo）

mycode [options] [prompt]     # 终端 REPL，工作区为当前目录（可从任意路径调用）
mycode-web [start|stop|...]   # start.sh 的软链包装，默认 start
```

`mycode` 会优先使用项目自带的 `.venv/bin/python`（探测 `import openai` 判断依赖是否装好），找不到时回退 `python3`。

## 测试与评测

```bash
source .venv/bin/activate

# 1) 单元测试
python -m pytest tests/unit -q          # 后端 386 项
cd frontend && npm test && cd ..        # 前端 12 项
./scripts/run_tests.sh                  # 仓库门禁：jsonl / sqlite 双后端 + tests/ 集成

# 2) 端到端测试（chain 全链路回归；需先 ./start.sh 起服务并配好模型）
python -m eval.smoke.runner --suite chain
python -m eval.langfuse.run_evals --judge   # 配了 Langfuse 后追加 LLM-as-Judge 评审

# 3) LOCA 对比测试（压缩策略四臂消融，全量跑完整个 bench；准备见下一节）
python -m eval.loca.runner --config-set 128k --window 100k --arm full
```

CI（`.github/workflows/ci.yml`）只跑第 1 类：后端 `pytest tests/ --ignore=tests/e2e` 的 **Python 3.11 × 3.12 × jsonl / sqlite** 四格矩阵、前端 `npm ci && npm test && npm run build`，以及 shell 脚本的 `bash -n` 与 CRLF 拒绝检查。端到端与 LOCA 需要真实服务、模型凭据和小时级时长，不在 CI 内。

## 评测 bench 准备与实验启动

### 前置：LOCA-bench

`eval/loca/runner.py` 把整只 agent 当 scaffold 跑 LOCA-bench。该基准是**外部仓库，不随本项目分发**（`projects/` 已 gitignore），需自行放置成如下结构：

```
projects/LOCA-bench/                            # 默认路径，可用 --loca-repo 指向别处
├── .venv/bin/python                            # LOCA 自己的解释器，可用 --loca-venv-python 覆盖
└── task-configs/final_<set>_set_config.json    # set ∈ 8k|16k|32k|64k|96k|128k|256k
```

解释器或配置文件缺任一，runner 立即 `SystemExit` 并打印缺失路径。LOCA 在进程内跑 agent，**不需要后端服务在跑**。

### 工作区位置是实验条件，不是实现细节

评测工作区固定在 `~/.mycode/eval_workspaces/`（**必须在仓库外**）。放进仓库内会有两个后果：任务产物泄漏进 git；agent 经 `load_agents_md` 向上遍历继承开发用的 AGENTS.md，其中的规则（例如"遇到网络问题必须告知用户"）在无人值守评测里等于弃题，实测会压低通过率。

### 开跑前预检

1. **触发验证先于全量**——消融最容易整批白跑的地方。触发线 `effective_window = window − 20K`，工具折叠在利用率 > 80% 时触发（`--window 100k` → 触发线 64K）。先跑 1-2 题，确认 runner 输出的 `compression_events` > 0。runner 已内置该检查：零触发时会打印 `⚠️ 压缩事件计数为 0——本次运行未触发压缩，消融对比无效`。四臂机制未分化时，跑多少次都测不到东西。
2. **网关直测**目标模型返回 200（连测两次排除瞬时抖动），确认请求真的指向你的端点。配置跨源混配（env 里的 key + SDK 默认 base）会静默打到 `api.openai.com`，401 伪装成限流。
3. **题集落在能力区间**（pass 率约 20%-70%）。全 0 或全 1 都没有区分度；换模型先跑 3 题探测再定 config-set。
4. **报告 meta 自证**：`eval/reports/{benchmark}-{ts}.json` 的 meta 段须含 arm / window / thinking / model / seed，否则结果不可归因。开跑前查一次。
5. **磁盘与内存**：每题工作区可达数百 MB；每并发任务约 1-1.5GB（agent + loca_side 双 Python，可能还有 chromium 树），16GB 机器并发上限 4-6——API 侧允许高并发不等于本地扛得住。
6. 实验期间**不要** `./start.sh restart`，backend_session 模式会杀掉活跃评测。

### 启动四臂消融

正式实验按**全量**跑：`--sample` 缺省即跑完整个 config set（LOCA 每个 set = 15 任务 × 5 states = 75 配置）。

```bash
# 单臂全量
python -m eval.loca.runner --config-set 128k --window 100k \
    --arm full --thinking off --model qwen3.6-27b

# 四臂：arm ∈ full | truncate | tool_only | session_only，除 arm 外所有参数必须完全一致
for arm in full truncate tool_only session_only; do
  python -m eval.loca.runner --config-set 128k --window 100k \
      --arm "$arm" --thinking off --model qwen3.6-27b
done
```

臂的含义：`full` 双层压缩 / `truncate` 朴素硬截断（同触发点、同目标水位、无摘要、保首条指令、整组隐藏）/ `tool_only` 仅工具折叠 / `session_only` 仅会话折叠。

**时长与并发**：LOCA 128k 单配置约 8.5min（qwen3.6-27b），75 配置串行约 10 小时。`--parallel N` 可并发（各任务工作区相互隔离），但每并发任务约 1-1.5GB 内存（agent + loca_side 双 Python，可能还有 chromium 树），16GB 机器上限 4-6——API 侧允许高并发不等于本地扛得住。小时级全量消融建议用 watcher 无人值守调度（见 `mycode-eval-skill/references/experiment-ops.md`）。

### 变量控制与统计口径

- **配对设计**：同一 config set 跨臂，臂间同模型、同 thinking 状态、同 `--seed`。run_id 内嵌 arm 与起始时间戳，并行臂不碰撞且报告可自归因。
- **统计方法**：按 `(seed, task_id)` 建 pass 矩阵，用 McNemar 错配对计数 + sign test 单边 p。**不比较裸 pass 率点估计**——点估计差在小样本下没有推断力。
- **可复现性**：`--select head`（默认）按配置文件顺序全量取，跨 run 完全确定；`--indices` 复现特定子集。`--sample N` 只用于探测与校准，不用于产出正式结论。
- **归因完整性**：报告 meta 必须含 arm / window / thinking / model / seed，缺一即不可归因；开跑前查一次。

结果异常漂亮或异常符合预期时，先做机制核查——触发计数、token 结构差异、失败形态分类三者与假设一致，才可下结论。

失败定位工作流与 trace / token 聚合脚本见 `mycode-eval-skill/`（`SKILL.md` + `references/`）。

## 数据位置

- 会话事件：`~/.mycode/sessions/*.events.jsonl`
- 模型配置：`~/.my-code/config.json`
- 运行日志：`logs/`

## License

[MIT](LICENSE)
