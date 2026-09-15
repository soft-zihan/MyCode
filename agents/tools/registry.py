"""Tool Registry - 工具注册表和执行模式定义。

每个工具可声明自己的执行模式：
- sequential: 必须顺序执行（有副作用）
- parallel: 可以并行执行（只读，无副作用）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

ToolDef = dict
PermissionMode = str

READ_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "compact_context", "shell_status", "search_history", "list_task_notes"}
EDIT_TOOLS = {"write_file", "edit_file", "skill_create", "write_workflow_pattern", "write_wiki_entry"}

CONCURRENCY_SAFE_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "shell_status"}

MAX_RESULT_CHARS = 50000

_activated_tools: set[str] = set()


def _truncate_result(result: str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep_each = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep_each]
        + f"\n\n[... truncated {len(result) - keep_each * 2} chars ...]\n\n"
        + result[-keep_each:]
    )


def reset_activated_tools() -> None:
    _activated_tools.clear()


def get_active_tool_definitions(all_tools: list[ToolDef] | None = None) -> list[ToolDef]:
    tools = all_tools if all_tools is not None else tool_definitions
    return [
        {k: v for k, v in t.items() if k != "deferred"}
        for t in tools
        if not t.get("deferred") or t["name"] in _activated_tools
    ]


def get_deferred_tool_names(all_tools: list[ToolDef] | None = None) -> list[str]:
    tools = all_tools if all_tools is not None else tool_definitions
    return [t["name"] for t in tools if t.get("deferred") and t["name"] not in _activated_tools]


def activate_tool(name: str) -> None:
    _activated_tools.add(name)


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
    "shell_status": "parallel",
    "search_history": "parallel",
    "list_task_notes": "parallel",
    "write_file": "sequential",
    "edit_file": "sequential",
    "run_shell": "sequential",
    "skill_create": "sequential",
    "write_workflow_pattern": "sequential",
    "write_wiki_entry": "sequential",
    "compact_context": "sequential",
    "context_restore": "sequential",
    "enter_plan_mode": "sequential",
    "exit_plan_mode": "sequential",
    "agent": "sequential",
    "tool_search": "sequential",
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
        "name": "list_task_notes",
        "description": "List all task notes from previous sessions. Returns a list of session IDs with titles and timestamps. The latest task note content is included in the result for immediate reference.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of task notes to list (default: 10)"},
            },
        },
    },
    {
        "name": "read_task_notes",
        "description": "Read the full content of a specific task note by session ID. This tool is automatically invoked when you need to read a specific task note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session ID to read task notes for. If omitted, reads the current session's notes."},
            },
        },
        "deferred": True,
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
        "deferred": True,
    },
    {
        "name": "exit_plan_mode",
        "description": "Exit plan mode after you have finished writing your plan to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "agent",
        "description": "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), 'general' (full tools).",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
                "prompt": {"type": "string", "description": "Detailed task instructions for the sub-agent"},
                "type": {"type": "string", "enum": ["explore", "plan", "general"], "description": "Agent type. Default: general"},
            },
            "required": ["description", "prompt"],
        },
    },
    {
        "name": "tool_search",
        "description": "Search for available tools by name or keyword. Returns full schema definitions for matching deferred tools so you can use them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool name or search keywords"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "write_workflow_pattern",
        "description": "Create a workflow_pattern entry in the wiki. Use this to record reusable troubleshooting workflows or operational procedures. The pattern will be automatically compiled into a skill when applied multiple times (applied_count >= 2).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pattern name (e.g., 'db-connection-timeout')"},
                "symptom": {"type": "string", "description": "Symptom description (what the user observes)"},
                "root_cause": {"type": "string", "description": "Root cause description"},
                "workaround": {"type": "string", "description": "Solution/troubleshooting steps"},
                "description": {"type": "string", "description": "Optional short description"},
            },
            "required": ["name", "symptom", "root_cause", "workaround"],
        },
    },
    {
        "name": "write_wiki_entry",
        "description": "Create a wiki entry for persistent memory. Use this to record important information that should be remembered across sessions. Types: knowledge (project info), self_improvement (lessons learned), user (user preferences), reference (external docs), workflow_pattern (troubleshooting workflows), plan (task plans).",
        "input_schema": {
            "type": "object",
            "properties": {
                "wiki_type": {"type": "string", "description": "Entry type: knowledge, self_improvement, user, reference, workflow_pattern, plan"},
                "name": {"type": "string", "description": "Entry name/title"},
                "content": {"type": "string", "description": "Entry content (markdown supported)"},
                "description": {"type": "string", "description": "Optional short description"},
            },
            "required": ["wiki_type", "name", "content"],
        },
    },
]
