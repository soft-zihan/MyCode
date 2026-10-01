"""Tool Registry - 工具注册表和执行模式定义。

每个工具可声明自己的执行模式：
- sequential: 必须顺序执行（有副作用）
- parallel: 可以并行执行（只读，无副作用）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from agents.tools.task_tools import TASK_LIST_TOOL

ToolDef = dict
PermissionMode = str

READ_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "web_search", "compact_context", "shell_status", "search_history", "list_session_notes"}
EDIT_TOOLS = {"write_file", "edit_file", "skill_create", "remember", "ask_user", "task_list"}

CONCURRENCY_SAFE_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "web_search", "shell_status"}

MAX_RESULT_CHARS = 50000


def _truncate_result(result: str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep_each = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep_each]
        + f"\n\n[... truncated {len(result) - keep_each * 2} chars ...]\n\n"
        + result[-keep_each:]
    )


def get_active_tool_definitions(all_tools: list[ToolDef] | None = None) -> list[ToolDef]:
    return all_tools if all_tools is not None else tool_definitions


@dataclass
class ToolExecutionMode:
    """工具执行模式。"""
    mode: str = "sequential"

    @classmethod
    def parallel(cls) -> ToolExecutionMode:
        return cls(mode="parallel")

    @classmethod
    def sequential(cls) -> ToolExecutionMode:
        return cls(mode="sequential")


TOOL_EXECUTION_MODES: dict[str, str] = {
    "read_file": "parallel",
    "outline_file": "parallel",
    "list_files": "parallel",
    "grep_search": "parallel",
    "web_search": "parallel",
    "shell_status": "parallel",
    "search_history": "parallel",
    "list_session_notes": "parallel",
    "write_file": "sequential",
    "edit_file": "sequential",
    "run_shell": "sequential",
    "skill_create": "sequential",
    "remember": "sequential",
    "compact_context": "sequential",
    "context_restore": "sequential",
    "enter_plan_mode": "sequential",
    "exit_plan_mode": "sequential",
    "agent": "sequential",
    "ask_user": "sequential",
    "task_list": "sequential",
}


def get_execution_mode(tool_name: str) -> str:
    return TOOL_EXECUTION_MODES.get(tool_name, "sequential")


tool_definitions: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file content with line numbers. Use offset and limit to read specific line ranges for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)"},
                "limit": {"type": "integer", "description": "Maximum number of lines to read (default: all lines)"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "outline_file",
        "description": "Show the structural outline of a file: classes/functions with line ranges (Python), heading sections (Markdown), top-level declarations and class members (TS/JS). Use it on large files BEFORE read_file to decide which section to read with offset/limit, instead of loading the whole file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to outline"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to write"},
                "content": {"type": "string", "description": "The content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly (including whitespace and indentation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace"},
                "new_string": {"type": "string", "description": "The string to replace it with"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": 'Glob pattern to match files (e.g., "**/*.ts", "src/**/*")'},
                "path": {"type": "string", "description": "Base directory to search from. Defaults to current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to current directory."},
                "include": {"type": "string", "description": 'File glob pattern to include (e.g., "*.ts", "*.py")'},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the public web. Dual backend: DuckDuckGo (primary) with automatic fallback to Exa AI search on failure. Use this for facts, documents, URLs, dates, and current information. Prefer it over curl or grep for internet research. The response includes which backend served the results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Maximum number of results to return (1-20, default: 8)"},
                "region": {"type": "string", "description": "Search region, such as us-en, cn-zh, wt-wt (default: us-en, ddgs backend only)"},
                "timelimit": {"type": "string", "enum": ["d", "w", "m", "y"], "description": "Optional recency filter: day, week, month, or year (ddgs backend only)"},
                "backend": {"type": "string", "enum": ["auto", "ddgs", "exa"], "description": "auto: ddgs first, fall back to exa on failure (default); or force a single backend"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return its output. Use this for running tests, installing packages, git operations, etc. Set background=true for long-running commands (servers, builds) to run them asynchronously and get a job_id immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000, ignored when background=true)"},
                "background": {"type": "boolean", "description": "If true, run asynchronously and return a job_id immediately (for long-running commands)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "shell_status",
        "description": "Check the status and output of background shell jobs started with run_shell(background=true). Omit job_id to list all background jobs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job_id returned by run_shell. Omit to list all jobs."},
            },
        },
    },
    {
        "name": "compact_context",
        "description": "Compact the current conversation context into structured session memory when the context is long, tool results are noisy, or a strategy reset is useful. This preserves task progress, current next steps, and tool-use experience, then continues from the folded memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for compacting now, such as long context, many tool calls, repeated failures, or strategy change.",
                },
            },
        },
    },
    {
        "name": "context_restore",
        "description": "Recover the original full content of a snipped or cleared tool result from the reversible context store. Use when you see a placeholder like '[Content snipped ... key ...]' and need the original content. The key is given inside the placeholder text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The restore key shown in the snipped/cleared placeholder"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "search_history",
        "description": "Search the conversation history (including hidden/folded messages) for keywords. Returns matching messages with sequence numbers. If hidden tool results are found, you can use context_restore to recover them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for in the conversation history"},
                "limit": {"type": "integer", "description": "Maximum number of results to return (default: 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_session_notes",
        "description": "List all session notes from previous sessions. Returns a list of session IDs with titles and timestamps. The latest session note content is included in the result for immediate reference.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of session notes to list (default: 10)"},
            },
        },
    },
    {
        "name": "git_diff_before_last_compress",
        "description": "Get the list of files changed before the last context compression. Returns file paths, status (added/modified/deleted), and line counts. Use this to understand what was modified before compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of file paths to filter. If provided, only returns changes for these files.",
                },
            },
        },
    },
    {
        "name": "git_diff_session",
        "description": "Get the list of files changed during a specific session. Returns file paths, status (added/modified/deleted), and line counts. Use this to understand what a session modified.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to get diff for.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of file paths to filter. If provided, only returns changes for these files.",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "skill",
        "description": "Invoke a registered skill by name and get its resolved instructions to follow. Available skills and their trigger conditions are listed in the 'Available Skills' section of the system prompt. Use this when the user's request matches a skill's When to use, or when the user types /<skill-name>.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill to invoke (exactly as listed in Available Skills)"},
                "args": {"type": "string", "description": "Optional arguments passed to the skill (substituted for $ARGUMENTS in its template)"},
            },
            "required": ["skill_name"],
        },
    },
    {
        "name": "skill_create",
        "description": "Create a new reusable skill from explicit durable workflow guidance when no suitable existing skill exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Concise reusable skill name"},
                "description": {"type": "string", "description": "One-sentence description of what the skill does and when to use it"},
                "instructions": {"type": "string", "description": "Reusable SKILL.md body. Focus on durable method, constraints, and workflow, not one-off task content."},
                "when_to_use": {"type": "string", "description": "Trigger condition for auto-invocation"},
                "target": {
                    "type": "string",
                    "enum": ["project", "user"],
                    "description": "Where to create the skill. Defaults to project.",
                },
                "context": {
                    "type": "string",
                    "enum": ["inline", "fork"],
                    "description": "Skill execution mode. Defaults to inline.",
                },
                "user_invocable": {"type": "boolean", "description": "Whether users can invoke it manually with /<skill>. Defaults to false."},
                "allowed_tools": {"type": "string", "description": "Optional comma-separated allowed tools for fork mode"},
                "evidence": {"type": "string", "description": "Short user-provided evidence showing why this is reusable"},
            },
            "required": ["name", "description", "instructions"],
        },
    },
    {
        "name": "enter_plan_mode",
        "description": "Enter plan mode to switch to a read-only planning phase. In plan mode, you can only read files and write to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "exit_plan_mode",
        "description": "Exit plan mode after you have finished writing your plan to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_user",
        "description": "向用户提问并等待回答。用于澄清需求、确认决策、或获取批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问的问题",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的选项列表。提供选项时优先使用。",
                },
                "context": {
                    "type": "string",
                    "description": "问题的背景信息",
                },
            },
            "required": ["question"],
        },
    },
    # 单一定义源在 task_tools.TASK_LIST_TOOL（schema 与 handler 同处一个模块，
    # 词表变更不会再悄悄分叉）。task_tools 不 import registry，无循环。
    TASK_LIST_TOOL,
    {
        "name": "agent",
        "description": "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and return their result wrapped in a <subagent session_id=... state=...> tag. RESUME: pass session_id (from that tag) to continue the same sub-agent — it keeps full memory of its earlier work, so the prompt only needs the new instruction. Omit session_id to spawn a fresh sub-agent — then the prompt must contain ALL needed context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
                "prompt": {"type": "string", "description": "Detailed task instructions. For a fresh sub-agent include full context; when resuming via session_id only the new instruction is needed"},
                "type": {"type": "string", "enum": ["explore", "reviewer", "general"], "description": "Agent type. Default: general. The enum and the available-types list in this tool's description are refreshed per request (including custom agents)"},
                "session_id": {"type": "string", "description": "Optional. The session_id of a sub-agent you previously spawned (from its <subagent session_id=...> result tag). Pass it to continue that sub-agent with its memory retained; omit to spawn a new one"},
                "background": {"type": "boolean", "description": "If true, launch in background and return immediately with the sub-agent's session_id (state=\"running\"). When it finishes, its result is automatically injected into this session and you will see it at the start of your next turn — do NOT sleep/poll or spawn a duplicate; if nothing else to do, end your reply"},
            },
            "required": ["description", "prompt"],
        },
    },
    {
        "name": "subagent_cancel",
        "description": "Hard-cancel a running sub-agent you spawned (typically a background one). Stops it immediately (within ~1s) — use this when the work is no longer needed or is stuck. To gracefully redirect a running sub-agent instead, resume it via the agent tool with its session_id (your prompt is steered in). The cancelled sub-agent's session stays persisted and can still be resumed later with the agent tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The sub-agent's session_id (from its <subagent session_id=...> result tag)"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "remember",
        "description": "Save a persistent memory to the wiki. Use IMMEDIATELY in the current turn when the user says 'remember X', 'always/never do X', 'from now on X', or corrects your behavior — such user-taught rules MUST use wiki_type=feedback even when they look like project conventions (litmus test: does it constrain how the agent should act? then feedback, not knowledge). Feedback content must include the Rule, Why, and How to apply. Deduplication is automatic: similar existing entries are merged, and the result tells you which entry was updated. Types: feedback (rules/corrections/confirmations the user teaches the agent), user (personal preferences), knowledge (objective project facts/architecture decisions/technical details), reference (external links/docs), workflow_pattern (troubleshooting pattern; requires symptom/root_cause/workaround instead of content).",
        "input_schema": {
            "type": "object",
            "properties": {
                "wiki_type": {"type": "string", "enum": ["feedback", "user", "knowledge", "reference", "workflow_pattern"], "description": "Memory type. feedback = any rule the user teaches/corrects/confirms ('remember...', 'never...', 'always...', 'from now on...'), even if it looks like a project convention; knowledge = objective project facts/architecture only; self_improvement (Agent's own mistakes) is NOT writable here"},
                "name": {"type": "string", "description": "Short descriptive entry name"},
                "description": {"type": "string", "description": "Optional one-line description"},
                "content": {"type": "string", "description": "Memory content (required except workflow_pattern). For feedback: Rule -> Why -> How to apply"},
                "symptom": {"type": "string", "description": "workflow_pattern only: what the user observes"},
                "root_cause": {"type": "string", "description": "workflow_pattern only: root cause"},
                "workaround": {"type": "string", "description": "workflow_pattern only: solution/troubleshooting steps"},
            },
            "required": ["wiki_type", "name"],
        },
    },
]
