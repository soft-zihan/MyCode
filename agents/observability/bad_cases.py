"""Bad Case 检测与存储模块。

设计：
- 使用 SQLite 存储 bad case 记录
- 支持用户反馈（踩按钮）和自动检测两种来源
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class BadCaseSource(str, Enum):
    """Bad case 来源。"""
    USER_FEEDBACK = "user_feedback"  # 用户踩按钮
    AUTO_DETECT = "auto_detect"  # 自动检测（工具重复/循环等）
    EVAL = "eval"  # 评测失败


class BadCaseStatus(str, Enum):
    """Bad case 状态。"""
    PENDING = "pending"  # 待审核
    VERIFIED = "verified"  # 已验证（可复现）
    FLAKY = "flaky"  # 偶发（不可复现）
    FIXED = "fixed"  # 已修复
    IGNORED = "ignored"  # 已忽略


class BadCaseSeverity(str, Enum):
    """Bad case 严重程度。"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class BadCase:
    """Bad case 记录。"""
    id: str
    session_id: str
    source: BadCaseSource
    status: BadCaseStatus = BadCaseStatus.PENDING
    severity: BadCaseSeverity = BadCaseSeverity.MEDIUM
    
    # 定位信息
    turn_number: int | None = None
    step_number: int | None = None
    tool_name: str | None = None
    
    # 问题描述
    signal_type: str = ""  # 信号类型：wrong_tool, incomplete, memory_not_used, other, tool_repeat, tool_cycle, force_stop, eval_failure
    reason: str = ""  # 问题原因
    comment: str = ""  # 用户/系统备注
    
    # 用户反馈（踩按钮）
    expected_tool: str | None = None  # 用户期望的工具名
    
    # 时间戳
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _db_path() -> Path:
    """获取数据库路径。"""
    db_dir = Path.home() / ".mycode" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "bad_cases.db"


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接。"""
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """初始化数据库表。"""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bad_cases (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'medium',
                turn_number INTEGER,
                step_number INTEGER,
                tool_name TEXT,
                signal_type TEXT NOT NULL,
                reason TEXT,
                comment TEXT,
                expected_tool TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bad_cases_session 
            ON bad_cases(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bad_cases_status 
            ON bad_cases(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bad_cases_source 
            ON bad_cases(source)
        """)
        conn.commit()
    finally:
        conn.close()


def _row_to_bad_case(row: sqlite3.Row) -> BadCase:
    """将数据库行转换为 BadCase 对象。"""
    return BadCase(
        id=row["id"],
        session_id=row["session_id"],
        source=BadCaseSource(row["source"]),
        status=BadCaseStatus(row["status"]),
        severity=BadCaseSeverity(row["severity"]),
        turn_number=row["turn_number"],
        step_number=row["step_number"],
        tool_name=row["tool_name"],
        signal_type=row["signal_type"],
        reason=row["reason"] or "",
        comment=row["comment"] or "",
        expected_tool=row["expected_tool"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_bad_case(bad_case: BadCase) -> BadCase:
    """创建 bad case 记录。"""
    _init_db()
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO bad_cases (
                id, session_id, source, status, severity,
                turn_number, step_number, tool_name,
                signal_type, reason, comment, expected_tool,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bad_case.id,
                bad_case.session_id,
                bad_case.source.value,
                bad_case.status.value,
                bad_case.severity.value,
                bad_case.turn_number,
                bad_case.step_number,
                bad_case.tool_name,
                bad_case.signal_type,
                bad_case.reason,
                bad_case.comment,
                bad_case.expected_tool,
                bad_case.created_at,
                bad_case.updated_at,
            ),
        )
        conn.commit()
        return bad_case
    finally:
        conn.close()


def get_bad_case(bad_case_id: str) -> BadCase | None:
    """获取单个 bad case。"""
    _init_db()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bad_cases WHERE id = ?",
            (bad_case_id,),
        ).fetchone()
        return _row_to_bad_case(row) if row else None
    finally:
        conn.close()


def list_bad_cases(
    session_id: str | None = None,
    source: BadCaseSource | None = None,
    status: BadCaseStatus | None = None,
    limit: int = 100,
) -> list[BadCase]:
    """列出 bad cases。"""
    _init_db()
    conn = _get_conn()
    try:
        query = "SELECT * FROM bad_cases WHERE 1=1"
        params: list[Any] = []
        
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if source:
            query += " AND source = ?"
            params.append(source.value)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(query, params).fetchall()
        return [_row_to_bad_case(row) for row in rows]
    finally:
        conn.close()


def update_bad_case(
    bad_case_id: str,
    status: BadCaseStatus | None = None,
    severity: BadCaseSeverity | None = None,
) -> BadCase | None:
    """更新 bad case。"""
    _init_db()
    conn = _get_conn()
    try:
        updates = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        
        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
        if severity is not None:
            updates.append("severity = ?")
            params.append(severity.value)
        
        params.append(bad_case_id)
        conn.execute(
            f"UPDATE bad_cases SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        
        return get_bad_case(bad_case_id)
    finally:
        conn.close()


def delete_bad_case(bad_case_id: str) -> bool:
    """删除 bad case。"""
    _init_db()
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM bad_cases WHERE id = ?",
            (bad_case_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def count_bad_cases(
    source: BadCaseSource | None = None,
    status: BadCaseStatus | None = None,
) -> int:
    """统计 bad case 数量。"""
    _init_db()
    conn = _get_conn()
    try:
        query = "SELECT COUNT(*) FROM bad_cases WHERE 1=1"
        params: list[Any] = []
        
        if source:
            query += " AND source = ?"
            params.append(source.value)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        
        row = conn.execute(query, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()
