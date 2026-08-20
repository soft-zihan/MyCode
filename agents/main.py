"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import re
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from .agent import Agent
from .session import list_sessions, load_session, get_latest_session_id
from .tools import set_background_done_callback
from .ui import (
    print_welcome,
    print_user_prompt,
    print_error,
    print_info,
    print_plan_for_approval,
    print_plan_approval_options,
    print_goodbye,
    print_interrupted,
    print_memory_entries,
    print_skill_entries,
    print_warning,
    print_context_rows,
    set_thinking_visible,
    thinking_visible,
)
from .memory import list_memories
from .skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
    skill_stats,
)
from .online_skill_eval import format_online_skill_eval_async


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mycode",
        description="My Code — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_model(cli_model: str | None) -> str:
    """模型优先级：命令行参数 > MODEL > MINI_CLAUDE_MODEL > 默认值。"""
    return (
        _clean_env(cli_model)
        or _clean_env(os.environ.get("MODEL"))
        or _clean_env(os.environ.get("MINI_CLAUDE_MODEL"))
        or "deepseek-chat"
    )


def _should_abort_on_sigint(agent) -> bool:
    """Ctrl+C 时是否应中断 agent：仅当 agent 正在处理且尚未被标记中止。

    注意：必须使用 is_processing（基于 asyncio 任务状态），而不是
    `_output_buffer is not None` —— 后者只在 run_once()（子 Agent）里被设置，
    在 REPL 顶层 chat 中恒为 None，会导致 Ctrl+C 永远无法打断。
    """
    return not agent._aborted and agent.is_processing


def _load_env_file() -> None:
    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


def _is_anthropic_compatible_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    path = (parsed.path or "").lower().rstrip("/")
    return path.endswith("/anthropic") or "/anthropic/" in path


def _resolve_api_config(cli_api_base: str | None) -> tuple[str | None, str | None, bool]:
    generic_api_key = _clean_env(os.environ.get("APIKEY")) or _clean_env(os.environ.get("MINI_CLAUDE_API_KEY"))
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))
    anthropic_api_key = _clean_env(os.environ.get("ANTHROPIC_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API")) or _clean_env(os.environ.get("MINI_CLAUDE_API_BASE"))
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))
    anthropic_api_base = _clean_env(os.environ.get("ANTHROPIC_BASE_URL"))

    resolved_api_base = _clean_env(cli_api_base) or generic_api_base or openai_api_base or anthropic_api_base

    if resolved_api_base:
        if _is_anthropic_compatible_base_url(resolved_api_base):
            return resolved_api_base, generic_api_key or anthropic_api_key or openai_api_key, False
        return resolved_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if anthropic_api_key or anthropic_api_base:
        return anthropic_api_base, generic_api_key or anthropic_api_key or openai_api_key, False

    if openai_api_key or openai_api_base:
        return openai_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if generic_api_key:
        return None, generic_api_key, False

    return None, None, False


# ─── @ 文件引用展开 ─────────────────────────────────────────

_AT_REF_RE = re.compile(r"@([\w./~-][\w./~\-]*)")
_AT_REF_MAX_BYTES = 32 * 1024  # 单个引用文件最多注入 32KB，避免撑爆上下文


def _expand_at_references(text: str) -> tuple[str, list[str]]:
    """把输入中的 @path 展开为文件内容块。

    返回 (新文本, 展开说明列表)。规则：
    - 相对路径基于当前 cwd 解析，支持 ~ 展开。
    - 文件存在且 ≤32KB：注入 "<file path>\\n内容\\n</file>" 块。
    - 目录：注入其一级条目列表。
    - 不存在：保留原样（可能是邮箱或模型上下文里的普通 @）。
    """
    notes: list[str] = []

    def _sub(m: re.Match) -> str:
        raw = m.group(1)
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            if path.is_file():
                size = path.stat().st_size
                if size > _AT_REF_MAX_BYTES:
                    notes.append(f"@{raw}: file too large ({size} bytes), use read_file instead")
                    return m.group(0)
                content = path.read_text(encoding="utf-8", errors="replace")
                notes.append(f"@{raw}: injected {size} bytes")
                return f"\n<file path=\"{path}\">\n{content}\n</file>\n"
            if path.is_dir():
                entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
                notes.append(f"@{raw}: directory listing ({len(entries)} entries)")
                return f"\n<directory path=\"{path}\">\n" + "\n".join(entries) + "\n</directory>\n"
        except OSError:
            pass
        return m.group(0)  # 不存在或不可读：保留原样

    return _AT_REF_RE.sub(_sub, text), notes


