from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace
from agents.core.frontmatter import parse_frontmatter
from .skill_file_ops import (
    create_skill_file,
    evolve_skill_file,
    format_skill_stats,
    record_online_skill_provenance,
    record_skill_feedback,
    record_skill_invocation,
)
from agents.observability.trace import trace_event


@dataclass
class SkillDefinition:
    # 一个 skill 在程序内的统一表示，由 SKILL.md 的 frontmatter 和正文解析得到。
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork"
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""
    # fork 模式下子 Agent 的模型引用（端点 ID 或裸模型名）；空串表示继承父端点。
    model: str = ""
    # 可执行 skill 标记（包含 pyproject.toml）
    executable: bool = False
    # 可执行 skill 的 import name
    import_name: str = ""
    # Hook 机制：关键词匹配直接召回
    hook_keywords: list[str] | None = None  # 触发关键词列表
    hook_mode: str = "inject"  # "inject" | "inline" | "fork"


# skills 按工作区缓存：首次读取时扫描磁盘，后续复用；修改 skill 后需要 reset。
# 服务器多会话并发时各会话工作区不同，必须按 workspace 键控，否则项目级 skills 串台。
_skills_cache: dict[Path, list[SkillDefinition]] = {}


def execute_skill(skill_name: str, args: object, substitutions: dict[str, str] | None = None) -> dict | None:
    # skill 工具的执行入口：按名字找到 skill，并返回解析后的 prompt 和执行配置。
    # substitutions：调用方运行时变量（U9：${SESSION_ID}/${ARTIFACTS_DIR}）。
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None

    record_skill_invocation(
        skill_name=skill.name,
        source=skill.source,
        context=skill.context,
        args=args,
    )

    return {
        "prompt": resolve_skill_prompt(skill, args, substitutions),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
        "source": skill.source,
        "skill_dir": skill.skill_dir,
        "model": skill.model,
    }



def resolve_skill_prompt(skill: SkillDefinition, args: object, substitutions: dict[str, str] | None = None) -> str:
    import re
    prompt = skill.prompt_template
    # 支持在 SKILL.md 正文中使用 $ARGUMENTS 或 ${ARGUMENTS} 引用用户参数。
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", str(args or ""), prompt)
    # 支持 skill 引用自己的目录，例如读取同目录下的 references/scripts。
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    # U9：调用方注入的运行时变量（如 ${SESSION_ID}/${ARTIFACTS_DIR}）
    for key, value in (substitutions or {}).items():
        prompt = prompt.replace(key, value)
    return prompt

def get_skill_by_name(skill_name:str)->SkillDefinition | None:
    # 通过 name 查找 skill；name 来自 frontmatter，没有写时使用目录名。
    for s in discover_skills():
        if s.name == skill_name:
            return s
    return None

def discover_skills() -> list[SkillDefinition]:
    workspace = get_workspace().resolve()
    cached = _skills_cache.get(workspace)
    if cached is not None:
        return cached

    skills: dict[str,SkillDefinition] = {}
    # 用户级 skills 优先级最高：~/.mycode/skills/<name>/SKILL.md
    user_dir = Path.home() / ".mycode" / "skills"
    _load_skills_from_dir(user_dir, "user", skills)
    # 用户级 skills（agents 目录）：~/.agents/skills/<name>/SKILL.md
    agents_dir = Path.home() / ".agents" / "skills"
    _load_skills_from_dir(agents_dir, "user", skills, overwrite=False)
    # 项目级 skills 优先级较低：<workspace>/.mycode/skills/<name>/SKILL.md
    project_dir = workspace / ".mycode" / "skills"
    _load_skills_from_dir(project_dir, "project", skills, overwrite=False)
    # 内置命令资产优先级最低（v2 plugin/command/*.txt 对齐：/initialize、/review），
    # 用户级/项目级同名 skill 可覆盖。
    builtin_dir = Path(__file__).resolve().parent.parent / "prompts" / "commands"
    _load_skills_from_dir(builtin_dir, "builtin", skills, overwrite=False)
    # 内置功能 skill（U9：visualization 图表生成），同为最低优先级可覆盖。
    builtin_skills_dir = Path(__file__).resolve().parent.parent / "prompts" / "skills"
    _load_skills_from_dir(builtin_skills_dir, "builtin", skills, overwrite=False)

    result = list(skills.values())

    # 加载可执行 skills
    _load_executable_skills(result)

    _skills_cache[workspace] = result
    return result


