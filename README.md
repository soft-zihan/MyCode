# Bear Agent

Bear Agent 是一个基于 Python 实现的 **自进化 Harness Agent**。它不是简单的命令行聊天工具，而是一个可运行、可阅读、可扩展的本地 Coding Agent Runtime：统一编排大模型推理、工具调用、文件编辑、Shell 执行、权限控制、长期记忆、Skills、自进化、MCP 外部工具、子 Agent 和会话恢复。

项目重点是 **Harness**：模型只负责推理和提出工具调用意图，真正的环境操作由 Bear Code Runtime 统一做权限判断、工具执行、结果回写、上下文压缩和经验沉淀。它适合学习 Claude Code 类工具的底层机制，也适合作为个人 Coding Agent、项目分析助手或领域 Agent 的二次开发基础。

## 核心亮点

- **自进化 Harness Agent**：从用户反馈中自动抽取可复用规则，新增或合并到 `SKILL.md`，让 Agent 能随着使用持续沉淀能力。
- **完整 Agent Loop**：模型请求、tool call 解析、权限检查、工具执行、tool result 回写、继续推理、会话保存形成闭环。
- **OpenAI / Anthropic 双协议**：支持 OpenAI-compatible 和 Anthropic-compatible 接口，便于接入不同模型服务或代理网关。
- **多模型注册表**：注册任意多个 URL/Key 相互独立的模型端点，主 Agent、子 Agent、side query 按**模型名**路由到对应端点。
- **可打断与可回退**：Ctrl+C 可打断主/子 Agent；`/rewind` 回退对话的同时恢复被修改的文件（写前快照 + 轮次边界）。
- **/goal 自主目标模式**：提炼成功标准 → 自主执行 → verifier 逐条判定 → 未达标回灌证据，直到达标或预算用尽。
- **上下文可视化与可逆压缩**：`/context` 查看上下文构成，`/ctx del/keep` 按工具配对组局部删除；snip/clear 压缩前原文无损入库，模型可用 `context_restore` 随时取回（ACE 式可逆上下文）。
- **工具系统与权限控制**：支持读写文件、精确编辑、代码搜索、Shell 命令、Skill 调用、子 Agent 和 MCP 工具；Plan Mode 下阻断写操作和 Shell。
- **Skills 体系**：通过项目级和用户级 `SKILL.md` 保存可复用任务方法，支持检索、调用、inline / fork 执行和版本化演化。
- **长期 Memory**：按项目路径 hash 隔离记忆，保存用户偏好、项目背景、历史决策和参考资料。
- **MCP 外部工具扩展**：自研 stdio JSON-RPC MCP Client，把外部 MCP Server 工具包装为 `mcp__server__tool`。
- **子 Agent**：支持 `explore`、`plan`、`general` 以及自定义子 Agent，用隔离上下文完成探索、规划或局部任务。
- **会话恢复和上下文压缩**：自动保存 session，支持 `--resume`、`/compact`，并对大工具结果做截断或持久化。
- **可插拔后端架构（Capability Seam）**：借鉴 Pi 的 Capability Seam 模式，5 个核心功能模块抽象为可替换接口，支持多种后端实现：
  - **Session Seam**：会话存储抽象（JSON/JSONL/内存）
  - **Compaction Seam**：压缩策略抽象（四级可逆/单级/自定义）
  - **Provider Seam**：LLM Provider 抽象（OpenAI/Anthropic/自定义）
  - **Memory Seam**：Memory 存储抽象（文件/SQLite）
  - **Skill Seam**：Skill 存储抽象（文件型）

## 评估框架（v2.1 计划中）

BearCode 计划集成 5 个开源 benchmark 建立完整的评估体系：

| Benchmark | 任务数 | 评估维度 |
|-----------|--------|---------|
| **SWE-bench Lite** | 300 | 真实 GitHub issue 修复 |
| **HumanEval** | 164 | 代码生成 |
| **MBPP** | 500 | 基础 Python 编程 |
| **SkillsBench** | 80+ | Skill 调用效果 |
| **Aider Edit** | 225 | 代码编辑 |

评估框架使用 **Inspect AI**（评估运行器）+ **Langfuse**（可观测性）。

详见 `wiki/MyCode升级进化方案v2.1.md`。

## 项目架构

![Bear Agent 总体架构](wiki/assets/architecture/01-overall-architecture.svg)

核心运行链路：

