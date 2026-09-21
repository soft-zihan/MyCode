"""Secrets 脱敏 — 纯正则，不做熵检测。

熵检测对 git 哈希 / embedding 缓存 / base64 图片误杀严重，segment 里这些恰是常客，
故只匹配已知密钥形态（✅ 2026-09-21 用户确认）。

调用点：
- segment 写入前（wiki_capture.capture_session_to_session）
- remember / 编译管道条目落库前
"""

from __future__ import annotations

import re

# (标签, 正则)。替换为 [REDACTED:{标签}]
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pem_private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    )),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    )),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
]

# key=value 形态：保留 key 名，值替换
_KV_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|api_key|apikey|secret_key|secret|access_token|auth_token|token)"
    r"(\s*[=:]\s*)"
    r"([\"']?)(\S{6,}?)(?=[\s\"',;}\)]|$)"
)

# 误报豁免：值是明显的占位符/引用而非密钥
_KV_FALSE_POSITIVES = re.compile(
    r"(?i)^(\*+|x{6,}|\.{3}|<[^>]*>?|\{\{.*|\$\{.*|%s|\{\d?\}|none|null|true|false|undefined)\}?$"
)


def redact_secrets(text: str) -> str:
    """把文本中的密钥形态字符串替换为 [REDACTED:{pattern}]。"""
    if not text:
        return text
    for label, pattern in _SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)

    def _kv_sub(m: re.Match[str]) -> str:
        value = m.group(4)
        if _KV_FALSE_POSITIVES.match(value):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:kv_secret]{m.group(3)}"

    return _KV_PATTERN.sub(_kv_sub, text)
