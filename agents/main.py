"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import re
import select
import signal
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from .agent import Agent
from .session import get_latest_session_id, load_session
from .tools import set_background_done_callback
from .ui import (
    print_welcome,
    print_user_prompt,
    print_user_message,
    print_input_metadata,
    print_error,
    print_info,
    print_plan_for_approval,
    print_plan_approval_options,
    print_goodbye,
    print_interrupted,
    print_processing_status,
    print_warning,
)
from .cli import registry as cli_registry, handle_skill_invocation


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
    parser.add_argument("--resume", "-c", "--continue", action="store_true", help="Resume last session (alias: -c)")
    parser.add_argument("--session", default=None, help="Resume specific session by ID")
    parser.add_argument("--fork", action="store_true", help="Fork session when resuming")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--trace", action="store_true", help="Enable JSONL trace logging (~/.bear-code/trace/)")
    parser.add_argument("--tui", action="store_true", help="Use Textual TUI interface (interactive with mouse support)")
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


def _resolve_api_config(cli_api_base: str | None) -> tuple[str | None, str | None]:
    generic_api_key = _clean_env(os.environ.get("APIKEY")) or _clean_env(os.environ.get("MINI_CLAUDE_API_KEY"))
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API")) or _clean_env(os.environ.get("MINI_CLAUDE_API_BASE"))
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))

    resolved_api_base = _clean_env(cli_api_base) or generic_api_base or openai_api_base

    if resolved_api_base:
        return resolved_api_base, generic_api_key or openai_api_key

    if openai_api_key or openai_api_base:
        return openai_api_base, generic_api_key or openai_api_key

    if generic_api_key:
        return None, generic_api_key

    return None, None


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
    "/new", "/clear", "/plan", "/cost", "/compact", "/cd", "/help", "/thinking",
    "/rewind", "/undo", "/goal", "/context", "/ctx", "/memory", "/skills",
    "/skill-stats", "/skill-eval", "/extract_now", "/skill-feedback",
    "/skill-evolve", "/skill-create", "/fork", "/sessions", "/switch",
    "/resume", "/rename", "/export", "/trace", "/graph", "/models", "/status",
    "/permission", "/perm", "/yolo", "/quit", "/q",
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


