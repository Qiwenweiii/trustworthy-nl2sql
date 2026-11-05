"""读取 SQLite 表结构，渲染成提示词里的 schema 说明。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ..semantic_tables import COLUMN_NOTES


def describe_schema(db_path: Path) -> str:
    """读取表结构并渲染成带中文列说明的 schema 文本。"""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if not tables:
            raise RuntimeError(f"数据库 {db_path} 里没有业务表")

        blocks: list[str] = []
        for table in tables:
            lines = [f"表 {table}:"]
            for _cid, name, col_type, _notnull, _default, pk in conn.execute(
                f'PRAGMA table_info("{table}")'
            ):
                flags = " PK" if pk else ""
                note = COLUMN_NOTES.get(f"{table}.{name}", "")
                suffix = f"  -- {note}" if note else ""
                lines.append(f"  - {name} {col_type}{flags}{suffix}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
