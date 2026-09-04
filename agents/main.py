"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import re
import signal
import sys
import threading
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .agent import Agent
from .core.session import get_latest_session_id, load_session
from .tools import set_background_done_callback
from .logging import print_info, print_error, print_warning
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
    parser.add_argument("--resume", "-c", "--continue", action="store_true", help="Resume last session")
    parser.add_argument("--session", default=None, help="Resume specific session by ID")
    parser.add_argument("--fork", action="store_true", help="Fork session when resuming")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--trace", action="store_true", help="Enable JSONL trace logging")
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
    return (
        _clean_env(cli_model)
        or _clean_env(os.environ.get("MODEL"))
        or _clean_env(os.environ.get("MINI_CLAUDE_MODEL"))
        or "deepseek-chat"
    )


def _should_abort_on_sigint(agent) -> bool:
    return not agent.aborted and agent.is_processing


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


_AT_REF_RE = re.compile(r"@([\w./~-][\w./~\-]*)")
_AT_REF_MAX_BYTES = 32 * 1024


def _expand_at_references(text: str) -> tuple[str, list[str]]:
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
        return m.group(0)

    return _AT_REF_RE.sub(_sub, text), notes


REPL_COMMANDS = [
    "/new", "/clear", "/plan", "/cost", "/compact", "/cd", "/help", "/thinking",
    "/rewind", "/undo", "/goal", "/context", "/ctx", "/memory", "/skills",
    "/skill-stats", "/skill-eval", "/extract_now", "/skill-feedback",
    "/skill-evolve", "/skill-create", "/fork", "/sessions", "/switch",
    "/resume", "/rename", "/export", "/trace", "/graph", "/models", "/status",
    "/permission", "/perm", "/yolo", "/quit", "/q",
]


def _complete_at_path(word: str) -> list[str]:
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
    try:
        import readline
    except ImportError:
        return
    try:
        readline.set_completer_delims(" \t\n;|&")
        readline.set_completer(_repl_completer)
        if "libedit" in (getattr(readline, "__doc__", "") or ""):
            readline.parse_and_bind('bind "^I" rl_complete')
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        pass


async def _dispatch_command(agent: Agent, inp: str) -> bool:
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
    from .cli.history import get_history
    
    history = get_history()
    
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import WordCompleter
        
        from .cli.registry import registry
        commands = [f"/{cmd.name}" for cmd in registry.list_all()]
        completer = WordCompleter(commands, sentence=True)
        
        pt_history = InMemoryHistory()
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
        print_info(f"Plan:\n{plan_content}")
        while True:
            try:
                choice = input("  Enter choice (1=clear+execute, 2=execute, 3=manual, 4=keep planning): ").strip()
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
                    feedback = input("  Feedback: ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print_warning("Invalid choice.")

    agent.set_plan_approval_fn(plan_approval_fn)

    bg_done_queue: "queue.Queue[tuple]" = queue.Queue()

    def _on_bg_done(job_id: str, command: str, output: str, exit_code: int) -> None:
        bg_done_queue.put((job_id, command, output, exit_code))

    set_background_done_callback(_on_bg_done)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if _should_abort_on_sigint(agent):
            agent.abort()
            sigint_count = 0
        else:
            sigint_count += 1
            if sigint_count >= 2:
                sys.exit(0)
            print_warning("Press Ctrl+C again to exit.")

    signal.signal(signal.SIGINT, handle_sigint)
    _setup_readline()

    while True:
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

        try:
            if use_prompt_toolkit and session:
                line = await session.prompt_async("❯ ")
            else:
                line = input("❯ ")
        except (EOFError, KeyboardInterrupt):
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        
        if not inp.startswith("/"):
            history.add(inp)
        
        if inp in ("exit", "quit", ":q"):
            break

        if inp.startswith("!"):
            command = inp[1:].strip()
            if not command:
                print_error("Usage: !<command>  e.g. !ls -la")
                continue
            from .tools import _run_shell
            result = _run_shell({"command": command, "timeout": 30000})
            print(result)
            continue

        if inp.startswith("/"):
            handled = await _dispatch_command(agent, inp)
            if handled:
                continue
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            print_error(
                f"Unknown command: /{cmd_name}. Type /help for the command list."
            )
            continue

        expanded, ref_notes = _expand_at_references(inp)
        
        for note in ref_notes:
            print_info(note)

        esc_listener_stop = threading.Event()

        def _esc_listener():
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                if not sys.stdin.isatty():
                    return
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    import select as _select
                    while not esc_listener_stop.is_set():
                        if _select.select([sys.stdin], [], [], 0.1)[0]:
                            ch = sys.stdin.read(1)
                            if ch == '\x1b':
                                if agent.is_processing and not agent.aborted:
                                    agent.abort()
                                break
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except (ImportError, OSError, ValueError, Exception):
                pass

        esc_thread = threading.Thread(target=_esc_listener, daemon=True)
        esc_thread.start()

        try:
            await agent.chat(expanded)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))
        finally:
            esc_listener_stop.set()
            esc_thread.join(timeout=0.5)

    await agent.drain_background_skill_tasks()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    await agent.chat(prompt)
    await agent.drain_background_skill_tasks()


def main() -> None:
    args = parse_args()
    _load_env_file()

    from .observability import init_tracing
    init_tracing()

    if args.help:
        print("""
Usage: mycode [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts
  --plan              Plan mode: read-only
  --accept-edits      Auto-approve file edits
  --dont-ask          Auto-deny confirmations
  --thinking          Enable extended thinking
  --model, -m         Model to use
  --api-base URL      Override API base URL
  --resume, -c        Resume last session
  --session <id>      Resume specific session
  --fork              Fork session when resuming
  --max-cost USD      Max USD spend
  --max-turns N       Max agentic turns
  --trace             Enable JSONL trace logging
  --help, -h          Show help
""")
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    model = _resolve_model(args.model)
    resolved_api_base, resolved_api_key = _resolve_api_config(args.api_base)

    if args.trace:
        from .observability.trace import set_trace_enabled, trace_path
        set_trace_enabled(True)
        print_info(f"Trace logging enabled: {trace_path()}")

    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base,
        api_key=resolved_api_key,
    )

    session_to_resume = None
    if args.session:
        session_to_resume = args.session
    elif args.resume:
        session_to_resume = get_latest_session_id()

    if session_to_resume:
        session = load_session(session_to_resume)
        if session:
            from agents.core.session import session_to_restore_dict
            restore_data = session_to_restore_dict(session)
            if args.fork:
                agent.restore_session(restore_data)
                agent.fork_session()
            else:
                agent.session_id = session_to_resume
                agent.restore_session(restore_data)
        else:
            print_info(f"Session not found: {session_to_resume}")

    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        try:
            asyncio.run(run_one_shot(agent, prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
