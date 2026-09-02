"""LLM API 熔断器。

连续失败 N 次后熔断 M 秒，防止反复失败浪费资源。

状态机：CLOSED → OPEN → HALF_OPEN → CLOSED
"""

from __future__ import annotations

import time
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout

    def can_execute(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN

    @property
    def state(self) -> CircuitState:
        return self._state


llm_circuit_breaker = CircuitBreaker()
