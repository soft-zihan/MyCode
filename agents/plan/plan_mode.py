"""Plan Mode Manager — Plan 模式逻辑封装。

职责：
- Plan 目录管理（{plan_dir}/ 下存储 spec.md, tasks.md 等）
- Plan 模式提示词构建（从策略文件动态加载）
- Plan 区域解析
- Plan 系统集成
- 产物校验
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Awaitable, Any

from agents.plan.strategy_loader import load_strategy, VALID_STAGES, strategy_config_from_app_config
from agents.logging import print_error
from agents.plan.plan_manager import count_tasks, create_plan, get_plan, append_tasks_to_plan, add_artifact
from agents.plan.plan_models import PlanGranularity


# checkbox 任务行：捕获前导空白（缩进判定的基准）、状态标记、编号、描述。
_CHECKBOX_TASK_RE = re.compile(r"^([ \t]*)- \[([ x!~-])\]\s*(\d+)\.\s*(.+)$")

# 缩进子项（bullet）。只在「前导空白长于父任务行」时才算子项，见
# checkbox_tasks_to_structured 的缩进判定。
_CHECKBOX_SUBITEM_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+)$")

# 子项里的验收键 → 解析器认得的 `**验收**:` 标记。全角冒号同样认（中文 plan 里
# `验收：` 很常见，而 plan_manager._parse_structured_tasks 只认半角），英文键也认
# （与 validate_plan_artifacts 同时接受「验收标准」与 "Acceptance" 一个口径）。
# 刻意**只**特判验收：其余键值对原样留在块正文里，物化时整块进 TaskItem.detail，
# 而「哪些键算结构化字段」是解析器的契约，转换器不该替它扩展。
_ACCEPTANCE_SUBITEM_RE = re.compile(
    r"^(验收|acceptance)\s*[:：]\s*(.+)$", re.IGNORECASE)


class PlanModeManager:
    """Plan 模式逻辑管理器。"""

    def __init__(
        self,
        *,
        workspace: Path,
        session_id: str,
        plan_approval_fn: Callable[[str], Awaitable[bool]] | None = None,
        strategy_config: dict[str, str] | None = None,
    ):
        self.workspace = workspace
        self.session_id = session_id
        self.plan_approval_fn = plan_approval_fn
        self.strategy_config = strategy_config or {}
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
        """构建 Plan 模式系统提示词（从策略文件动态加载）。"""
        plan_dir = self._plan_dir or self.generate_plan_dir()

        # 加载各阶段策略（全局应用配置为底，会话级覆盖）
        cfg = {**strategy_config_from_app_config(), **self.strategy_config}
        grill_spec_content, _, _ = load_strategy(
            "grill-spec",
            cfg.get("grill-spec", "simple"),
            self.workspace,
            plan_dir,
        )
        tasks_content, _, _ = load_strategy(
            "tasks",
            cfg.get("tasks", "structured"),
            self.workspace,
            plan_dir,
        )

        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except files under {plan_dir}), run non-readonly tools, or make any changes to the system.

## Plan Directory: {plan_dir}

## 双轨规划 — 按任务复杂度选择轨道（二选一，不要混用）

### 轻量轨（默认，适合大多数任务）：单一 plan.md
分钟级~小时级、涉及文件较少的任务，只写一个文件：
- `{plan_dir}/plan.md` — 首行必须是单行 `# <标题>`（它会被 slug 化，成为这份计划永久目录的名字），随后包含以下小节：
  - `## 背景`（为什么做）
  - `## 方案`(怎么做)
  - `## 任务清单`（checkbox 格式，每行一个，编号从 1 开始）：
    ```
    - [ ] 1. 任务描述（一句话）
      - 验收: 一条能证明完成的命令
      - 改动: 涉及的文件/函数
      - 注意: 容易踩的坑、必须守住的约束
    ```
    缩进子项会被物化进 task_list 的 detail 与 acceptance：`验收` 成为完成判据，
    其余成为该任务的详细执行方案，在它成为焦点时自动注入上下文。
    **explore 与 grill 都是可选的**——需求清楚就直接写，不清楚才用 ask_user 追问。
  - `## 验收`（如何验证整体完成）

### 重量轨（复杂任务显式升级）：三文档
跨多模块、需要审计、跨会话的大任务，写三个文件：
- `{plan_dir}/spec.md` — 需求规格（必须含"验收标准"小节）
- `{plan_dir}/design.md` — 设计文档
- `{plan_dir}/tasks.md` — 任务清单

#### Phase 1: Grill + Spec（需求澄清 + 规格）

{grill_spec_content}

#### Phase 2: Tasks（任务拆分）

{tasks_content}

## 提交审批

Call exit_plan_mode when your plan is ready for user review.

## 追加模式
如果 plan 目录已存在，先 read_file 读取现有内容。保留已有规格/方案不变，在任务清单末尾追加新的 tasks。

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    def read_draft_artifacts(self) -> dict[str, Any]:
        """读取草稿产物并判定轨道。

        plan.md 存在且 spec.md 不存在 → 轻量轨（minimal）；否则重量轨（standard）。
        """
        result = {"granularity": "standard", "spec": "", "design": "", "tasks": "", "plan": ""}
        if not self._plan_dir:
            return result

        def _read(name: str) -> str:
            f = self._plan_dir / name
            return f.read_text(encoding="utf-8") if f.exists() else ""

        plan_md = _read("plan.md")
        spec = _read("spec.md")
        if plan_md and not spec:
            result["granularity"] = "minimal"
            result["plan"] = plan_md
        else:
            result["spec"] = spec
            result["design"] = _read("design.md")
            result["tasks"] = _read("tasks.md")
            result["plan"] = plan_md
        return result

    @staticmethod
    def checkbox_tasks_to_structured(content: str) -> str:
        """把 checkbox 任务清单转为结构化 tasks.md。

        输出形状是三个下游的契约，改造时不得破坏：
        - `plan_manager._parse_structured_tasks` 认 `### Task N:` 块与
          `- **验收**:` / `- **状态**:` 标记；
        - `plan_manager._update_task_status_in_file` 在块内替换首条 `**状态**:`；
        - `handle_plan_system_integration` 用 `"### Task" in structured_tasks`
          判定「轻量轨 plan.md 里确实有任务」。
        `<!-- TASKS START/END -->` 包裹与五个状态标记的映射照旧保留。

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
        """校验 Plan 产物完整性。

        Returns:
            {
                "valid": bool,
                "errors": list[str],
                "artifacts": {
                    "spec.md": {"exists": bool, "size": int},
                    "tasks.md": {"exists": bool, "size": int, "task_count": int},
                }
            }
        """
        errors: list[str] = []
        artifacts: dict[str, dict[str, Any]] = {}

        if not self._plan_dir:
            return {"valid": False, "errors": ["plan_dir not set"], "artifacts": {}, "granularity": "standard"}


        plan_path = self._plan_dir / "plan.md"
        spec_path = self._plan_dir / "spec.md"
        minimal = plan_path.exists() and not spec_path.exists()

        if minimal:
            # 轻量轨：plan.md 非空 + 含 checkbox 任务清单
            content = plan_path.read_text(encoding="utf-8")
            task_count = count_tasks(content)
            artifacts["plan.md"] = {"exists": True, "size": len(content), "task_count": task_count}
            if not content.strip():
                errors.append("plan.md 为空")
            if task_count == 0:
                errors.append("plan.md 缺少任务清单（checkbox 格式：- [ ] 1. 任务描述）")
        else:
            # 重量轨：spec.md（含验收标准）+ tasks.md（任务数 > 0）
            if spec_path.exists():
                spec_content = spec_path.read_text(encoding="utf-8")
                has_acc = "验收标准" in spec_content or "Acceptance" in spec_content
                artifacts["spec.md"] = {"exists": True, "size": len(spec_content), "has_acceptance_criteria": has_acc}
                if not has_acc:
                    errors.append("spec.md 缺少验收标准")
            else:
                artifacts["spec.md"] = {"exists": False, "size": 0}
                errors.append("spec.md 不存在（重量轨需 spec.md+tasks.md；轻量轨请写 plan.md）")

            tasks_path = self._plan_dir / "tasks.md"
            if tasks_path.exists():
                tasks_content = tasks_path.read_text(encoding="utf-8")
                task_count = count_tasks(tasks_content)
                artifacts["tasks.md"] = {"exists": True, "size": len(tasks_content), "task_count": task_count}
                if task_count == 0:
                    errors.append("tasks.md 没有定义任何任务")
            else:
                artifacts["tasks.md"] = {"exists": False, "size": 0, "task_count": 0}
                errors.append("tasks.md 不存在")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "artifacts": artifacts,
            "granularity": "minimal" if minimal else "standard",
        }

    def handle_plan_system_integration(
        self,
        spec_content: str,
        plan_content: str,
        tasks_content: str,
        session: Any,
        granularity: str = "standard",
        plan_md: str = "",
    ) -> dict | None:
        """自动创建或追加 Plan 系统条目。

        granularity="minimal"：轻量轨，plan.md 为源文档，
        checkbox 任务清单转换为结构化 tasks.md（执行状态机依赖）。

        返回 None 意味着**什么都没落地**：plan 系统里没有条目、session.plan_slug 没设、
        session/plan_linked 没发，调用方因此也不会物化任务进 task_list。调用方必须把
        这件事说给模型和用户听——批准此时已经生效、permission_mode 已经切换，回滚不了，
        唯一能做的是别宣称一切正常（见 plan_tool_executor._PLAN_NOT_RECORDED）。
        """

        if granularity == "minimal":
            if not plan_md.strip():
                return None
            structured_tasks = self.checkbox_tasks_to_structured(plan_md)
            if "### Task" not in structured_tasks:
                return None
            tasks_content = tasks_content or structured_tasks
        elif not tasks_content:
            return None

        slug = ""
        if granularity == "minimal":
            title_match = re.search(r"^#\s+(.+)", plan_md, re.M)
            if title_match:
                slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]
        elif spec_content:
            title_match = re.search(r"# Spec:\s*(.+)", spec_content)
            if title_match:
                slug = re.sub(r"[^a-z0-9]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]

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
            # 兜底路径**不是**奇异情况：轻量轨的标题正则只认 H1（`^#\s+`，不匹配
            # `##`），重量轨只认 `# Spec:`，两者都可能没写 → 每次都会走到这里。
            # 曾经这里写的就是 `f"plan-{self.session_id}"`，于是默认轨道上的批准
            # 100% 静默失效（一行 stderr 是唯一痕迹）。别把它「简化」回去。
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
            plan_dir = create_plan(
                slug=slug,
                granularity=PlanGranularity.MINIMAL if granularity == "minimal" else PlanGranularity.STANDARD,
            )

            if granularity == "minimal":
                add_artifact(slug, "plan.md", plan_md)
            else:
                if spec_content:
                    add_artifact(slug, "spec.md", spec_content)
                if plan_content:
                    add_artifact(slug, "design.md", plan_content)

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
