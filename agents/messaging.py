"""Agent-to-Agent messaging system.

Features:
- 同 supervisor 下的 Worker 间消息传递
- 消息大小和频率限制
- 投递模式: steer (注入活跃工作) / follow_up (等待空闲)
- 消息历史持久化
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .trace import trace_event


DEFAULT_MESSAGE_DIR = Path.home() / ".bear-code" / "messages"
MAX_MESSAGE_SIZE = 10_000  # 单条消息最大字符数
MAX_MESSAGES_PER_MINUTE = 30  # 每分钟最大消息数


class DeliveryMode(str, Enum):
    """消息投递模式。"""
    STEER = "steer"           # 注入到活跃工作中（打断当前任务）
    FOLLOW_UP = "follow_up"   # 等待空闲后投递


@dataclass
class AgentMessage:
    """Agent 间消息。"""
    from_agent: str
    to_agent: str
    content: str
    delivery_mode: DeliveryMode = DeliveryMode.FOLLOW_UP
    message_id: str = ""
    timestamp: float = field(default_factory=time.time)
    delivered: bool = False
    read: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "content": self.content,
            "delivery_mode": self.delivery_mode.value,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "delivered": self.delivered,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentMessage:
        return cls(
            from_agent=data["from_agent"],
            to_agent=data["to_agent"],
            content=data["content"],
            delivery_mode=DeliveryMode(data.get("delivery_mode", "follow_up")),
            message_id=data.get("message_id", ""),
            timestamp=data.get("timestamp", time.time()),
            delivered=data.get("delivered", False),
            read=data.get("read", False),
        )


class MessageBus:
    """Agent 间消息总线。"""

    def __init__(self, message_dir: Path = DEFAULT_MESSAGE_DIR):
        self.message_dir = message_dir
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._rate_limits: dict[str, list[float]] = {}
        self._history: list[AgentMessage] = []

    def _ensure_dir(self) -> None:
        self.message_dir.mkdir(parents=True, exist_ok=True)

    def register_agent(self, agent_id: str) -> None:
        """注册 Agent 到消息总线。"""
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()
            trace_event("messaging.register", agent_id=agent_id)

    def unregister_agent(self, agent_id: str) -> None:
        """从消息总线注销 Agent。"""
        if agent_id in self._queues:
            del self._queues[agent_id]
            trace_event("messaging.unregister", agent_id=agent_id)

    def _check_rate_limit(self, from_agent: str) -> bool:
        """检查发送频率限制。"""
        now = time.time()
        if from_agent not in self._rate_limits:
            self._rate_limits[from_agent] = []

        # 清理 1 分钟前的记录
        self._rate_limits[from_agent] = [
            t for t in self._rate_limits[from_agent] if now - t < 60
        ]

        if len(self._rate_limits[from_agent]) >= MAX_MESSAGES_PER_MINUTE:
            return False

        self._rate_limits[from_agent].append(now)
        return True

    async def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        delivery_mode: DeliveryMode = DeliveryMode.FOLLOW_UP,
    ) -> AgentMessage | None:
        """发送消息到指定 Agent。

        Args:
            from_agent: 发送方 Agent ID
            to_agent: 接收方 Agent ID
            content: 消息内容
            delivery_mode: 投递模式

        Returns:
            发送的消息，如果失败返回 None
        """
        # 验证
        if to_agent not in self._queues:
            trace_event("messaging.error", error=f"Agent not found: {to_agent}")
            return None

        if len(content) > MAX_MESSAGE_SIZE:
            trace_event("messaging.error", error="Message too large")
            return None

        if not self._check_rate_limit(from_agent):
            trace_event("messaging.error", error="Rate limit exceeded")
            return None

        # 创建消息
        message = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            delivery_mode=delivery_mode,
            message_id=f"{from_agent}_{int(time.time() * 1000)}",
        )

        # 投递
        await self._queues[to_agent].put(message)
        message.delivered = True

        # 记录历史
        self._history.append(message)
        self._save_history()

        trace_event(
            "messaging.send",
            from_agent=from_agent,
            to_agent=to_agent,
            delivery_mode=delivery_mode.value,
        )

        return message

    async def receive(self, agent_id: str, timeout: float | None = None) -> AgentMessage | None:
        """接收消息。

        Args:
            agent_id: 接收方 Agent ID
            timeout: 超时时间（秒）

        Returns:
            接收到的消息，超时返回 None
        """
        if agent_id not in self._queues:
            return None

        try:
            if timeout:
                message = await asyncio.wait_for(
                    self._queues[agent_id].get(),
                    timeout=timeout,
                )
            else:
                message = await self._queues[agent_id].get()

            message.read = True
            return message
        except asyncio.TimeoutError:
            return None

    def pending_count(self, agent_id: str) -> int:
        """获取待处理消息数量。"""
        if agent_id not in self._queues:
            return 0
        return self._queues[agent_id].qsize()

    def _save_history(self) -> None:
        """保存消息历史。"""
        self._ensure_dir()
        history_file = self.message_dir / "history.jsonl"
        with history_file.open("a", encoding="utf-8") as f:
            # 只保存最近的消息
            recent = self._history[-100:]
            for msg in recent[-1:]:  # 只写最新的
                f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

    def load_history(self) -> list[AgentMessage]:
        """加载消息历史。"""
        self._ensure_dir()
        history_file = self.message_dir / "history.jsonl"
        if not history_file.exists():
            return []

        messages = []
        try:
            for line in history_file.read_text().splitlines():
                if line.strip():
                    data = json.loads(line)
                    messages.append(AgentMessage.from_dict(data))
        except Exception:
            pass

        return messages


# ─── Convenience Functions ────────────────────────────────────────────────────


_global_bus: MessageBus | None = None


def get_message_bus() -> MessageBus:
    """获取全局消息总线。"""
    global _global_bus
    if _global_bus is None:
        _global_bus = MessageBus()
    return _global_bus


async def send_message(
    from_agent: str,
    to_agent: str,
    content: str,
    delivery_mode: DeliveryMode = DeliveryMode.FOLLOW_UP,
) -> AgentMessage | None:
    """发送消息的便捷函数。"""
    bus = get_message_bus()
    return await bus.send(from_agent, to_agent, content, delivery_mode)


async def receive_message(agent_id: str, timeout: float | None = None) -> AgentMessage | None:
    """接收消息的便捷函数。"""
    bus = get_message_bus()
    return await bus.receive(agent_id, timeout)
