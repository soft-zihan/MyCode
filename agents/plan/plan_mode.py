"""Plan Mode Manager — Plan 模式逻辑封装。

职责：
- Plan 目录管理（{plan_dir}/ 下唯一产物 plan.md——单轨，重量轨已删）
- Plan 模式提示词构建
- checkbox 任务清单 → 结构化 tasks.md 转换（物化侧解析器的上游）
- 产物格式校验（spec §五：四条硬规则 + 警告通道）
- Plan 系统集成（批准后落库）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Awaitable, Any

from agents.logging import print_error
from agents.plan.plan_manager import (
    count_tasks, create_plan, get_plan, append_tasks_to_plan, add_artifact,
    parse_tasks_content,
)
from agents.plan.plan_models import PlanGranularity


# checkbox 任务行：捕获前导空白（缩进判定的基准）、状态标记、编号、描述。
_CHECKBOX_TASK_RE = re.compile(r"^([ \t]*)- \[([ x!~-])\]\s*(\d+)\.\s*(.+)$")

# 缩进子项（bullet）。只在「前导空白长于父任务行」时才算子项，见
# checkbox_tasks_to_structured 的缩进判定。
_CHECKBOX_SUBITEM_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+)$")

# 子项里的验收键 → 解析器认得的 `**验收**:` 标记。全角冒号同样认（中文 plan 里
# `验收：` 很常见，而 plan_manager._parse_structured_tasks 只认半角），英文键也认
# （`acceptance:`，与提示词的中文键同一口径）。
# 刻意**只**特判验收：其余键值对原样留在块正文里，物化时整块进 TaskItem.detail，
# 而「哪些键算结构化字段」是解析器的契约，转换器不该替它扩展。
_ACCEPTANCE_SUBITEM_RE = re.compile(
    r"^(验收|acceptance)\s*[:：]\s*(.+)$", re.IGNORECASE)

# 首行 H1 标题（validate_plan_artifacts 硬规则 1）。markdown 允许标题前最多 3 个
# 空格缩进（4 个起是代码块），所以容忍 ` {0,3}`；`##` 匹配不到——第一个 `#` 后面
# 必须是空白。必须与 handle_plan_system_integration 的 slug 提取正则保持同一口径：
# 校验放行的标题，slug 必须提得出来，否则又退回 approved- 兜底那条降级路径。
_H1_TITLE_RE = re.compile(r"^ {0,3}#\s+\S")

# `## 任务清单` 小节标题（validate_plan_artifacts 硬规则 2）。必须是
# plan_tool_executor._TASK_LIST_HEADING_RE 的**子集**：校验器放行的标题，
# _strip_task_checklist_section 必须切得动，否则整份清单会重复进对话。
# 子集关系：` {0,3}` ⊆ `\s{0,3}`，`##` ⊆ `#{2,}`，其余逐字相同。
_CHECKLIST_HEADING_RE = re.compile(r"^ {0,3}##[ \t]*任务清单", re.M)


class PlanModeManager:
    """Plan 模式逻辑管理器。"""

    def __init__(
        self,
        *,
        workspace: Path,
        session_id: str,
        plan_approval_fn: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self.workspace = workspace
        self.session_id = session_id
        self.plan_approval_fn = plan_approval_fn
        self._plan_dir: Path | None = None
        self._pre_plan_mode: str | None = None

    def generate_plan_dir(self) -> Path:
        """生成 Plan 草稿目录路径。

        这里的 `plan-` 前缀是**草稿**命名空间，必须与
        handle_plan_system_integration 的兜底 slug（`approved-` 前缀）保持互斥，
        理由见那处的注释。改动任一侧之前先读另一侧。
        """
        d = self.workspace / ".mycode" / "plans" / f"plan-{self.session_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def build_plan_mode_prompt(self) -> str:
        """构建 Plan 模式系统提示词（单轨：唯一产物 plan.md）。

        这段文本是**尾部 ephemeral system 消息**（prompt_runtime.build_tail_system_messages
        每请求现读重拼），不进 system[0]、也不落盘。两个后果都要记住：
        - 进出 plan 模式对 prefix cache **中性**（尾部在对话历史之后，不参与前缀命中）；
        - 它**不进历史**，所以模型看不到「上一轮 plan 模式说过什么」，每次调用都是全新一份。
          这也是为什么本提示词必须自包含，不能依赖「之前已经讲过」。
        """
        plan_dir = self._plan_dir or self.generate_plan_dir()

        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except files under {plan_dir}), run non-readonly tools, or make any changes to the system.

## Plan Directory: {plan_dir}

## 唯一产物：`{plan_dir}/plan.md`

任务是轻是重你自己判断，产物只有这一个文件。结构：

- **首行必须是单行 `# <标题>`**——它会被 slug 化，成为这份计划永久目录的名字。
  写成 `## 标题` 或者漏写，批准后的落库会失败。
- `## 背景`（为什么做）
- `## 方案`（怎么做）
- `## 任务清单`——**必须是这个精确标题**（切分逻辑只认它），checkbox 格式，编号从 1 开始：

    ```
    - [ ] 1. 任务描述（一句话）
      - 验收: 一条能证明完成的命令
      - 改动: 涉及的文件/函数
      - 注意: 容易踩的坑、必须守住的约束
    ```

  缩进子项会被物化进 task_list 的 detail 与 acceptance：`验收` 成为完成判据，
  其余成为该任务的详细执行方案，在它成为焦点时自动注入上下文。
  **每条任务都必须有 `验收:`**——缺了它，这条任务永远无法被检验。
- `## 验收`（如何验证整体完成）

## 需求不清楚时才追问

需求清楚就直接写，不清楚才用 ask_user 追问。追问用「编号问题 + 推荐答案」的格式，
让用户一次答完，而不是一轮挤一个问题：

```
❓ Q1 - 问题描述：选项 A 还是选项 B？
➡️ 推荐：选项 A（理由）

❓ Q2 - ...
➡️ 推荐：...
```

## 本模式下的限制

- **只读**：除 `{plan_dir}` 下的文件外不能写任何东西，不能跑非只读命令。
- **`task_list` 不可用**：计划批准后由**系统**自动物化进 task_list，不用你建。

## 追加模式
如果 plan 目录已存在，先 read_file 读取现有内容。保留已有背景/方案不变，在任务清单末尾追加新的 tasks。

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    def read_draft_artifacts(self) -> dict[str, str]:
        """读取草稿产物（单轨：唯一产物 plan.md）。

        旧会话草稿目录里残留的 spec.md / design.md / tasks.md 刻意不读：重量轨已删，
        读进来也没有消费者（sessions.py 的 _DRAFT_ARTIFACT_NAMES 已同步收缩）。
        """
        if not self._plan_dir:
            return {"plan": ""}
        f = self._plan_dir / "plan.md"
        return {"plan": f.read_text(encoding="utf-8") if f.exists() else ""}

    @staticmethod
    def checkbox_tasks_to_structured(content: str) -> str:
        """把 checkbox 任务清单转为结构化 tasks.md。

        输出形状是两个下游的契约，改造时不得破坏：
        - `plan_manager._parse_structured_tasks` 认 `### Task N:` 块与
          `- **验收**:` / `- **状态**:` 标记；
        - `handle_plan_system_integration` 用 `"### Task" in structured_tasks`
          判定「轻量轨 plan.md 里确实有任务」。
        `<!-- TASKS START/END -->` 包裹与五个状态标记的映射照旧保留。

        （曾经还有第三个下游：`plan_manager._update_task_status_in_file`，在块内
        替换首条 `**状态**:`。它随 Plan 3b Task A1 删掉的 8 个 plan REST 端点
        一起消失了——逐条任务的执行状态现在只住在 task_list 里，不再回写
        tasks.md。）

        **缩进子项不再丢弃**（Plan 3a Task 2）：此前只把 `- [ ] N. 描述` 转成
        `### Task N: 描述` + 状态，缩进子项整段扔掉——而解析器其实早就支持
        `**验收**:`。不补这个洞，轻量轨 plan 物化出来的 detail 与 acceptance 全是
        空的，渐进式披露与验收闸门双双空转。现在 `验收:` 映射成解析器认得的
        `- **验收**:`，其余键值对与无键 bullet 原样留在块正文里，物化时整块进
        TaskItem.detail。

        缩进判定是**相对的**（前导空白长于父任务行），不是固定两空格：四空格、
        tab、以及嵌在别的列表里的任务清单都得认。同级或更浅的非空行结束当前块，
        否则紧跟在任务清单后面的 `## 验收` 小节会被吸进最后一个任务。空行不打断
        （清单里常见）；更深但不是 bullet 的行（续行说明）跳过且**不**关块，好让
        它后面的子项仍能归位。
        """
        status_map = {"x": "done", "!": "failed", "~": "in-progress", "-": "skipped"}
        icon_map = {"pending": "[ ]", "in-progress": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}

        # [编号, 描述, 状态, 子项行]——用 list 而不是 tuple：子项要就地 append
        blocks: list[list] = []
        parent_indent = -1          # < 0 = 当前不在任何任务块里

        for raw in content.split("\n"):
            line = raw.rstrip("\r")  # 文件是 CRLF 时 split("\n") 会留下尾随 \r

            m = _CHECKBOX_TASK_RE.match(line)
            if m:
                indent, marker, tid, desc = m.groups()
                blocks.append([
                    tid,
                    desc.strip(),
                    status_map.get(marker, "pending"),
                    [],
                ])
                parent_indent = len(indent)
                continue

            if parent_indent < 0 or not line.strip():
                continue

            if len(line) - len(line.lstrip(" \t")) <= parent_indent:
                parent_indent = -1   # 同级或更浅的非空行：块结束
                continue

            sub = _CHECKBOX_SUBITEM_RE.match(line)
            if not sub:
                continue
            text = sub.group(1).strip()
            acc = _ACCEPTANCE_SUBITEM_RE.match(text)
            blocks[-1][3].append(
                f"- **验收**: {acc.group(2).strip()}" if acc else f"- {text}")

        out = ["<!-- TASKS START -->"]
        for tid, desc, status, subitems in blocks:
            out.append(f"### Task {tid}: {desc}")
            out.append(f"- **状态**: {icon_map[status]} {status}")
            out.extend(subitems)
            out.append("")
        out.append("<!-- TASKS END -->")
        return "\n".join(out) + "\n"

    def validate_plan_artifacts(self) -> dict[str, Any]:
        """校验 plan.md 的格式（单轨，spec §五：四条硬规则 + 警告通道）。

        这是 B 批次 turn 收尾送审钩子的判定输入，返回形状是契约：

        Returns:
            {
                "valid": bool,          # 有任一硬错误即 False（不送审）
                "errors": list[str],    # 硬错误。文本必须**可直接执行**：指明第几条
                                        # 任务、缺什么、正确写法长什么样——模型自我
                                        # 修正的成功率取决于这个，比规则本身更重要
                "warnings": list[str],  # 进诊断、不拦送审
                "task_count": int,      # count_tasks 数出的 checkbox 任务数
            }

        刻意**不**自带一套任务解析：计数复用 count_tasks（checkbox 分支），逐条
        验收检查复用 checkbox_tasks_to_structured → parse_tasks_content——物化走的
        正是后面这条链。两个解析器必然漂移，而漂移的后果是「校验器放行的计划物化出
        另一个条数」；test_plan_system.py 有一条测试把 task_count 与转换产物钉在一起。
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not self._plan_dir:
            return {"valid": False, "errors": ["plan_dir 未设置（尚未进入 plan 模式）"],
                    "warnings": [], "task_count": 0}

        plan_path = self._plan_dir / "plan.md"
        if not plan_path.exists():
            # spec §十.1：模型只在对话里输出计划、不写文件。宽容接受会断掉
            # 「计划落盘、可审批、可物化」这条链，所以硬报错并说清该写哪。
            return {
                "valid": False,
                "errors": [f"plan.md 不存在：计划必须写进 {plan_path}，"
                           f"只在对话里输出的计划不会被送审。"],
                "warnings": [], "task_count": 0,
            }

        content = plan_path.read_text(encoding="utf-8")
        if not content.strip():
            return {
                "valid": False,
                "errors": [f"plan.md 为空：把计划写进 {plan_path}"
                           f"（首行单行 `# <标题>`，任务写进 `## 任务清单` 小节）。"],
                "warnings": [], "task_count": 0,
            }

        # 规则 1：首行（首个非空行）必须是单行 `# <标题>`，容忍 markdown 允许的最多
        # 3 个空格缩进。它是 slug 的唯一来源——Plan 3a 的真 bug：`^#\s+` 匹配不到
        # `##`，兜底 slug 与草稿目录同名撞车，create_plan 抛错被吞成 None，物化
        # **静默什么都不落地**。所以 `##` 必须在这里被拦下，而不是等批准后发现。
        first_line = next((ln for ln in content.split("\n") if ln.strip()), "")
        if not _H1_TITLE_RE.match(first_line):
            errors.append(
                "plan.md 首行必须是单行 `# <标题>`（最多 3 个空格缩进；`## <标题>` "
                "不算）——标题会被 slug 化成这份计划的永久目录名，缺了它落库只能走"
                "降级兜底。"
            )

        # 规则 2：必须有 `## 任务清单` 小节（精确标题）。切分逻辑
        # （_strip_task_checklist_section）只认它，切不动时整份清单会重复进对话。
        if not _CHECKLIST_HEADING_RE.search(content):
            errors.append(
                "缺少 `## 任务清单` 小节：标题必须逐字是 `## 任务清单`"
                "（写成 `## Tasks`、`## 任务` 都不行，清单切分逻辑只认这个标题）。"
            )

        # 规则 3：至少一条 `- [ ] N. <描述>`。计数用 count_tasks，不自写正则。
        task_count = count_tasks(content)
        # 规则 4：每条任务必须有 `验收:` 缩进子项——它是完成判据，也是软验收闸门的
        # 输入，缺了它这条任务永远无法被闸门检验。逐条检查走物化侧同一套解析
        # （转换 → parse_tasks_content），不另写块解析器。
        structured = self.checkbox_tasks_to_structured(content)
        tasks = parse_tasks_content(structured)
        if task_count == 0 or not tasks:
            errors.append(
                "plan.md 没有任务：`## 任务清单` 小节下至少要有一条 checkbox 任务，"
                "正确写法：\n- [ ] 1. 任务描述（一句话）\n  - 验收: 一条能证明完成的命令"
            )
        for t in tasks:
            if not (t.acceptance or "").strip():
                errors.append(
                    f"任务 {t.id}（{t.description}）缺少 `验收:` 缩进子项。正确写法：\n"
                    f"- [ ] {t.id}. {t.description}\n"
                    f"  - 验收: 一条能证明完成的命令"
                )

        # 编号连续性只是警告（spec §五.3）：物化用的是清单**顺序**而不是编号，
        # 把编号做成硬错误只会制造无谓的送审失败（追加模式下模型接着旧编号往下写
        # 是常见且无害的，同理不从 1 开始也不拦）。
        ids = [t.id for t in tasks]
        if ids and ids != list(range(1, len(ids) + 1)):
            warnings.append(
                f"任务编号应从 1 开始且连续（当前：{', '.join(map(str, ids))}）。"
                "不影响物化（物化按清单顺序），但建议修正。"
            )

        # 可选小节缺失 → 警告，不拦送审（spec §五）。
        for name, why in (("背景", "为什么做"), ("方案", "怎么做"), ("验收", "如何验证整体完成")):
            if not re.search(rf"^ {{0,3}}##[ \t]*{name}", content, re.M):
                warnings.append(f"缺少 `## {name}` 小节（{why}）——建议补充。")

        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "task_count": task_count,
        }

    def handle_plan_system_integration(
        self,
        plan_md: str,
        session: Any,
    ) -> dict | None:
        """自动创建或追加 Plan 系统条目（单轨：plan.md 是唯一源文档）。

        checkbox 任务清单会被转换成结构化 tasks.md（物化与冻结文档都依赖这个形状），
        plan.md 与 tasks.md 一起落进永久目录。

        返回 None 意味着**什么都没落地**：plan 系统里没有条目、session.plan_slug 没设、
        session/plan_linked 没发，调用方因此也不会物化任务进 task_list。调用方必须把
        这件事说给模型和用户听——批准此时已经生效、permission_mode 已经切换，回滚不了，
        唯一能做的是别宣称一切正常（见 plan_tool_executor._PLAN_NOT_RECORDED）。
        """

        if not plan_md.strip():
            return None
        structured_tasks = self.checkbox_tasks_to_structured(plan_md)
        if "### Task" not in structured_tasks:
            return None
        tasks_content = structured_tasks

        slug = ""
        # 与 validate_plan_artifacts 规则 1 同一口径（容忍最多 3 个空格缩进）：
        # 校验器放行的 H1 必须在这里提得出 slug，不然 approved- 兜底会悄悄接管。
        title_match = re.search(r"^ {0,3}#\s+(.+)", plan_md, re.M)
        if title_match:
            slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]

        if not slug:
            # 兜底 slug 必须与 generate_plan_dir() 的**草稿目录名**结构性不同名。
            #
            # 不变量：`approved-{session_id}` 与 `plan-{session_id}` 两套前缀互斥，
            # 谁都不许改成对方。原因是这两半会在同一个 `.mycode/plans/` 下相遇，而
            # create_plan 遇到已存在的目录直接抛 ValueError：
            #   generate_plan_dir() 在 build_plan_mode_prompt 里就把草稿目录 mkdir
            #   出来了 → 若兜底 slug 与之同名，create_plan **必然**抛 → 下面的
            #   blanket except 吞掉它返回 None → _finalize_plan_exit 的
            #   `if plan_result and plan_result.get("slug")` 为假 → 不物化、不清草稿、
            #   不发 session/plan_linked，消息里只剩「像成功」的一句话。
            # 曾经这里写的就是 `f"plan-{self.session_id}"`，于是默认轨道上的批准
            # 100% 静默失效（一行 stderr 是唯一痕迹）。别把它「简化」回去。
            #
            # A3 之后校验器把「首行必须 H1」做成了硬规则（validate_plan_artifacts
            # 规则 1），正常送审路径到不了这里；兜底仍必须存在——integration 可能被
            # 绕过校验器直接调用，且标题可能 slug 化成空串（全是标点/emoji）。
            slug = f"approved-{self.session_id}"

        if session.plan_slug:
            existing_plan = get_plan(session.plan_slug)
            if existing_plan and existing_plan.status.value not in ("archived", "abandoned"):
                try:
                    append_tasks_to_plan(session.plan_slug, tasks_content)
                    return {"slug": session.plan_slug, "action": "appended"}
                except Exception as e:
                    # slug 与异常类型都要在：调用方拿到 None 之后，这一行是唯一的现场。
                    print_error(
                        f"[plan] failed to append tasks to plan "
                        f"'{session.plan_slug}': {e!r}"
                    )
                    return None

        try:
            # 单轨一律 MINIMAL。PlanGranularity 枚举与 plan_manager 侧的读写是盘上
            # _meta.md 的反序列化面（旧目录里躺着 `granularity: standard`），刻意
            # 不动；这里只是不再有别的选择。
            plan_dir = create_plan(slug=slug, granularity=PlanGranularity.MINIMAL)

            add_artifact(slug, "plan.md", plan_md)
            add_artifact(slug, "tasks.md", tasks_content)

            session.plan_slug = slug
            session.append("session/plan_linked", {"plan_slug": slug})

            return {"slug": slug, "action": "created", "plan_dir": str(plan_dir)}
        except Exception as e:
            # 这一行是 None 的唯一现场，必须够诊断：slug（碰撞时它就是答案）+ 异常
            # 类型与消息（`{e}` 会丢掉类型，而 ValueError("already exists") 与
            # OSError 的处置完全不同）+ 一句「什么都没落地」，免得读日志的人以为
            # 只是少写了个 artifact。
            print_error(
                f"[plan] failed to create plan '{slug}' — nothing was recorded and no "
                f"task was materialized into task_list: {e!r}"
            )
            return None

    @property
    def plan_dir(self) -> Path | None:
        return self._plan_dir

    @plan_dir.setter
    def plan_dir(self, value: Path | None) -> None:
        self._plan_dir = value

    @property
    def pre_plan_mode(self) -> str | None:
        return self._pre_plan_mode

    @pre_plan_mode.setter
    def pre_plan_mode(self, value: str | None) -> None:
        self._pre_plan_mode = value
