from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..registry import command
from ...core.session import list_sessions, load_session, delete_session, clean_sessions
from ...ui import print_info, print_error, print_session_rows

if TYPE_CHECKING:
    from ..agent import Agent


def _format_session_time(start_time: str) -> str:
    """Format session start time as relative time."""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        diff = now - start
        
        if diff.days == 0:
            if diff.seconds < 3600:
                return f"{diff.seconds // 60}m ago"
            return f"{diff.seconds // 3600}h ago"
        elif diff.days == 1:
            return "1d ago"
        elif diff.days < 7:
            return f"{diff.days}d ago"
        elif diff.days < 30:
            return f"{diff.days // 7}w ago"
        else:
            return f"{diff.days // 30}mo ago"
    except:
        return start_time[:10] if start_time else ""


def _get_session_category(start_time: str) -> str:
    """Get category for session based on time."""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        diff = now - start
        
        if diff.days == 0:
            return "Today"
        elif diff.days == 1:
            return "Yesterday"
        elif diff.days < 7:
            return "This Week"
        else:
            return "Older"
    except:
        return "Older"


@command(
    name="sessions",
    description="List, delete, or clean sessions",
    usage="[rm <id>|clean]",
    category="session",
)
async def cmd_sessions(agent: "Agent", args: str) -> None:
    if args.startswith("rm "):
        target = args[3:].strip()
        if not target:
            print_error("Usage: /sessions rm <session_id>")
            return
        if target == agent.session_id:
            print_error("Cannot delete the current session. /switch to another one first.")
            return
        if delete_session(target):
            print_info(f"Deleted session {target}.")
        else:
            print_error(f"Session not found: {target}")
        return
    if args == "clean":
        deleted = clean_sessions(only_tmp=True)
        print_info(f"Cleaned {deleted} test/temp session(s).")
        return
    
    sessions = list_sessions()
    if not sessions:
        print_info("No saved sessions.")
        return
    
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    
    # Try to use interactive dialog
    try:
        from ...tui.dialog import SelectDialog, DialogOption, DialogAction
        
        def on_switch(session_id: str):
            """Switch to selected session."""
            if session_id == agent.session_id:
                return
            session = load_session(session_id)
            if session:
                from agents.core.session import session_to_restore_dict
                agent.session_id = session_id
                agent.restore_session(session_to_restore_dict(session))
                print_info(f"Switched to session {session_id[:8]}...")
        
        def on_delete(session_id: str):
            """Delete selected session."""
            if session_id == agent.session_id:
                print_error("Cannot delete the current session.")
                return
            if delete_session(session_id):
                print_info(f"Deleted session {session_id[:8]}...")
            else:
                print_error(f"Failed to delete session")
        
        # Build options
        options = []
        for s in sessions[:50]:  # Limit to 50 sessions
            sid = s.get("id", "")
            title = s.get("title", "Untitled")
            start = s.get("startTime", "")
            msgs = s.get("messageCount", 0)
            
            options.append(DialogOption(
                title=title[:50],  # Truncate long titles
                value=sid,
                description=f"({msgs} msgs)",
                footer=_format_session_time(start),
                category=_get_session_category(start),
            ))
        
        actions = [
            DialogAction("Delete", "d", on_delete),
        ]
        
        dialog = SelectDialog(
            title="Sessions",
            options=options,
            actions=actions,
            current=agent.session_id,
        )
        
        selected = await dialog.run_async()
        if selected:
            on_switch(selected)
    
    except ImportError:
        # Fallback to simple list
        print_session_rows(sessions[:30], current_id=agent.session_id)


@command(
    name="switch",
    description="Switch to another session",
    usage="<session_id>",
    category="session",
)
async def cmd_switch(agent: "Agent", args: str) -> None:
    target = args.strip()
    if not target:
        print_error("Usage: /switch <session_id>  (see /sessions)")
        return
    session = load_session(target)
    if not session:
        print_error(f"Session not found: {target}")
        return
    from agents.core.session import session_to_restore_dict
    agent.session_id = target
    agent.restore_session(session_to_restore_dict(session))