```text
用户输入
  -> agents/main.py
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Harness 做权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

## 目录结构

```text
BearAgent/
├── agents/
│   ├── main.py                    # CLI 入口、REPL、参数解析
│   ├── agent.py                   # Agent Runtime、模型调用、工具调度、上下文压缩
│   ├── options.py                 # AgentOptions 配置数据类
│   ├── seams/                     # Capability Seam 接口定义（v0.2 新增）
│   │   ├── session.py             # Session Seam 接口 + 实现
│   │   ├── compaction.py          # Compaction Seam 接口 + 实现
│   │   ├── provider.py            # Provider Seam 接口 + 实现
│   │   ├── memory.py              # Memory Seam 接口 + 实现
│   │   └── skill.py               # Skill Seam 接口 + 实现
│   ├── tools.py                   # 内置工具和权限系统
│   ├── prompt.py                  # System prompt 动态构建
│   ├── skills.py                  # Skills 加载、检索、执行、创建和演化封装
│   ├── online_skill_evolution.py  # 在线 Skill 抽取和 add/merge/discard 决策
│   ├── skill_evolution.py         # Skill 落盘、版本快照、审计统计
│   ├── memory.py                  # 长期记忆系统（含 memory 工具 add/replace/remove）
│   ├── mcp_client.py              # MCP stdio JSON-RPC 客户端
│   ├── subagent.py                # 子 Agent 配置
│   ├── session.py                 # 会话保存与恢复
│   ├── model_registry.py          # 多模型端点注册表，按模型名路由
│   ├── checkpoints.py             # 文件写前快照与轮次边界（/rewind）
│   ├── goal.py                    # /goal 自主目标循环（标准提炼+verifier）
│   ├── context_edit.py            # 上下文描述与按工具配对组的局部删除
│   ├── context_store.py           # ACE 式可逆上下文存储（raw+abstract）
│   ├── session_memory.py          # 会话记忆折叠 schema 与解析
│   ├── online_skill_eval.py       # 在线 Skills 评测（replay/规则/LLM judge）
│   ├── frontmatter.py             # frontmatter 解析器
│   └── ui.py                      # 终端 UI 输出
├── tests/                         # pytest 测试（307 例，离线可跑）
├── .bear/
│   ├── skills/                    # 项目级 Skills
│   └── skill-evolution/           # Skills 自进化审计产物
├── wiki/                          # 项目文档中心
├── Dockerfile
├── requirements.txt
└── README.md
```

## 快速启动

### 1. 准备环境

推荐环境：

- Python 3.11+
- macOS / Linux
- Git
- ripgrep，可选但推荐
- 一个 OpenAI-compatible 或 Anthropic-compatible 模型接口

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 `.env`

项目会自动读取当前目录或父目录中的 `.env`。

Anthropic-compatible 示例：

```env
APIKEY=sk-your-api-key
API=https://your-host/anthropic
MODEL=claude-sonnet-4-6
```

OpenAI-compatible 示例：

```env
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://your-host/v1
MODEL=gpt-4o
```

也可以使用通用变量：

```env
APIKEY=sk-your-api-key
API=https://your-host/v1
MODEL=deepseek-chat
```

协议判断规则：

- `API` 或 `--api-base` 路径包含 `/anthropic` 时，按 Anthropic-compatible 调用。
- 否则有 OpenAI base URL 时，按 OpenAI-compatible 调用。
- `--model` 会覆盖 `.env` 中的 `MODEL`（也支持 `MINI_CLAUDE_MODEL` 变量）。

### 2.5 多模型注册表（可选）

可以注册任意多个 URL/Key 相互独立的模型端点，子 Agent 和 side query 按**模型名**路由：

```env
# 注册端点：<ID> 只是把三行归为一组的内部标签，可任意起名，想加更多 API 再加一组
BEAR_ENDPOINT_A_BASE_URL=https://host-a/v1
BEAR_ENDPOINT_A_API_KEY=sk-aaa
BEAR_ENDPOINT_A_MODEL=deepseek-v4-pro

BEAR_ENDPOINT_B_BASE_URL=https://host-b/anthropic
BEAR_ENDPOINT_B_API_KEY=sk-bbb
BEAR_ENDPOINT_B_MODEL=claude-sonnet-4-6

# 子 Agent 路由：值写【模型名】，注册表自动找到提供该模型的端点
BEAR_MODEL_EXPLORE=claude-sonnet-4-6
BEAR_MODEL_PLAN=claude-sonnet-4-6
BEAR_MODEL_GENERAL=deepseek-v4-pro