# ─── Tab 补全（readline）───────────────────────────────────

REPL_COMMANDS = [
    "/clear", "/plan", "/cost", "/compact", "/cd", "/help", "/thinking",
    "/rewind", "/goal", "/context", "/ctx", "/memory", "/skills",
    "/skill-stats", "/skill-eval", "/extract_now", "/skill-feedback",
    "/skill-evolve", "/skill-create", "/fork", "/sessions", "/switch",
]


def _complete_at_path(word: str) -> list[str]:
    """@ 前缀的文件/目录路径补全候选。

    规则：以最后一个路径分量作为待补全前缀，在其父目录里匹配。
    以 "/" 结尾时则列出该目录内容。目录候选追加 "/"。
    """
    raw = word[1:]
    expanded = os.path.expanduser(raw)
    if raw.endswith("/"):
        parent, prefix = Path(expanded), ""
    else:
        p = Path(expanded)
        parent, prefix = p.parent, p.name
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    matches = []
    for p in entries:
        if p.name.startswith("."):
            continue
        if not p.name.startswith(prefix):
            continue
        matches.append(f"@{p}/" if p.is_dir() else f"@{p}")
    return matches


def _repl_completer(text: str, state: int):
    """readline 补全：/ 开头补全命令，@ 开头补全路径。"""
    try:
        import readline

        line = readline.get_line_buffer()
        start = readline.get_begidx()
        prefix_is_word_start = start == 0 or (start > 0 and line[start - 1] in " \t")
        if text.startswith("/") and prefix_is_word_start:
            candidates = [c for c in REPL_COMMANDS if c.startswith(text)]
        elif text.startswith("@"):
            candidates = _complete_at_path(text)
        else:
            candidates = []
    except Exception:
        candidates = []
    return candidates[state] if state < len(candidates) else None