@command(
    name="resume",
    description="Resume a session",
    usage="[<session_id>]",
    category="session",
)
async def cmd_resume(agent: "Agent", args: str) -> None:
    target = args.strip()
    if not target:
        sessions = list_sessions()
        if not sessions:
            print_info("No saved sessions to resume.")
            return
        sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
        print_info("Recent sessions:")
        for i, s in enumerate(sessions[:10]):
            sid = s.get("id", "N/A")[:8]
            title = s.get("title", "Untitled")
            start = s.get("startTime", "N/A")[:10]
            msgs = s.get("messageCount", 0)
            print(f"  {i+1}. {sid}... - {title} ({start}, {msgs} msgs)")
        print_info("Usage: /resume <session_id> or /switch <session_id>")
        return
    session = load_session(target)
    if not session:
        print_error(f"Session not found: {target}")
        return
    from agents.core.session import session_to_restore_dict
    agent.session_id = target
    agent.restore_session(session_to_restore_dict(session))
    print_info(f"Resumed session {target}")


@command(
    name="clear",
    description="Clear conversation history",
    category="session",
)
async def cmd_clear(agent: "Agent", args: str) -> None:
    agent.clear_history()


@command(
    name="compact",
    description="Compact conversation context",
    category="session",
)
async def cmd_compact(agent: "Agent", args: str) -> None:
    try:
        await agent.compact()
    except Exception as e:
        print_error(str(e))


@command(
    name="rewind",
    description="Rewind N turns of conversation",
    usage="[N]",
    category="session",
)
async def cmd_rewind(agent: "Agent", args: str) -> None:
    n = 1
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            print_error("Usage: /rewind [N]  (N = number of turns to rewind)")
            return
    print_info(agent.rewind(n))


@command(
    name="fork",
    description="Fork current session",
    category="session",
)
async def cmd_fork(agent: "Agent", args: str) -> None:
    print_info(agent.fork_session())


@command(
    name="subagents",
    description="View subagent task progress",
    aliases=["sa", "tasks"],
    category="session",
)
async def cmd_subagents(agent: "Agent", args: str) -> None:
    """View subagent task progress."""
    from ...tui.subagent_tracker import get_tracker
    from ...tui.output import print_subagent_progress
    
    tracker = get_tracker()
    tasks = tracker.get_recent_tasks(20)
    print_subagent_progress(tasks)


@command(
    name="new",
    description="Start new session (alias for /clear)",
    aliases=["clear"],
    category="session",
)
async def cmd_new(agent: "Agent", args: str) -> None:
    agent.clear_history()


@command(
    name="undo",
    description="Undo last turn (alias for /rewind 1)",
    category="session",
)
async def cmd_undo(agent: "Agent", args: str) -> None:
    print_info(agent.rewind(1))


@command(
    name="rename",
    description="Rename current session",
    usage="<name>",
    category="session",
)
async def cmd_rename(agent: "Agent", args: str) -> None:
    name = args.strip()
    if not name:
        print_error("Usage: /rename <name>")
        return
    from ...core.session import save_session_meta
    if save_session_meta(agent.session_id, {"title": name}):
        print_info(f"Session renamed to: {name}")
    else:
        print_error("Failed to rename session")


@command(
    name="export",
    description="Export session transcript to file",
    usage="[file]",
    category="session",
)
async def cmd_export(agent: "Agent", args: str) -> None:
    filename = args.strip()
    if not filename:
        filename = f"session_{agent.session_id}.json"
    path = Path(filename)
    data = {
        "session_id": agent.session_id,
        "messages": agent._openai_messages,
        "model": agent.model,
    }
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        print_info(f"Exported to: {path.absolute()}")
    except OSError as e:
        print_error(f"Export failed: {e}")


@command(
    name="quit",
    description="Exit the session",
    aliases=["q", "exit"],
    category="session",
)
async def cmd_quit(agent: "Agent", args: str) -> None:
    from ...ui import print_goodbye
    print_goodbye()
    import sys
    sys.exit(0)
