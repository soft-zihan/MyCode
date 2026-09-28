# MyCode

自进化的 Agentic Coding Runtime。不是 API Wrapper，而是完整的编排框架（Harness）：模型只产生 tool_call，由 Harness 负责权限检查、工具执行、结果回写与事件持久化，形成持续多轮 ReAct 闭环。

核心能力：

- **Event 溯源会话**：Event 为唯一数据源，jsonl / sqlite 双后端，支持回退（rewind）与投影缓存
- **两级渐进式上下文压缩**：工具折叠（浅，可逆）→ 会话折叠（深，LLM 摘要），Surface 索引机制保证恢复字节精确
- **Wiki 项目记忆**：会话笔记、项目知识异步编译、语义召回（embedding）、五重 Memory 防污染
- **Skill 自进化**：在线候选沉淀 → 评测 → 晋升的完整闭环
- **Plan 系统**：多阶段策略路由（grill-spec / tasks / execute / review / converge），各阶段可独立绑定模型
- **全链路可观测**：Langfuse tracing + bad case 检测 + 三层评测体系（单元 / smoke 全链 / benchmark）

评测结果（详见 eval/）：SWE-bench Verified 38.2% · HLE 20.2% · LOCOMO 51.8%（+9.7 vs 关闭持久记忆）· SkillsBench 65.5%（+24.1 vs No Skill）。

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

| 目录 | 说明 |
| --- | --- |
| `agents/` | Agent Loop、上下文压缩、会话存储、工具、Skill 进化、Wiki 记忆、Plan 系统、提示词 |
| `frontend/` | React + Vite 前端与 FastAPI 后端（`frontend/server/`），openapi.json 为前后端契约单源 |
| `eval/` | smoke 全链评测、Langfuse judge、GAIA / HLE / LOCA / SkillsBench 基准 |
| `tests/` | unit / integration / e2e 三层测试 |
| `start.sh` | 一键启停脚本 |

## 环境要求

- Python 3.11+（开发环境 3.14）
- Node.js 18+（开发环境 26）
- macOS / Linux（`start.sh` 依赖 `lsof`）

## 安装

```bash
git clone https://github.com/soft-zihan/MyCode.git
cd MyCode

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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

Wiki 语义召回需要 embedding 配置（默认 SiliconFlow 的 `BAAI/bge-large-zh-v1.5`，免费；也可切本机 ollama），同样可在 UI 中配置。

## 环境变量（可选）

在项目根创建 `.env`（已 gitignore），`start.sh` 会自动加载：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYCODE_TRACING` | `1` | Langfuse tracing 开关 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 空 | Langfuse 密钥（不填则仅本地日志） |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Langfuse 服务地址 |
| `MYCODE_SESSION_BACKEND` | `jsonl` | 会话后端：`jsonl` / `sqlite` |
| `MYCODE_HOST` | 空（仅本机） | 远程部署监听地址，如 `0.0.0.0` |
| `MYCODE_AUTH_TOKEN` | 空 | 暴露服务时的 Bearer 鉴权 token |
| `MYCODE_SIDE_MODEL` | 空 | 压缩 side query 使用的辅助端点 |
| `MYCODE_SKILL_AUTO_ACTIVATE` | 开 | Skill 自动激活开关 |
| `MYCODE_AUTO_SKILL_EVOLUTION` | 开 | Skill 在线自进化开关 |

## 启动

```bash
./start.sh            # 启动后端(:5555) + 前端(:8090)
./start.sh status     # 查看状态
./start.sh restart    # 重启
./start.sh stop       # 停止
```

访问 http://localhost:8090 。日志在 `logs/.backend.log` 与 `logs/.frontend.log`。

远程访问推荐 SSH 隧道（无需鉴权）：

```bash
ssh -L 8090:localhost:8090 -L 5555:localhost:5555 user@server
```

直接暴露公网需显式设置 `MYCODE_HOST=0.0.0.0` 与 `MYCODE_AUTH_TOKEN`。

## 测试与评测

```bash
source .venv/bin/activate

# 单元 + 集成测试（jsonl / sqlite 双腿）
python -m pytest tests/unit -q

# 全链路 smoke 评测（需已配置模型；接 Langfuse 后可评审）
python -m eval.smoke.runner --suite chain
python -m eval.langfuse.run_evals --judge

# GAIA 对比实验（独立 benchmark，固定 Level 3）
python -m eval.gaia.runner --sample 10
```

## 数据位置

- 会话事件：`~/.mycode/sessions/*.events.jsonl`
- 模型配置：`~/.my-code/config.json`
- 运行日志：`logs/`

## Docker

暂不提供容器化部署，请按上述步骤原生运行。