def _setup_readline() -> None:
    """启用 Tab 补全（/ 命令与 @ 路径）。非 TTY 或无 readline 时静默跳过。

    macOS 的 Python readline 底层是 libedit 而非 GNU readline：
    GNU 语法 "tab: complete" 会被接受但【静默无效】，必须用 libedit 的
    bind 语法把 Ctrl+I(Tab) 绑到 rl_complete，否则 Tab 完全没反应。
    """
    try:
        import readline
    except ImportError:
        return
    try:
        # @ 不在分隔符里，保证 "@path" 作为整体参与补全
        readline.set_completer_delims(" \t\n;|&")
        readline.set_completer(_repl_completer)
        if "libedit" in (getattr(readline, "__doc__", "") or ""):
            readline.parse_and_bind('bind "^I" rl_complete')
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        pass


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    # 后台 shell 完成回调：watcher 线程把结果放进队列，
    # REPL 每轮输入前检查队列，若有完成的后台任务就自动用其结果发起对话。
    bg_done_queue: "queue.Queue[tuple]" = queue.Queue()

    def _on_bg_done(job_id: str, command: str, output: str, exit_code: int) -> None:
        bg_done_queue.put((job_id, command, output, exit_code))

    set_background_done_callback(_on_bg_done)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if _should_abort_on_sigint(agent):
            # Agent is processing
            agent.abort()
            print_interrupted()
            sigint_count = 0
            print_user_prompt(agent.status_line())
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print_goodbye()
                sys.exit(0)
            print_warning("Press Ctrl+C again to exit.")
            print_user_prompt(agent.status_line())

    signal.signal(signal.SIGINT, handle_sigint)
    _setup_readline()
    print_welcome()

    while True:
        # 后台 shell 完成自动勾起对话：消费队列里的完成事件，
        # 把结果作为新一轮用户输入发给模型，让模型基于结果继续。
        while not bg_done_queue.empty():
            try:
                job_id, command, output, exit_code = bg_done_queue.get_nowait()
            except queue.Empty:
                break
            print_info(f"Background job {job_id} finished (exit {exit_code}).")
            followup = (
                f"[Background shell job {job_id} finished]\n"
                f"Command: {command}\nExit code: {exit_code}\nOutput:\n{output}\n\n"
                "Review this result and continue the task accordingly."
            )
            try:
                await agent.chat(followup)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))

        print_user_prompt(agent.status_line())
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print_goodbye()
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print_goodbye()
            break

        # ! 前缀：直接执行 shell 命令（不经过模型），输出打印后回到提示符。
        # 用户想在对话中自己跑命令时用这个，避免 /ls 被当成对话发给模型。
        if inp.startswith("!"):
            command = inp[1:].strip()
            if not command:
                print_error("Usage: !<command>  e.g. !ls -la")
                continue
            from .tools import _run_shell
            from .ui import console as _console
            result = _run_shell({"command": command, "timeout": 30000})
            _console.print(result)
            continue

        # REPL commands
        if inp == "/fork":
            print_info(agent.fork_session())
            continue
        if inp == "/sessions":
            sessions = list_sessions()
            if not sessions:
                print_info("No saved sessions.")
            else:
                sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
                lines = [f"  {s.get('id')}  {s.get('startTime','')}  msgs={s.get('messageCount',0)}  cwd={s.get('cwd','')}" for s in sessions[:20]]
                print_info("Sessions (newest first):\n" + "\n".join(lines))
            continue
        if inp.startswith("/switch"):
            target = inp[len("/switch"):].strip()
            if not target:
                print_error("Usage: /switch <session_id>  (see /sessions)")
                continue
            session = load_session(target)
            if not session:
                print_error(f"Session not found: {target}")
                continue
            agent.session_id = target
            agent.restore_session({
                "anthropicMessages": session.get("anthropicMessages"),
                "openaiMessages": session.get("openaiMessages"),
                "foldedSessionMemories": session.get("foldedSessionMemories"),
                "checkpointStore": session.get("checkpointStore"),
                "turnBoundaries": session.get("turnBoundaries"),
                "contextStore": session.get("contextStore"),
            })
            continue
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/rewind" or inp.startswith("/rewind "):
            # /rewind [N]：回退最近 N 轮对话（默认 1），同时恢复被修改的文件。
            n = 1
            if inp.startswith("/rewind "):
                try:
                    n = int(inp.split(" ", 1)[1].strip())
                except ValueError:
                    print_error("Usage: /rewind [N]  (N = number of turns to rewind)")
                    continue
            print_info(agent.rewind(n))
            continue
        if inp == "/context":
            # /context：可视化当前上下文（index/role/内容摘要/字符数）。
            rows = agent.describe_context()
            if not rows:
                print_info("Context is empty.")
            else:
                print_context_rows(rows)
            continue
        if inp.startswith("/ctx del") or inp.startswith("/ctx keep"):
            # /ctx del <spec>：删除指定消息组（保持工具配对完整）。
            # /ctx keep <spec>：只保留指定消息组。
            # spec 支持批量表达式：1,3,5~10（逗号/空格混合，~ 或 - 表示范围）。
            from .context_edit import parse_index_spec

            action = "del" if inp.startswith("/ctx del") else "keep"
            rest = inp.split(None, 2)[2] if len(inp.split(None, 2)) > 2 else ""
            try:
                indexes = parse_index_spec(rest)
            except ValueError:
                print_error(f"Usage: /ctx {action} <index> [index2 ...]  e.g. /ctx {action} 1,3,5~10")
                continue
            if not indexes:
                print_error(f"Usage: /ctx {action} <index> [index2 ...]  e.g. /ctx {action} 1,3,5~10")
                continue
            if action == "del":
                print_info(agent.delete_context_messages(indexes))
            else:
                print_info(agent.keep_context_messages(indexes))
            continue
        if inp.startswith("/goal"):
            # /goal <目标>：提炼成功标准 → 用户确认 → 自主执行+验证循环。
            from .goal import GoalLoop, extract_goal_criteria

            goal_text = inp[len("/goal"):].strip()
            if not goal_text:
                print_error("Usage: /goal <goal description>")
                continue
            side_query = agent._build_side_query(max_tokens=2400)
            if not side_query:
                print_error("No side-query model configured; cannot extract goal criteria.")
                continue
            print_info("Extracting success criteria...")
            criteria = await extract_goal_criteria(goal_text, side_query)
            if not criteria:
                print_error("Could not extract verifiable success criteria. Try a more concrete goal.")
                continue
            print_info("Success criteria:\n" + "\n".join(f"  {i}. {c}" for i, c in enumerate(criteria, 1)))
            try:
                answer = input("  Start goal mode with these criteria? (y/n): ")
            except EOFError:
                answer = "n"
            if not answer.lower().startswith("y"):
                print_info("Goal mode cancelled.")
                continue
            loop = GoalLoop(agent, goal_text, criteria, side_query=side_query)
            state = await loop.run()
            print_info(
                f"Goal mode finished: {state.status} after {state.iteration} iteration(s)."
                + ("\nUse /rewind to undo changes if the result is not what you wanted."
                   if state.status != "achieved" else "")
            )
            continue
        if inp == "/help":
            cmds = "\n".join(f"  {c}" for c in REPL_COMMANDS)
            print_info(
                "REPL commands:\n" + cmds +
                "\n\nTips:\n"
                "  @path            引用文件/目录，内容自动注入本轮输入\n"
                "  Tab              补全 / 命令与 @ 路径\n"
                "  /help            显示本帮助；完整说明见 --help"
            )
            continue
        if inp == "/thinking":
            new_state = not thinking_visible()
            set_thinking_visible(new_state)
            print_info(f"Thinking display: {'ON' if new_state else 'OFF'}")
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_memory_entries(memories)
            continue
        if inp == "/cd" or inp.startswith("/cd "):
            # /cd <path>：切换工作目录。Memory/Skills/规则都按 cwd 隔离，
            # 切换后刷新 system prompt 让模型看到新的工作目录与项目规则。
            target = inp[len("/cd"):].strip() if inp.startswith("/cd ") else ""
            if not target:
                print_info(f"Current directory: {Path.cwd()}")
                continue
            new_dir = Path(os.path.expanduser(target))
            if not new_dir.is_absolute():
                new_dir = Path.cwd() / new_dir
            try:
                new_dir = new_dir.resolve()
                if not new_dir.is_dir():
                    print_error(f"Not a directory: {new_dir}")
                    continue
                os.chdir(new_dir)
            except OSError as e:
                print_error(f"Cannot change directory: {e}")
                continue
            agent._refresh_runtime_system_prompt()
            print_info(f"Changed working directory to: {new_dir}")
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .bear/skills/<name>/SKILL.md")
            else:
                print_skill_entries(skills)
            continue
        if inp == "/skill-stats":
            print_info(skill_stats())
            continue
        if inp == "/skill-eval":
            side_query = agent._build_side_query(max_tokens=2400)
            print_info(await format_online_skill_eval_async(side_query=side_query))
            continue
        if inp.startswith("/extract_now"):
            hint = inp[len("/extract_now") :].strip()
            result = await agent.extract_now(hint)
            if result.get("ok"):
                print_info("Ran online skill extraction for the current pending window.")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-feedback "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 2)
            if len(parts) < 2:
                print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
                continue
            note = parts[2] if len(parts) > 2 else ""
            record_feedback(parts[0], parts[1], note)
            print_info(f"Recorded feedback for skill: {parts[0]}")
            continue
        if inp.startswith("/skill-evolve "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 1)
            if len(parts) < 2:
                print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                continue
            result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
            if result.get("ok"):
                print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-create "):
            _, rest = inp.split(" ", 1)
            parts = [part.strip() for part in rest.split("|", 3)]
            if len(parts) < 4 or not all(parts[:4]):
                print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
                continue
            result = create_skill(
                name=parts[0],
                description=parts[1],
                when_to_use=parts[2],
                instructions=parts[3],
                target="project",
                context="inline",
                user_invocable=False,
                evidence="Manual REPL skill creation",
            )
            if result.get("ok"):
                print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
            else:
                print_error(str(result.get("error") or result))
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        result = execute_skill(skill.name, cmd_args)
                        if not result:
                            print_error(f"Unknown skill: {skill.name}")
                            continue
                        await agent.chat(result["prompt"])
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue
            # 既不是内置命令也不是可调用 skill：报错而不是发给模型，
            # 避免 /ls 这类输入被当成对话浪费 token。想问“ls 是什么”请用自然语言。
            print_error(
                f"Unknown command: /{cmd_name}. Type /help for the command list, "
                "or write it as a natural-language question to ask the model."
            )
            continue

        # Normal chat
        # @path 引用展开：把 @文件/@目录 的内容注入本轮输入。
        expanded, ref_notes = _expand_at_references(inp)
        for note in ref_notes:
            print_info(note)
        try:
            await agent.chat(expanded)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))
    await agent.drain_background_skill_tasks()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    await agent.chat(prompt)
    await agent.drain_background_skill_tasks()


