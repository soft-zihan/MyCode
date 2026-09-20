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

READ_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "compact_context", "shell_status", "search_history", "list_session_notes", "plan_status", "plan_list", "plan_read_artifact", "plan_explore", "plan_continue", "plan_retry", "plan_recall", "plan_check_expired"}
EDIT_TOOLS = {"write_file", "edit_file", "skill_create", "write_workflow_pattern", "write_wiki_entry", "plan_propose", "plan_update", "plan_task_start", "plan_task_done", "plan_task_failed", "plan_complete", "plan_add_artifact", "plan_archive", "plan_save_explore", "plan_abandon", "plan_reopen", "plan_pause", "plan_resume", "plan_skip", "plan_redo", "plan_rollback", "ask_user", "todolist"}

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
    "list_session_notes": "parallel",
    "plan_status": "parallel",
    "plan_list": "parallel",
    "plan_read_artifact": "parallel",
    "plan_explore": "parallel",
    "plan_continue": "parallel",
    "plan_retry": "parallel",
    "plan_recall": "parallel",
    "plan_check_expired": "parallel",
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
    "plan_propose": "sequential",
    "plan_update": "sequential",
    "plan_task_start": "sequential",
    "plan_task_done": "sequential",
    "plan_task_failed": "sequential",
    "plan_complete": "sequential",
    "plan_add_artifact": "sequential",
    "plan_archive": "sequential",
    "plan_save_explore": "sequential",
    "plan_abandon": "sequential",
    "plan_reopen": "sequential",
    "plan_pause": "sequential",
    "plan_resume": "sequential",
    "plan_skip": "sequential",
    "plan_redo": "sequential",
    "plan_rollback": "sequential",
    "ask_user": "sequential",
    "todolist": "sequential",
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
    {
        "name": "todolist",
        "description": "管理任务清单。用于追踪多步任务的进度。Plan 模式下禁用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["add", "update", "remove", "list"],
                    "description": "操作类型",
                },
                "id": {
                    "type": "integer",
                    "description": "任务 ID（update/remove 时必需）",
                },
                "content": {
                    "type": "string",
                    "description": "任务内容（add 时必需，update 时可选）",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                    "description": "任务状态（update 时可选）",
                },
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "任务优先级（add 时可选，默认 medium）",
                },
            },
            "required": ["operation"],
        },
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
        "description": "Create a wiki entry for persistent memory. Use this to record important information that should be remembered across sessions. Types: knowledge (project info), self_improvement (lessons learned), user (user preferences), reference (external docs), workflow_pattern (troubleshooting workflows).",
        "input_schema": {
            "type": "object",
            "properties": {
                "wiki_type": {"type": "string", "description": "Entry type: knowledge, self_improvement, user, reference, workflow_pattern"},
                "name": {"type": "string", "description": "Entry name/title"},
                "content": {"type": "string", "description": "Entry content (markdown supported)"},
                "description": {"type": "string", "description": "Optional short description"},
            },
            "required": ["wiki_type", "name", "content"],
        },
    },
    {
        "name": "plan_propose",
        "description": "Create a new plan for structured task execution. Plans have a slug, priority, tags, and granularity (minimal/standard/full).",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "URL-safe plan identifier (e.g., 'add-dark-mode')"},
                "priority": {"type": "string", "description": "Priority level (P0-P4, default: P2)"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "granularity": {"type": "string", "enum": ["minimal", "standard", "full"], "description": "Plan granularity (default: minimal)"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_status",
        "description": "Get the status and task list of a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_list",
        "description": "List all active plans (or all plans including archived).",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_archived": {"type": "boolean", "description": "Include archived plans (default: false)"},
            },
        },
    },
    {
        "name": "plan_update",
        "description": "Update plan status or tags.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "status": {"type": "string", "enum": ["proposed", "in-progress", "paused", "completed", "archived", "abandoned"], "description": "New status"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "New tags"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_task_start",
        "description": "Mark a task as in-progress before starting implementation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID (1-indexed)"},
            },
            "required": ["slug", "task_id"],
        },
    },
    {
        "name": "plan_task_done",
        "description": "Mark a task as done after successful implementation and verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID (1-indexed)"},
                "commit": {"type": "string", "description": "Git commit hash (optional)"},
                "verification": {"type": "string", "description": 'REQUIRED. Verification evidence as JSON string: {"command": "...", "exit_code": 0, "output_snippet": "..."}'},
            },
            "required": ["slug", "task_id", "verification"],
        },
    },
    {
        "name": "plan_task_failed",
        "description": "Mark a task as failed with error information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID (1-indexed)"},
                "error": {"type": "string", "description": "Error message"},
            },
            "required": ["slug", "task_id"],
        },
    },
    {
        "name": "plan_complete",
        "description": "Mark the entire plan as completed (ready to archive). Call this after all tasks are done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_add_artifact",
        "description": "Add an artifact file to a plan (proposal.md, design.md, specs/*.md, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "filename": {"type": "string", "description": "Artifact filename (e.g., 'proposal.md', 'specs/auth-flow.md')"},
                "content": {"type": "string", "description": "Artifact content"},
            },
            "required": ["slug", "filename", "content"],
        },
    },
    {
        "name": "plan_read_artifact",
        "description": "Read an artifact file from a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "filename": {"type": "string", "description": "Artifact filename"},
            },
            "required": ["slug", "filename"],
        },
    },
    {
        "name": "plan_archive",
        "description": "Archive a completed plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_explore",
        "description": "Get an explore prompt for researching a topic before creating a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to explore"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "plan_save_explore",
        "description": "Save explore results for future reference.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic that was explored"},
                "content": {"type": "string", "description": "Explore results content"},
            },
            "required": ["topic", "content"],
        },
    },
    {
        "name": "plan_continue",
        "description": "Continue executing the next task in a plan. Returns a prompt for the sub-agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_retry",
        "description": "Retry a failed task in a plan. Returns a prompt for the sub-agent with error context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID to retry"},
            },
            "required": ["slug", "task_id"],
        },
    },
    {
        "name": "plan_recall",
        "description": "Search for relevant plans using semantic matching, tags, and status filtering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"},
                "include_archived": {"type": "boolean", "description": "Include archived plans (default: false)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "plan_abandon",
        "description": "Abandon a proposed or in-progress plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_reopen",
        "description": "Reopen a completed or abandoned plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_check_expired",
        "description": "Check for plans that have been inactive for too long.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "plan_pause",
        "description": "Pause an in-progress plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_resume",
        "description": "Resume a paused plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "plan_skip",
        "description": "Skip a task in a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID to skip"},
            },
            "required": ["slug", "task_id"],
        },
    },
    {
        "name": "plan_redo",
        "description": "Redo a completed task (reset to pending).",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "task_id": {"type": "integer", "description": "Task ID to redo"},
            },
            "required": ["slug", "task_id"],
        },
    },
    {
        "name": "plan_rollback",
        "description": "Rollback plan to a specific task's state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Plan slug"},
                "to_task": {"type": "integer", "description": "Task ID to rollback to"},
            },
            "required": ["slug", "to_task"],
        },
    },
]