def _load_executable_skills(skills: list[SkillDefinition]) -> None:
    """加载所有可执行 skills 到命名空间。"""
    executable_dirs = [
        Path(s.skill_dir) for s in skills
        if s.executable and s.skill_dir
    ]
    if not executable_dirs:
        return

    from .executable_skills import load_executable_skills
    result = load_executable_skills(executable_dirs)

    trace_event(
        "skill.load_batch",
        metadata={
            "total": result.get("total", 0),
            "loaded": result.get("loaded", 0),
            "failed": result.get("failed", 0),
        },
    )

def _load_skills_from_dir( base_dir: Path, source: str, skills:dict[str, SkillDefinition], overwrite: bool = True) -> None:
    # 只加载目录形式的 skill，不加载 .mycode/skills/foo.md 这种单文件形式。
    if not base_dir.is_dir():
        return
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        # 文件名必须是 SKILL.md，大小写要一致。
        skill_file = entry/  "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry))
        if skill:
            # 项目级加载时 overwrite=False，避免覆盖同名用户级 skill。
            if not overwrite and skill.name in skills:
                continue
            skills[skill.name] = skill

def _parse_skill_file(file_path: Path, source: str, skill_dir: str) -> SkillDefinition:
    try:
        # SKILL.md = frontmatter 配置 + markdown 正文。
        raw = file_path.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta

        # name 没写时用目录名；user-invocable 默认 true；context 默认 inline。
        name = meta.get("name") or file_path.parent.name or "unknown"
        user_invocable = meta.get("user-invocable", "true") != "false"
        context = "fork" if meta.get("context") == "fork" else "inline"

        # 检测是否为可执行 skill（包含 pyproject.toml）
        from .executable_skills import is_executable_skill, get_import_name
        skill_dir_path = Path(skill_dir)
        executable = is_executable_skill(skill_dir_path)
        import_name = get_import_name(name) if executable else ""

        allowed_tools: list[str] | None = None
        if "allowed-tools" in meta:
            raw_tools = meta["allowed-tools"]
            # allowed-tools 支持 JSON 数组字符串，也支持逗号分隔。
            if raw_tools.startswith("["):
                try:
                    allowed_tools = json.loads(raw_tools)
                except Exception:
                    allowed_tools = [s.strip() for s in raw_tools.strip("[]").split(",")]
            else:
                allowed_tools = [s.strip() for s in raw_tools.split(",")]

        # Hook 机制：解析 hook-keywords 和 hook-mode
        hook_keywords: list[str] | None = None
        if "hook-keywords" in meta:
            raw_keywords = meta["hook-keywords"]
            # hook-keywords 支持 JSON 数组字符串，也支持逗号分隔
            if raw_keywords.startswith("["):
                try:
                    hook_keywords = json.loads(raw_keywords)
                except Exception:
                    hook_keywords = [s.strip() for s in raw_keywords.strip("[]").split(",")]
            else:
                hook_keywords = [s.strip() for s in raw_keywords.split(",")]
            # 过滤空字符串
            hook_keywords = [k for k in hook_keywords if k]
        
        hook_mode = str(meta.get("hook-mode") or "inject").strip().lower()
        if hook_mode not in ("inject", "inline", "fork"):
            hook_mode = "inject"

        return SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed_tools,
            user_invocable=user_invocable,
            context=context,
            prompt_template=result.body,
            source=source,
            skill_dir=skill_dir,
            model=str(meta.get("model") or "").strip(),
            executable=executable,
            import_name=import_name,
            hook_keywords=hook_keywords,
            hook_mode=hook_mode,
        )

    except Exception:
        return None


def build_skill_descriptions() -> str:
    # 把已加载的 skills 写进 system prompt，让模型知道哪些 skill 可用。
    skills = discover_skills()

    lines = ["# Available Skills", ""]
    if not skills:
        lines.append("(No skills are currently registered.)")
        lines.append("")
    # user_invocable=True 的 skill 主要给用户通过 /<name> 手动调用。
    invocable = [s for s in skills if s.user_invocable]
    # user_invocable=False 的 skill 作为自动调用候选，模型根据 when_to_use 决定是否调用 skill 工具。
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills:")
        lines.append("When the user's request matches a skill's When to use, call the `skill` tool with that skill name before continuing. Do not ask the user to invoke it manually.")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    lines.append("To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments.")
    lines.append("")
    lines.append("# Skill Evolution")
    lines.append("MyCode has an online skill evolution loop after each assistant response. Do not create or evolve skills during normal task execution unless the user explicitly asks for manual skill maintenance.")
    lines.append("If manual maintenance is explicitly requested, call `skill_evolve` only for durable reusable feedback on an existing skill, and call `skill_create` only when no suitable existing skill exists.")
    lines.append("Never create or evolve skills from one-off task content, private secrets, temporary project facts, or assistant-only guesses.")
    return "\n".join(lines)


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