def main() -> None:
    """CLI 程序入口：准备运行配置，创建 Agent，并按参数选择一次性执行或交互模式。"""
    # 解析命令行参数，例如 --plan、--resume、--model，以及可选的一次性 prompt。
    args = parse_args()
    _load_env_file()

    if args.help:
        # 自定义帮助文本，展示 My Code 支持的启动参数和 REPL 内置命令。
        print("""
Usage: mycode [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: deepseek-chat, or MODEL env)
  --api-base URL      Override API base URL from CLI or .env
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /cd [path]          Change working directory (no arg = show current)
  /help               Show REPL command list
  /thinking           Toggle thinking display (default OFF)
  /rewind [N]         Rewind last N turns (default 1), restoring changed files
  /goal <goal>        Autonomous goal mode with verifier loop
  /fork               Fork current session into a new independent branch
  /sessions           List saved sessions
  /switch <id>        Switch to a saved session by id
  /context            Visualize context (index/role/label/tokens)
  /ctx del <spec>     Delete message groups, e.g. /ctx del 1,3,5~10
  /ctx keep <spec>    Keep only the given message groups
  /memory             List saved memories
  /skills             List available skills
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate online skill evolution quality
  /extract_now        Extract the current pending online skill window: /extract_now [hint]
  /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skill-create       Create a skill: /skill-create <name> | <description> | <when-to-use> | <instructions>
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Tips:
  @path               Reference a file/dir in your prompt, e.g. "summarize @README.md"
  !command            Run a shell command directly in the REPL, e.g. !ls -la
  Tab                 Autocomplete / commands and @ paths
  BEAR_CONTEXT_WINDOW=N            Override the model context window (tokens)
  BEAR_AUTO_COMPACT_THRESHOLD=0.9  Auto-compact when context reaches this fraction
  BEAR_MD_RENDER=0                 Disable auto Markdown re-render after streaming

Examples:
  mycode "fix the bug in src/app.ts"
  mycode --yolo "run all tests and fix failures"
  mycode --plan "how would you refactor this?"
  mycode --max-cost 0.50 --max-turns 20 "implement feature X"
  MODEL=deepseek-chat APIKEY=sk-xxx API=https://api.deepseek.com/anthropic mycode "hello"
  MODEL=gpt-4o OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://aihubmix.com/v1 mycode "hello"
  mycode --resume
  mycode  # starts interactive REPL
""")
        sys.exit(0)

    # 将命令行布尔开关统一转换成 Agent 内部使用的权限模式。
    permission_mode = _resolve_permission_mode(args)
    # 模型优先使用命令行参数，其次读取 .env 中的 MODEL / MINI_CLAUDE_MODEL，最后回落到默认模型。
    model = _resolve_model(args.model)
    resolved_api_base, resolved_api_key, resolved_use_openai = _resolve_api_config(args.api_base)

    # 没有可用 API key 时无法调用模型，直接提示配置方式并退出。
    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    # 创建主 Agent。OpenAI-compatible 和 Anthropic 原生接口使用不同的 base URL 参数名传入。
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
    )

    # Resume session
    # --resume 会加载最近一次会话，把历史消息恢复到新建的 Agent 中。
    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                agent.restore_session({
                    "anthropicMessages": session.get("anthropicMessages"),
                    "openaiMessages": session.get("openaiMessages"),
                    "foldedSessionMemories": session.get("foldedSessionMemories"),
                })
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    # 如果命令行后面带了普通文本参数，就拼成一次性 prompt；否则进入交互式 REPL。
    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # One-shot mode
        # 一次性模式：执行完用户 prompt 后进程结束。
        try:
            asyncio.run(run_one_shot(agent, prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        # 交互模式：启动循环读取用户输入，直到用户退出。
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
