"""Phase 3-5 测试：累计文件追踪 + 分支摘要 + Daemon + Extension 事件系统。

测试覆盖：
- session_memory.py: 文件操作提取和累计追踪
- session.py: 分支摘要生成和注入
- daemon.py: Daemon 基础结构
- scheduler.py: Cron 解析和任务调度
- messaging.py: Agent 间消息传递
- extensions/: 事件总线和扩展加载
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ── Phase 3.1: 累计文件追踪测试 ──────────────────────────────────────────────


class TestFileTracking:
    """测试文件操作提取和累计追踪。"""

    def test_extract_file_ops_read(self):
        from agents.session_memory import extract_file_ops

        messages = [
            {
                "role": "tool",
                "content": 'read_file path=/src/main.py',
            },
            {
                "role": "tool",
                "content": 'list_files dir=/src',
            },
        ]

        read_files, modified_files = extract_file_ops(messages)
        assert "/src/main.py" in read_files
        assert "/src" in read_files
        assert len(modified_files) == 0

    def test_extract_file_ops_write(self):
        from agents.session_memory import extract_file_ops

        messages = [
            {
                "role": "tool",
                "content": 'write_file path=/src/output.py',
            },
            {
                "role": "tool",
                "content": 'edit_file path=/src/config.py',
            },
        ]

        read_files, modified_files = extract_file_ops(messages)
        assert "/src/output.py" in modified_files
        assert "/src/config.py" in modified_files

    def test_extract_file_ops_openai_format(self):
        from agents.session_memory import extract_file_ops

        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/src/test.py"}',
                        },
                    },
                ],
            },
        ]

        read_files, _ = extract_file_ops(messages)
        assert "/src/test.py" in read_files

    def test_extract_file_ops_anthropic_format(self):
        from agents.session_memory import extract_file_ops

        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "input": {"path": "/src/new.py"},
                    },
                ],
            },
        ]

        _, modified_files = extract_file_ops(messages)
        assert "/src/new.py" in modified_files

    def test_merge_file_tracking(self):
        from agents.session_memory import merge_file_tracking

        current_read = ["/a.py", "/b.py"]
        current_modified = ["/c.py"]
        previous = {
            "details": {
                "readFiles": ["/x.py", "/y.py"],
                "modifiedFiles": ["/z.py"],
            },
        }

        merged_read, merged_modified = merge_file_tracking(
            current_read, current_modified, previous
        )

        assert set(merged_read) == {"/a.py", "/b.py", "/x.py", "/y.py"}
        assert set(merged_modified) == {"/c.py", "/z.py"}

    def test_merge_file_tracking_no_previous(self):
        from agents.session_memory import merge_file_tracking

        current_read = ["/a.py"]
        current_modified = ["/b.py"]

        merged_read, merged_modified = merge_file_tracking(
            current_read, current_modified, None
        )

        assert merged_read == ["/a.py"]
        assert merged_modified == ["/b.py"]


# ── Phase 3.2: 分支摘要测试 ──────────────────────────────────────────────────


class TestBranchSummary:
    """测试分支摘要生成和注入。"""

    def test_generate_branch_summary(self):
        from agents.session import generate_branch_summary

        entries = [
            {"id": "1", "role": "user", "content": "Hello"},
            {"id": "2", "role": "assistant", "content": "Hi there"},
            {"id": "3", "role": "user", "content": "How are you?"},
        ]

        summary = generate_branch_summary(entries)

        assert summary["type"] == "branch_summary"
        assert "summary" in summary
        assert summary["summary"]["user_turns"] == 2
        assert summary["summary"]["assistant_turns"] == 1

    def test_generate_branch_summary_with_ancestor(self):
        from agents.session import generate_branch_summary

        entries = [
            {"id": "1", "role": "user", "content": "First"},
            {"id": "2", "role": "assistant", "content": "Response"},
            {"id": "3", "role": "user", "content": "Second"},
        ]

        summary = generate_branch_summary(entries, common_ancestor_id="2")

        # 应该只包含到 ancestor 之前的条目
        assert summary["summary"]["user_turns"] == 1

    def test_format_branch_summary_for_injection(self):
        from agents.session import format_branch_summary_for_injection

        summaries = [
            {
                "type": "branch_summary",
                "summary": {
                    "user_turns": 5,
                    "assistant_turns": 3,
                    "tools_used": ["read_file", "write_file"],
                    "key_user_requests": ["Fix the bug"],
                },
            },
        ]

        result = format_branch_summary_for_injection(summaries)

        assert "<branch-history>" in result
        assert "User requests: 5 turns" in result
        assert "read_file" in result

    def test_format_branch_summary_empty(self):
        from agents.session import format_branch_summary_for_injection

        result = format_branch_summary_for_injection([])
        assert result == ""


# ── Phase 4.1: Daemon 测试 ───────────────────────────────────────────────────


class TestDaemon:
    """测试 Daemon 基础结构。"""

    def test_daemon_supervisor_init(self, tmp_path):
        from agents.daemon import DaemonSupervisor

        supervisor = DaemonSupervisor(
            socket_path=tmp_path / "test.sock",
            pid_file=tmp_path / "test.pid",
            log_file=tmp_path / "test.log",
        )

        assert supervisor.workers == {}
        assert supervisor._running is False

    def test_daemon_client_init(self, tmp_path):
        from agents.daemon import DaemonClient

        client = DaemonClient(socket_path=tmp_path / "test.sock")
        assert client.socket_path == tmp_path / "test.sock"


# ── Phase 4.2: Scheduler 测试 ────────────────────────────────────────────────


class TestScheduler:
    """测试 Cron 解析和任务调度。"""

    def test_cron_parser_wildcard(self):
        from agents.scheduler import CronParser

        parser = CronParser("* * * * *")
        assert parser.matches(datetime(2024, 1, 1, 12, 30))

    def test_cron_parser_specific_time(self):
        from agents.scheduler import CronParser

        parser = CronParser("30 12 * * *")
        assert parser.matches(datetime(2024, 1, 1, 12, 30))
        assert not parser.matches(datetime(2024, 1, 1, 12, 31))

    def test_cron_parser_step(self):
        from agents.scheduler import CronParser

        parser = CronParser("*/5 * * * *")
        assert parser.matches(datetime(2024, 1, 1, 12, 0))
        assert parser.matches(datetime(2024, 1, 1, 12, 5))
        assert parser.matches(datetime(2024, 1, 1, 12, 10))
        assert not parser.matches(datetime(2024, 1, 1, 12, 3))

    def test_cron_parser_range(self):
        from agents.scheduler import CronParser

        parser = CronParser("0 9-17 * * *")
        assert parser.matches(datetime(2024, 1, 1, 9, 0))
        assert parser.matches(datetime(2024, 1, 1, 17, 0))
        assert not parser.matches(datetime(2024, 1, 1, 8, 0))

    def test_cron_parser_invalid(self):
        from agents.scheduler import CronParser

        with pytest.raises(ValueError):
            CronParser("invalid")

    def test_scheduler_add_task(self, tmp_path):
        from agents.scheduler import Scheduler

        scheduler = Scheduler(schedule_file=tmp_path / "schedules.json")
        task = scheduler.add_task("test", "*/5 * * * *", "echo hello")

        assert task.task_id == "test"
        assert task.cron == "*/5 * * * *"
        assert "test" in scheduler.tasks

    def test_scheduler_remove_task(self, tmp_path):
        from agents.scheduler import Scheduler

        scheduler = Scheduler(schedule_file=tmp_path / "schedules.json")
        scheduler.add_task("test", "*/5 * * * *", "echo hello")
        assert scheduler.remove_task("test")
        assert "test" not in scheduler.tasks

    def test_scheduler_persistence(self, tmp_path):
        from agents.scheduler import Scheduler

        schedule_file = tmp_path / "schedules.json"

        # 添加任务
        scheduler1 = Scheduler(schedule_file=schedule_file)
        scheduler1.add_task("test", "*/5 * * * *", "echo hello")
        scheduler1.save()

        # 加载任务
        scheduler2 = Scheduler(schedule_file=schedule_file)
        scheduler2.load()
        assert "test" in scheduler2.tasks


# ── Phase 4.3: Messaging 测试 ────────────────────────────────────────────────


class TestMessaging:
    """测试 Agent 间消息传递。"""

    def test_message_bus_register(self, tmp_path):
        from agents.messaging import MessageBus

        bus = MessageBus(message_dir=tmp_path)
        bus.register_agent("agent1")
        bus.register_agent("agent2")

        assert "agent1" in bus._queues
        assert "agent2" in bus._queues

    @pytest.mark.asyncio
    async def test_message_bus_send_receive(self, tmp_path):
        from agents.messaging import MessageBus

        bus = MessageBus(message_dir=tmp_path)
        bus.register_agent("agent1")
        bus.register_agent("agent2")

        # 发送消息
        msg = await bus.send("agent1", "agent2", "Hello!")
        assert msg is not None
        assert msg.from_agent == "agent1"
        assert msg.to_agent == "agent2"
        assert msg.content == "Hello!"

        # 接收消息
        received = await bus.receive("agent2", timeout=1.0)
        assert received is not None
        assert received.content == "Hello!"

    @pytest.mark.asyncio
    async def test_message_bus_rate_limit(self, tmp_path):
        from agents.messaging import MessageBus, MAX_MESSAGES_PER_MINUTE

        bus = MessageBus(message_dir=tmp_path)
        bus.register_agent("agent1")
        bus.register_agent("agent2")

        # 发送大量消息
        for _ in range(MAX_MESSAGES_PER_MINUTE):
            await bus.send("agent1", "agent2", "msg")

        # 超过限制应该失败
        result = await bus.send("agent1", "agent2", "overflow")
        assert result is None

    @pytest.mark.asyncio
    async def test_message_bus_size_limit(self, tmp_path):
        from agents.messaging import MessageBus, MAX_MESSAGE_SIZE

        bus = MessageBus(message_dir=tmp_path)
        bus.register_agent("agent1")
        bus.register_agent("agent2")

        # 超大消息应该失败
        result = await bus.send("agent1", "agent2", "x" * (MAX_MESSAGE_SIZE + 1))
        assert result is None

    def test_message_bus_pending_count(self, tmp_path):
        from agents.messaging import MessageBus

        bus = MessageBus(message_dir=tmp_path)
        bus.register_agent("agent1")

        assert bus.pending_count("agent1") == 0


# ── Phase 5: Extension 事件系统测试 ──────────────────────────────────────────


class TestEventBus:
    """测试事件总线。"""

    @pytest.mark.asyncio
    async def test_event_bus_subscribe_emit(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe(EventType.SESSION_START, handler)
        await bus.emit_simple(EventType.SESSION_START, session_id="test")

        assert len(received) == 1
        assert received[0].type == EventType.SESSION_START

    @pytest.mark.asyncio
    async def test_event_bus_intercept(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()

        async def blocker(event: Event):
            event.block("Blocked!")

        bus.subscribe(EventType.TOOL_CALL, blocker)

        event = Event(type=EventType.TOOL_CALL, data={"tool_name": "test"})
        result = await bus.emit(event)

        assert result.blocked is True
        assert result.block_reason == "Blocked!"

    @pytest.mark.asyncio
    async def test_event_bus_modify(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()

        async def modifier(event: Event):
            event.modify({"modified": True})

        bus.subscribe(EventType.TOOL_RESULT, modifier)

        event = Event(type=EventType.TOOL_RESULT, data={"result": "original"})
        result = await bus.emit(event)

        assert result.modified is True
        assert result.modified_data == {"modified": True}

    @pytest.mark.asyncio
    async def test_event_bus_priority(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()
        order = []

        async def handler1(event: Event):
            order.append(1)

        async def handler2(event: Event):
            order.append(2)

        bus.subscribe(EventType.TURN_START, handler1, priority=0)
        bus.subscribe(EventType.TURN_START, handler2, priority=10)

        await bus.emit_simple(EventType.TURN_START)

        # 高优先级先执行
        assert order == [2, 1]

    def test_event_bus_unsubscribe(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe(EventType.SESSION_START, handler)
        bus.unsubscribe(handler)

        # 手动触发（不通过 emit）来验证取消订阅
        assert len(bus._handlers.get(EventType.SESSION_START, [])) == 0

    def test_event_bus_list_subscriptions(self):
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()

        async def handler(event: Event):
            pass

        bus.subscribe(EventType.SESSION_START, handler)
        bus.subscribe(EventType.TURN_START, handler)

        subs = bus.list_subscriptions()
        assert "session_start" in subs
        assert "turn_start" in subs


class TestExtensionLoader:
    """测试扩展加载器。"""

    def test_extension_loader_discover(self, tmp_path):
        from agents.extensions.loader import ExtensionLoader

        # 创建测试扩展
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "test_ext.py").write_text("""
def setup(ext):
    pass