# side query 路由（记忆召回/会话折叠/skill 进化等轻量调用）
BEAR_SIDE_MODEL=deepseek-v4-pro

# 上下文窗口与自动压缩（可选）
BEAR_CONTEXT_WINDOW=200000        # 覆盖模型上下文窗口（token）
BEAR_AUTO_COMPACT_THRESHOLD=0.9   # 上下文达到该比例时自动压缩（默认 0.7）
```

自定义子 Agent 也可以在 `.bear/agents/<name>.md` frontmatter 里写 `model: <模型名>` 自行配置，无需环境变量。

### 3. 启动 REPL

```bash
python3 -m agents.main
```

启动后直接输入任务，例如：

```text
阅读这个项目，告诉我 Agent Loop 是怎么跑起来的
```

### 4. 执行一次性任务

```bash
python3 -m agents.main "总结这个项目的目录结构和核心模块"
```

### 5. 使用 Plan Mode

Plan Mode 适合重构、复杂修复和多文件修改。它会先只读分析和写计划，用户审批后再执行。

```bash
python3 -m agents.main --plan "分析 Skills 检索逻辑应该如何优化"
```

REPL 中也可以输入：

```text
/plan
```

### 6. 恢复最近会话

```bash
python3 -m agents.main --resume
```

## 如何让项目自动沉淀并进化 Skills

Bear Code 的核心特色是 **自进化 Skills**。它可以从用户明确反馈中抽取未来可复用的规则，并自动新增或合并到项目级或用户级 `SKILL.md`。

### 1. 开启自动自进化

默认 `BEAR_AUTO_SKILL_EVOLUTION` 是开启的。为了明确配置，建议在 `.env` 中写：

```env
BEAR_AUTO_SKILL_EVOLUTION=1
BEAR_AUTO_SKILL_TARGET=project
```

含义：

- `BEAR_AUTO_SKILL_EVOLUTION=1`：启用在线 Skill 自进化。
- `BEAR_AUTO_SKILL_TARGET=project`：自动新增的 Skill 写入当前项目 `.bear/skills/`。

如果希望沉淀为所有项目共享的个人 Skill：

```env
BEAR_AUTO_SKILL_TARGET=user
```

对应路径：

```text
project: <project>/.bear/skills/<skill_name>/SKILL.md
user:    ~/.bear/skills/<skill_name>/SKILL.md
```

### 2. 用允许写入的权限模式启动

后台自动写入 Skill 需要当前权限模式允许写文件。推荐使用：

```bash
BEAR_AUTO_SKILL_EVOLUTION=1 \
BEAR_AUTO_SKILL_TARGET=project \
python3 -m agents.main --accept-edits
```

也可以使用更激进的模式：

```bash
python3 -m agents.main --yolo
```

不建议长期默认使用 `--yolo`，因为它会跳过确认。日常推荐 `--accept-edits`，既能让后台 Skill 写入正常发生，又不会绕过所有权限判断。

### 3. 给出可复用反馈

自进化不是把每一句话都写成 Skill。它只适合沉淀稳定、明确、未来同类任务仍适用的规则。

不太适合沉淀：

```text
帮我写一篇 500 字政府报告。
```

适合沉淀：

```text
以后写政府报告、工作汇报、调研材料时，默认先给可直接使用的初稿，不要连续追问；结构按“标题、背景、主要情况、问题分析、工作举措、下一步计划”组织，语言要正式克制。
```

适合演化已有 Skill：

```text
以后这类政府报告还要加入关键数据指标，不能只写概念性表述。
```

### 4. 自进化链路如何工作

```text
第 N 轮用户任务
  -> Agent 输出结果
  -> 保存 pending extraction window
第 N+1 轮用户反馈
  -> 合并进上一轮 window
  -> online_ingest()
  -> Extractor 抽取候选 Skill
  -> Maintainer 判断 add / merge / discard
  -> create_skill_file() 或 evolve_skill_file()
  -> 写入 SKILL.md
  -> 记录 provenance、usage stats 和版本快照