async def _dispatch_command(agent: Agent, inp: str) -> bool:
    """Dispatch a slash command using the CLI registry. Returns True if handled."""
    cmd, args = cli_registry.find(inp)
    if cmd:
        result = cmd.handler(agent, args)
        if asyncio.iscoroutine(result):
            await result
        return True
    if inp.startswith("/"):
        space_idx = inp.find(" ")
        cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
        cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
        return await handle_skill_invocation(agent, cmd_name, cmd_args)
    return False


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""
    from .history import get_history
    
    # Initialize prompt history
    history = get_history()
    
    # Try to use prompt_toolkit for better input
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import WordCompleter
        
        # Build completer from commands
        from .cli.registry import registry
        commands = [f"/{cmd.name}" for cmd in registry.list_all()]
        completer = WordCompleter(commands, sentence=True)
        
        # Create prompt session with history
        pt_history = InMemoryHistory()
        # Load recent history
        for text in history.recent(100):
            pt_history.append_string(text)
        
        session = PromptSession(
            history=pt_history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
        )
        use_prompt_toolkit = True
    except ImportError:
        use_prompt_toolkit = False
        session = None

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input().strip().lower()
            return answer in ("y", "yes", "a", "always")
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

        # Show status bar (but not ❯ if using prompt_toolkit)
        if use_prompt_toolkit:
            # Just show status bar without ❯
            from pathlib import Path
            from .tui.theme import theme
            from .ui import console
            
            cwd = str(Path.cwd())
            home = str(Path.home())
            if cwd.startswith(home):
                cwd = "~" + cwd[len(home):]
            
            term_width = console.width or 80
            max_cwd_len = term_width // 2
            if len(cwd) > max_cwd_len:
                cwd = "..." + cwd[-(max_cwd_len-3):]
            
            status = agent.status_line()
            right_part = status
            padding = max(2, term_width - len(cwd) - len(right_part))
            
            console.print(f"[{theme.TEXT_MUTED}]{cwd}[/{theme.TEXT_MUTED}]" + 
                          " " * padding + 
                          f"[{theme.TEXT_MUTED}]{right_part}[/{theme.TEXT_MUTED}]")
        else:
            print_user_prompt(agent.status_line())
        
        print_input_metadata(agent.permission_mode, agent.model)
        try:
            if use_prompt_toolkit and session:
                line = await session.prompt_async("❯ ")
            else:
                line = input()
        except (EOFError, KeyboardInterrupt):
            print_goodbye()
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        
        # Add to history (only non-command inputs)
        if not inp.startswith("/"):
            history.add(inp)
        
        if inp in ("exit", "quit", ":q"):
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
        if inp.startswith("/"):
            handled = await _dispatch_command(agent, inp)
            if handled:
                continue
            # Unknown command: error instead of sending to model
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            print_error(
                f"Unknown command: /{cmd_name}. Type /help for the command list, "
                "or write it as a natural-language question to ask the model."
            )
            continue

        # Normal chat
        # @path 引用展开：把 @文件/@目录 的内容注入本轮输入。
        expanded, ref_notes = _expand_at_references(inp)
        
        # 提取 @ 引用的文件路径用于显示 badge
        at_refs = _AT_REF_RE.findall(inp)
        file_refs = [r for r in at_refs if (Path.cwd() / os.path.expanduser(r)).is_file()]
        
        # 显示文件引用 badge（用户输入已在 ❯ 行显示，无需重复显示消息）
        if file_refs:
            from .ui import console
            from .tui.theme import theme
            for f in file_refs:
                console.print(f"[{theme.PRIMARY}]┃[/{theme.PRIMARY}] [{theme.SECONDARY}] File [/{theme.SECONDARY}] {f}")
        
        for note in ref_notes:
            print_info(note)

        # ESC 键中断监听：在 agent 处理时启动后台线程监听 ESC 键
        esc_listener_started = False
        esc_listener_stop = threading.Event()

        def _esc_listener():
            """后台线程：监听 ESC 键，按下时中断 agent。"""
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                # 检查是否是 TTY
                if not sys.stdin.isatty():
                    return
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    while not esc_listener_stop.is_set():
                        if select.select([sys.stdin], [], [], 0.1)[0]:
                            ch = sys.stdin.read(1)
                            if ch == '\x1b':  # ESC
                                if agent.is_processing and not agent._aborted:
                                    agent.abort()
                                    print_interrupted()
                                break
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except (ImportError, OSError, ValueError, Exception):
                # 非 TTY 环境或 termios 不可用，静默退出
                pass

        try:
            # 显示处理中状态
            print_processing_status()
            
            # 启动 ESC 监听线程
            esc_thread = threading.Thread(target=_esc_listener, daemon=True)
            esc_thread.start()
            esc_listener_started = True

            await agent.chat(expanded)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))
        finally:
            if esc_listener_started:
                esc_listener_stop.set()
                esc_thread.join(timeout=0.5)
    await agent.drain_background_skill_tasks()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    await agent.chat(prompt)
    await agent.drain_background_skill_tasks()


