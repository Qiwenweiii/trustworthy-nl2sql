"""只读执行器：read-only URI + authorizer 白名单 + 行数上限 + 看门狗超时。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .sql_guard import FORBIDDEN_FUNCTIONS, strip_trailing_semicolons

_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


class QueryTimeout(RuntimeError):
    """查询超过配置时长，已中断。"""


@dataclass(frozen=True)
class QueryResult:
    """只读查询结果，rows 已转成列名到值的字典。"""

    columns: list[str]
    rows: list[dict]
    truncated: bool
    max_rows: int
    elapsed_ms: float

    def as_dict(self) -> dict:
        """返回可直接序列化进 outcome 的结果字段。"""
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "empty_result": is_result_empty(self.rows),
            "truncated": self.truncated,
            "max_rows": self.max_rows,
            "elapsed_ms": self.elapsed_ms,
        }


def is_result_empty(rows: list[dict]) -> bool:
    """按结果形状判空：0 行，或每行的每个取值都是 SQL NULL（聚合无匹配的典型形态）。

    0 与空串是查出来的数值，不算空——否则会把合法的 0 答案误标成没数据。
    """
    return not rows or all(value is None for row in rows for value in row.values())


def _authorizer(action: int, arg1, arg2, dbname, trigger) -> int:
    """authorizer 白名单：只放行读操作，并额外拒绝危险函数调用。"""
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FORBIDDEN_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """以只读 URI 打开连接，并装上 authorizer 白名单。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.enable_load_extension(False)
    conn.set_authorizer(_authorizer)
    return conn


def _wrap_with_limit(sql: str, max_rows: int) -> str:
    """在查询外包一层 LIMIT，避免数据库返回全量结果。"""
    return f"SELECT * FROM (\n{strip_trailing_semicolons(sql)}\n) LIMIT {max_rows + 1}"


def run_query(
    db_path: Path, sql: str, *, max_rows: int = 200, timeout_s: float = 5.0
) -> QueryResult:
    """执行只读查询并截断到 max_rows；超时抛 QueryTimeout。"""
    if not db_path.exists():
        raise FileNotFoundError(f"演示库不存在: {db_path}")

    deadline = time.monotonic() + timeout_s
    timed_out = False

    def _on_progress() -> int:
        """progress handler：超过 deadline 时请求中断查询。"""
        nonlocal timed_out
        if time.monotonic() > deadline:
            timed_out = True
            return 1
        return 0

    started = time.monotonic()
    with closing(open_readonly_connection(db_path)) as conn:
        conn.set_progress_handler(_on_progress, 1000)
        try:
            cursor = conn.execute(_wrap_with_limit(sql, max_rows))
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.Error as exc:
            if timed_out:
                raise QueryTimeout(f"查询超过 {timeout_s} 秒，已中断") from exc
            raise

        columns = [description[0] for description in cursor.description or []]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]

    return QueryResult(
        columns=columns,
        rows=[dict(zip(columns, row)) for row in rows],
        truncated=truncated,
        max_rows=max_rows,
        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
    )
