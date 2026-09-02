"""请求上下文追踪。

使用 contextvars 存储 request_id，贯穿整个调用链，用于关联日志。
"""

from __future__ import annotations

import contextvars
import uuid

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('request_id', default='')


def get_request_id() -> str:
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def new_request_id() -> str:
    request_id = str(uuid.uuid4())[:8]
    set_request_id(request_id)
    return request_id