def _tokens(text: str) -> set[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    found = {m.group(0) for m in _TOKEN_RE.finditer(raw)}
    expanded = set(found)
    for token in found:
        if len(token) > 3 and token.endswith("s"):
            expanded.add(token[:-1])
    found = expanded
    cjk = re.findall(r"[\u4e00-\u9fff]+", raw)
    for chunk in cjk:
        if len(chunk) >= 2:
            found.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return {x for x in found if x.strip() and x not in _STOP_TOKENS}


def _token_list(text: str) -> list[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(raw)]
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    expanded: list[str] = []
    for token in tokens:
        if not token.strip() or token in _STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def _skill_search_text(skill: SkillDefinition) -> str:
    return "\n".join(
        [
            skill.name,
            skill.description,
            skill.when_to_use or "",
            skill.prompt_template[:4000],
        ]
    )


def retrieve_relevant_skills(
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.08,
) -> list[dict[str, Any]]:
    query_terms = _token_list(query)
    query_tokens = set(query_terms)
    if not query_tokens:
        return []

    docs: list[tuple[SkillDefinition, list[str]]] = []
    document_frequency: Counter[str] = Counter()
    for skill in discover_skills():
        meta_terms = _token_list("\n".join([skill.name, skill.description, skill.when_to_use or ""]))
        body_terms = _token_list(skill.prompt_template[:2500])
        terms = (meta_terms * 3) + body_terms
        if not terms:
            continue
        docs.append((skill, terms))
        document_frequency.update(set(terms))
    if not docs:
        return []

    avg_doc_len = sum(len(terms) for _, terms in docs) / max(1, len(docs))
    doc_count = len(docs)
    k1 = 1.4
    b = 0.75
    hits: list[dict[str, Any]] = []
    for skill, terms in docs:
        term_counts = Counter(terms)
        overlap = query_tokens & set(term_counts)
        if not overlap:
            continue
        raw_score = 0.0
        doc_len = max(1, len(terms))
        for token in overlap:
            tf = term_counts[token]
            idf = math.log(1 + (doc_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denom = tf + k1 * (1 - b + b * doc_len / max(1.0, avg_doc_len))
            raw_score += idf * (tf * (k1 + 1)) / max(denom, 0.0001)
        name_bonus = 0.15 if skill.name.lower() in str(query or "").lower() else 0.0
        score = min(1.0, (raw_score / max(3.0, len(query_tokens))) + name_bonus)
        if score < float(min_score):
            continue
        hits.append(
            {
                "score": float(score),
                "name": skill.name,
                "description": skill.description,
                "when_to_use": skill.when_to_use or "",
                "source": skill.source,
                "context": skill.context,
                "user_invocable": bool(skill.user_invocable),
                "skill_dir": skill.skill_dir,
            }
        )

    hits.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return hits[: max(1, int(limit or 1))]


def reset_skill_cache() -> None:
    # 测试或运行中刷新 skills 时使用；普通用户通常重启程序即可。
    _skills_cache.clear()


def evolve_skill(
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
) -> dict:
    skill = get_skill_by_name(skill_name)
    result = evolve_skill_file(
        skill_name=skill_name,
        lesson=lesson,
        rationale=rationale,
        target=target,
        active_dir=skill.skill_dir if skill else "",
        instructions=instructions,
        description=description,
        when_to_use=when_to_use,
        tags=tags,
    )
    if result.get("ok"):
        reset_skill_cache()
    return result


def create_skill(
    name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    target: str = "project",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: object = None,
    evidence: str = "",
    actor: str = "agent",
    tags: list[str] | None = None,
) -> dict:
    result = create_skill_file(
        name=name,
        description=description,
        instructions=instructions,
        when_to_use=when_to_use,
        target=target,
        context=context,
        user_invocable=user_invocable,
        allowed_tools=allowed_tools,
        evidence=evidence,
        actor=actor,
        tags=tags,
    )
    if result.get("ok"):
        reset_skill_cache()
    return result


def record_online_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    record_online_skill_provenance(
        action=action,
        skill_name=skill_name,
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=decision,
        error=error,
    )


def record_feedback(skill_name: str, rating: str, note: str = "") -> None:
    record_skill_feedback(skill_name=skill_name, rating=rating, note=note)


def skill_stats() -> str:
    return format_skill_stats()