```

核心文件：

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

审计产物：

```text
.bear/skill-evolution/usage.jsonl
.bear/skill-evolution/online_provenance.jsonl
.bear/skill-evolution/online_skill_provenance.json
.bear/skill-evolution/skill_usage_stats.json
.bear/skill-evolution/history/
.bear/skill-evolution/pruned/
```

### 5. 手动触发当前窗口抽取

如果你想立即把当前对话窗口送入自进化链路，可以在 REPL 中执行：

```text
/extract_now 这是一个可复用的写作规则
```

在 `default` 模式下，显式抽取会走交互确认；在 `--accept-edits` 或 `--yolo` 下会更顺畅。

### 6. 手动创建 Skill

```text
/skill-create government-report-writing | 政府报告写作规范 | 用户要求写政府报告、工作汇报、调研报告、公文材料时使用 | 默认按标题、背景、问题、举措、成效、下一步计划组织内容；语言正式克制；信息不足时先生成可用初稿，并在末尾列出待补充信息。
```

### 7. 手动演化 Skill

```text
/skill-evolve government-report-writing 以后政府报告类任务不要使用口语化表达，优先使用正式、稳健、可汇报的句式。
```

### 8. 查看 Skills 和统计

```text
/skills
/skill-stats
```

## 常用命令

### CLI 参数


| 参数             | 功能                                        |
| ------------------ | --------------------------------------------- |
| `--model`, `-m`  | 指定模型，覆盖`.env` 中的 `MODEL`           |
| `--api-base`     | 覆盖 API base URL                           |
| `--plan`         | 只读规划模式                                |
| `--accept-edits` | 自动允许编辑类操作，推荐用于自动沉淀 Skills |
| `--yolo`, `-y`   | 跳过确认                                    |
| `--dont-ask`     | 自动拒绝需要确认的操作，适合 CI             |
| `--resume`       | 恢复最近会话                                |
| `--max-cost`     | 费用上限                                    |
| `--max-turns`    | 最大 agentic turns                          |

### REPL 命令


| 命令                                                                    | 功能                                                                 |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `/clear`                                                                | 清空对话历史                                                         |
| `/plan`                                                                 | 切换 Plan Mode                                                       |
| `/cost`                                                                 | 显示 token 和费用估算                                                |
| `/compact`                                                              | 手动压缩上下文                                                       |
| `/memory`                                                               | 列出长期记忆                                                         |
| `/skills`                                                               | 列出可用 Skills                                                      |
| `/skill-stats`                                                          | 查看 Skill 使用和演化统计                                            |
| `/skill-eval`                                                           | 在线 Skills 评测（replay、规则、LLM judge）                          |
| `/extract_now [hint]`                                                   | 抽取当前 pending window                                              |
| `/skill-feedback <skill> <rating> [note]`                               | 记录 Skill 反馈                                                      |
| `/skill-evolve <skill> <lesson>`                                        | 手动演化 Skill                                                       |
| `/skill-create <name> | <description> | <when-to-use> | <instructions>` | 手动创建 Skill                                                       |
| `/rewind [N]`                                                           | 回退最近 N 轮对话（默认 1），同时恢复被修改的文件                    |
| `/goal <目标>`                                                          | 自主目标模式：提炼成功标准 → 确认 → 执行+verifier 验证循环         |
| `/context`                                                              | 可视化上下文（index/role/内容摘要/字符数）                           |
| `/ctx del N [N2 ...]`                                                   | 删除指定消息组（支持批量：`/ctx del 1,3,5~10`）                      |
| `/ctx keep N [N2 ...]`                                                  | 只保留指定消息组，其余删除                                           |
| `/cd [path]`                                                            | 切换工作目录（Memory/Skills/规则按 cwd 隔离，切换后自动刷新 prompt） |
| `/help`                                                                 | 显示 REPL 命令列表                                                   |
| `/thinking`                                                             | 开关 thinking 显示（默认关闭，对齐 Claude Code / Codex CLI 惯例）    |

输入提示：

- 提示符上方状态行实时显示：当前模型、当前上下文 token/窗口（利用率）、会话累计 in/out tokens（注意：单位是 token，`/context` 表格显示的是字符数）。
- 输入中用 `@路径` 引用文件/目录，内容会自动注入本轮输入（单文件上限 32KB）。
- Tab 键自动补全 `/` 命令与 `@` 路径。
- 流式输出结束后自动擦除原文并用 rich 重渲染 Markdown（`BEAR_MD_RENDER=0` 可关闭）。
- `/` 开头但不是已知命令也不是可调用 skill 时直接报错，不会发给模型。

## Skills 是什么

Skill 是一个可复用能力说明文件，通常是一个带 frontmatter 的 `SKILL.md`。它保存的不是某次任务的具体内容，而是未来同类任务可复用的方法、规范、偏好或流程。

Skill 路径：

```text
用户级：~/.bear/skills/<skill_name>/SKILL.md
项目级：<project>/.bear/skills/<skill_name>/SKILL.md
```

示例：

```markdown
---
name: code_review
description: Review code changes with a bug-risk-first mindset.
when-to-use: When the user asks for code review.
user-invocable: true
context: inline
---

