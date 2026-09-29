# MyCode

[![CI](https://github.com/soft-zihan/MyCode/actions/workflows/ci.yml/badge.svg)](https://github.com/soft-zihan/MyCode/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

编译式自进化的可观测 Coding Agent，支持任意 OpenAI 兼容端点。

核心特点：

* **Event-Sourcing 架构**：append-only 事件日志为唯一数据源（JSONL/SQLite 双后端，统一 Protocol 抽象，双份测试），对话、压缩、rewind/fork、崩溃恢复（Torn Tail 修复 + write-ahead claim + 启动 interrupted 扫描，覆盖子会话）、wiki 知识提取、前端重建全部派生于 Event；索引降低 message 构造开销，checkpoint + tail replay 快速冷启动，前端懒加载分页，WebSocket 单连接多路复用，断连续传，交互和可视化良好。
* **上下文工程**：上下文组装时系统提示词冻结，易变更内容后置，长任务 prefix cache 命中率平均**高于96%**. **双层可逆无感压缩**，工具折叠（可逆、按 key 取回）→ 会话折叠（hidden agent 编译会话笔记带校验 + 项目知识回写 wiki）渐进升级；压缩前编辑内容可查询，(跨)会话内容可检索。 LOCA-128k×100k 压力窗四臂消融：**Pass@1 66.7%，高于朴素截断 33.4pp、仅工具折叠 20.0pp、仅会话折叠 13.4pp**. steer 消息支持排队/插入。todolist 列表渐进式披露。
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
| `eval/`     | smoke 全链评测、Langfuse judge、GAIA / HLE / LOCA / SkillsBench 基准                  |
| `tests/`    | unit / integration / e2e 三层测试                                                     |
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

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 跑测试 / 评测还需 pytest、requests（已含上一行依赖）
pip install -r requirements-dev.txt

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
./start.sh            # 启动后端(:5555) + 前端(:8090)，等价于 ./start.sh start
./start.sh status     # 查看状态
./start.sh restart    # 重启
./start.sh stop       # 停止
```

访问 http://localhost:8090 。日志在 `logs/.backend.log` 与 `logs/.frontend.log`（`logs/` 由脚本自动创建）。

`stop` / `restart` 会等后端优雅退出（最多 10s）再强制清理端口——lifespan 的关闭段需要 flush Langfuse trace 并给活跃会话合成 `turn/end{shutdown}`。

远程访问推荐 SSH 隧道（无需鉴权）：

```bash
ssh -L 8090:localhost:8090 -L 5555:localhost:5555 user@server
```

直接暴露公网需显式设置 `MYCODE_HOST=0.0.0.0` 与 `MYCODE_AUTH_TOKEN`。注意这只约束后端：`frontend/vite.config.ts` 的 dev server 固定绑 `0.0.0.0`，`:8090` 默认即对局域网可达（静态界面无鉴权，`/api` 经代理仍受 token 保护）。生产部署请用 `npm run build` 产物配合反向代理，而非 dev server。

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

# 单元测试
python -m pytest tests/unit -q

# 单元 + 集成，jsonl / sqlite 双后端各跑一遍（仓库门禁）
./scripts/run_tests.sh
./scripts/run_tests.sh --smoke   # 追加全链路 smoke（jsonl 腿）

# 全链路 smoke 评测：HTTP/WS 客户端，需先 ./start.sh 起后端并配好模型
python -m eval.smoke.runner --suite chain
python -m eval.langfuse.run_evals --judge

# GAIA 对比实验（独立 benchmark，固定 Level 3；需已配置模型）
python -m eval.gaia.runner --sample 10

# 前端单元测试 + 类型检查 + 构建
cd frontend && npm test && npm run build && cd ..
```

CI（`.github/workflows/ci.yml`）在 push / PR 时跑：后端 `pytest tests/ --ignore=tests/e2e` 的 **Python 3.11 × 3.12 × jsonl / sqlite** 四格矩阵、前端 `npm ci && npm test && npm run build`，以及 shell 脚本的 `bash -n` 与 CRLF 拒绝检查。smoke / GAIA 腿需要真实服务与模型凭据，不在 CI 内。

## 数据位置

- 会话事件：`~/.mycode/sessions/*.events.jsonl`
- 模型配置：`~/.my-code/config.json`
- 运行日志：`logs/`

## License

[MIT](LICENSE)