""")

        loader = ExtensionLoader(extensions_dir=ext_dir)
        discovered = loader.discover()

        assert len(discovered) == 1
        assert discovered[0].name == "test_ext"

    def test_extension_loader_load(self, tmp_path):
        from agents.extensions.loader import ExtensionLoader

        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "test_ext.py").write_text("""
def setup(ext):
    pass
""")

        loader = ExtensionLoader(extensions_dir=ext_dir)
        loader.discover()
        results = loader.load_all()

        assert results["test_ext"] is True
        assert loader.extensions["test_ext"].module is not None
        assert hasattr(loader.extensions["test_ext"].module, "setup")

    def test_extension_loader_reload(self, tmp_path):
        from agents.extensions.loader import ExtensionLoader

        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        ext_file = ext_dir / "test_ext.py"
        ext_file.write_text("VERSION = 1\ndef setup(ext): pass")

        loader = ExtensionLoader(extensions_dir=ext_dir)
        loader.discover()
        loader.load_all()

        # 修改扩展
        ext_file.write_text("VERSION = 2\ndef setup(ext): pass")

        # 热重载
        assert loader.reload("test_ext")
        # 验证模块已重新加载
        assert loader.extensions["test_ext"].module is not None


class TestExtensionAPI:
    """测试扩展 API。"""

    def test_api_register_tool(self):
        from agents.extensions.api import ExtensionAPI
        from agents.extensions.event_bus import EventBus

        bus = EventBus()
        api = ExtensionAPI("test_ext", bus)

        api.register_tool({
            "name": "my_tool",
            "description": "Test tool",
            "parameters": {},
            "handler": lambda: None,
        })

        tools = api.get_tools()
        assert "my_tool" in tools

    def test_api_register_command(self):
        from agents.extensions.api import ExtensionAPI
        from agents.extensions.event_bus import EventBus

        bus = EventBus()
        api = ExtensionAPI("test_ext", bus)

        async def handler(args):
            return "done"

        api.register_command("mycommand", handler)

        commands = api.get_commands()
        assert "mycommand" in commands

    def test_api_unregister_all(self):
        from agents.extensions.api import ExtensionAPI
        from agents.extensions.event_bus import EventBus, Event, EventType

        bus = EventBus()
        api = ExtensionAPI("test_ext", bus)

        async def handler(event):
            pass

        api.subscribe(EventType.SESSION_START, handler)
        api.register_tool({"name": "tool", "handler": lambda: None})
        api.register_command("cmd", lambda: None)

        api.unregister_all()

        assert len(api.get_tools()) == 0
        assert len(api.get_commands()) == 0


# ── 运行测试 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