# Workflow

1. Read the relevant code first.
2. Lead with bugs, regressions, security risks, and missing tests.
3. Cite file paths and line numbers when possible.
4. Keep summary secondary to findings.
```

Skill 和 Memory 的区别：


| 类型   | 保存内容                               |
| -------- | ---------------------------------------- |
| Memory | 用户偏好、项目事实、历史决策、参考资料 |
| Skill  | 可复用任务流程、输出规范、领域方法     |

## MCP 支持

Bear Code 支持 MCP 外部工具扩展。MCP Server 可以通过 stdio JSON-RPC 暴露工具，Bear Code 会将其包装为 Agent 可调用工具。

配置来源：

```text
~/.bear/settings.json
<project>/.bear/settings.json
<project>/.mcp.json
```

配置示例：

```json
{
  "mcpServers": {
    "example": {
      "command": "python",
      "args": ["server.py"],
      "env": {}
    }
  }
}
```

工具命名规则：

```text
mcp__<serverName>__<toolName>
```

## Docker 运行

构建镜像：

```bash
docker build -t bear-code .
```

启动交互式会话：

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD:/workspace" \
  -v bear-code-sessions:/root/.bear-code \
  -v bear-code-memory:/root/.BearCode \
  bear-code
```

允许自动沉淀 Skills：

```bash
docker run --rm -it \
  --env-file .env \
  -e BEAR_AUTO_SKILL_EVOLUTION=1 \
  -e BEAR_AUTO_SKILL_TARGET=project \
  -v "$PWD:/workspace" \
  -v bear-code-sessions:/root/.bear-code \
  -v bear-code-memory:/root/.BearCode \
  bear-code --accept-edits
```

## 重要数据路径


| 数据              | 路径                                           |
| ------------------- | ------------------------------------------------ |
| 项目级 Skills     | `.bear/skills/<skill_name>/SKILL.md`           |
| 用户级 Skills     | `~/.bear/skills/<skill_name>/SKILL.md`         |
| Skills 自进化审计 | `.bear/skill-evolution/`                       |
| 长期记忆          | `~/.BearCode/projects/<project_hash>/memory/`  |
| 会话历史          | `~/.bear-code/sessions/`                       |
| 大工具结果        | `~/.bear-code/tool-results/`                   |
| Plan Mode 计划    | `~/.bear/plans/`                               |
| 文件回退快照      | `~/.bear-code/checkpoints/<session_id>/files/` |

## 文档入口

更完整的学习和展示材料在 `wiki/`：


| 文档                                                    | 内容                                                          |
| --------------------------------------------------------- | --------------------------------------------------------------- |
| [学习说明](wiki/从0到1学习了解项目.md)                  | 面向学习者的完整项目说明                                      |
| [架构设计](wiki/架构设计.md)                            | 系统分层、主链路、模块边界和数据流                            |
| [核心源码阅读指南](wiki/核心源码阅读指南.md)            | 按源码顺序学习 Agent Loop、工具、Skills、Memory、MCP 和自进化 |
| [技术亮点](wiki/技术亮点.md)                            | 技术亮点和核心代码讲解                                        |
| [Skills 自进化逻辑](wiki/Skills自进化逻辑与实现思路.md) | 自进化设计和实现取舍                                          |
| [简历包装](wiki/简历包装.md)                            | 简历 bullet、面试表达和项目包装                               |
| [升级文档](wiki/升级文档.md)                            | 2026-08 升级 Step 0–9 的全部改动与实现原理                   |

## 测试

```bash
pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q   # 113 例，全部离线，不碰真实数据/API
```

## 适合如何使用

Bear Code 适合：

- 学习 Claude Code 类工具的 Agent Loop 和工具调用机制。
- 学习如何做本地文件编辑型 Agent 的权限控制。
- 学习 Skills、Memory、MCP、子 Agent 如何接入同一个 Runtime。
- 扩展成个人 Coding Agent。
- 扩展成带长期记忆和可复用经验沉淀的领域 Agent。

如果只看一个核心点：Bear Code 是一个会把稳定用户反馈沉淀成 Skills 的 **自进化 Harness Agent**。
