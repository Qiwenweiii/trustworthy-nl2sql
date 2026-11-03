from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from data_agent.services.executor import (
    QueryTimeout,
    open_readonly_connection,
    run_query,
)


@pytest.fixture()
def demo_db(tmp_path):
    """创建只有 orders 表的临时库。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL);
            INSERT INTO orders (id, amount) VALUES (1, 10.0), (2, 20.0), (3, 30.0);
            """
        )
        conn.commit()
    return path


def test_row_cap_truncates_and_flags(demo_db):
    """超过上限时只返回前 N 行并标记截断。"""
    result = run_query(demo_db, "SELECT id FROM orders ORDER BY id", max_rows=2)
    assert result.columns == ["id"]
    assert result.rows == [{"id": 1}, {"id": 2}]
    assert result.truncated is True
    assert result.as_dict()["row_count"] == 2


def test_rows_become_dicts(demo_db):
    """结果行应转成列名到值的字典。"""
    result = run_query(demo_db, "SELECT id, amount FROM orders WHERE id = 1")
    assert result.rows == [{"id": 1, "amount": 10.0}]
    assert result.truncated is False


def test_authorizer_blocks_write_even_if_guard_is_bypassed(demo_db):
    """绕过静态校验直接写库也会被 authorizer 拒绝。"""
    with closing(open_readonly_connection(demo_db)) as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM orders")

    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3


def test_uri_readonly_blocks_write_at_database_layer(demo_db):
    """只读 URI 在数据库层拒绝写操作。"""
    conn = sqlite3.connect(f"file:{demo_db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM orders")
    finally:
        conn.close()


def test_timeout_interrupts_long_query(demo_db):
    """无限递归查询会被 progress handler 中断。"""
    sql = (
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) "
        "SELECT count(*) FROM cnt"
    )
    with pytest.raises(QueryTimeout):
        run_query(demo_db, sql, timeout_s=0.05)


def test_missing_db_raises(tmp_path):
    """库文件不存在时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        run_query(tmp_path / "missing.db", "SELECT 1")