def main() -> None:
    """CLI 程序入口：准备运行配置，创建 Agent，并按参数选择一次性执行或交互模式。"""
    # 解析命令行参数，例如 --plan、--resume、--model，以及可选的一次性 prompt。
    args = parse_args()
    _load_env_file()

    # 如果指定了 --tui，启动 Textual 交互式界面
    if getattr(args, 'tui', False):
        from .tui.textual_app import run_textual_app
        run_textual_app(args)
        return

    if args.help:
        # 自定义帮助文本，展示 My Code 支持的启动参数和 REPL 内置命令。
        print("""
Usage: mycode [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (model-dependent)
  --model, -m         Model to use (default: deepseek-chat, or MODEL env)
  --api-base URL      Override API base URL from CLI or .env
  --resume, -c        Resume the last session (alias: --continue)
  --session <id>      Resume specific session by ID
  --fork              Fork session when resuming
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --trace             Enable JSONL trace logging (~/.bear-code/trace/)
  --help, -h          Show this help

REPL commands:
  Session:
    /new, /clear        Start new session (clear history)
    /sessions           List saved sessions
    /switch <id>        Switch to a saved session by id
    /resume [id]        Resume a session (no arg = list recent)
    /fork               Fork current session into a new branch
    /rename <name>      Rename current session
    /export [file]      Export session transcript to file
    /compact            Manually compact conversation
    /rewind [N], /undo  Rewind last N turns (default 1), restoring files
    /quit, /q, exit     Quit the session

  Model & Status:
    /models [name]      List available models or switch model
    /status             Show current model, session, tokens, cost
    /cost               Show token usage and cost
    /context            Visualize context (index/role/label/tokens)
    /ctx del <spec>     Delete message groups, e.g. /ctx del 1,3,5~10
    /ctx keep <spec>    Keep only the given message groups

  Mode & Config:
    /plan               Toggle plan mode (read-only <-> normal)
    /permission [mode]  Switch permission mode (default/acceptEdits/bypassPermissions/plan/dontAsk)
    /yolo on|off        Quick toggle for bypass permissions mode
    /thinking           Toggle thinking display (default OFF)
    /cd [path]          Change working directory (no arg = show current)
    /trace [on|off|n]   View recent trace events / toggle trace logging

  Skills & Memory:
    /memory             List saved memories
    /skills             List available skills
    /skill-stats        Show skill usage and evolution stats
    /skill-eval         Evaluate online skill evolution quality
    /extract_now        Extract the current pending window: /extract_now [hint]
    /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
    /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
    /skill-create       Create: /skill-create <name> | <desc> | <when> | <instr>
    /<skill-name>       Invoke a skill (e.g. /commit "fix types")

  Other:
    /goal <goal>        Autonomous goal mode with verifier loop
    /help               Show this help message

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
  mycode -c                           # resume last session
  mycode --session abc123             # resume specific session
  mycode -c --fork                    # fork from last session
  mycode --max-cost 0.50 --max-turns 20 "implement feature X"
  mycode  # starts interactive REPL
""")
        sys.exit(0)

    # 将命令行布尔开关统一转换成 Agent 内部使用的权限模式。
    permission_mode = _resolve_permission_mode(args)
    # 模型优先使用命令行参数，其次读取 .env 中的 MODEL / MINI_CLAUDE_MODEL，最后回落到默认模型。
    model = _resolve_model(args.model)
    resolved_api_base, resolved_api_key = _resolve_api_config(args.api_base)

    # --trace 开启 JSONL 事件日志（也可用 BEAR_TRACE=1 或 REPL 内 /trace on）。
    if args.trace:
        from .trace import set_trace_enabled, trace_path
        set_trace_enabled(True)
        print_info(f"Trace logging enabled: {trace_path()}")

    # 没有可用 API key 时无法调用模型，直接提示配置方式并退出。
    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    # 创建主 Agent。
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base,
        api_key=resolved_api_key,
    )

    # Resume session
    # --resume/-c 会加载最近一次会话，--session <id> 加载指定会话。
    # --fork 会从恢复的会话创建一个新分支。
    session_to_resume = None
    if args.session:
        session_to_resume = args.session
    elif args.resume:
        session_to_resume = get_latest_session_id()

    if session_to_resume:
        session = load_session(session_to_resume)
        if session:
            if args.fork:
                agent.restore_session({
                    "openaiMessages": session.get("openaiMessages"),
                    "foldedSessionMemories": session.get("foldedSessionMemories"),
                    "checkpointStore": session.get("checkpointStore"),
                    "turnBoundaries": session.get("turnBoundaries"),
                    "contextStore": session.get("contextStore"),
                })
                agent.fork_session()
            else:
                agent.session_id = session_to_resume
                agent.restore_session({
                    "openaiMessages": session.get("openaiMessages"),
                    "foldedSessionMemories": session.get("foldedSessionMemories"),
                    "checkpointStore": session.get("checkpointStore"),
                    "turnBoundaries": session.get("turnBoundaries"),
                    "contextStore": session.get("contextStore"),
                })
        else:
            print_info(f"Session not found: {session_to_resume}")

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
