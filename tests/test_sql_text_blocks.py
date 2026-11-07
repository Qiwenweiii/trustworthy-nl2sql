"""split_query_blocks 的单元测试：SQL 切成查询块的契约。

约定（阶段 4 方案 §5.1）：块列表外层在前；每块带 span/depth/tables/own_spans；
tables 只收"本块自己的 FROM/JOIN 直接引用的表名"（小写、去 schema 前缀）；
own_spans = 块文本扣掉所有后代查询块后的区间清单；括号不配对 = 判读不懂抛 SqlScanError。
"""

from __future__ import annotations

import pytest

from data_agent.services.sql_text import SqlScanError, in_spans, split_query_blocks


def test_single_select_is_one_block():
    sql = "SELECT sum(amount) FROM orders WHERE amount > 10"
    blocks = split_query_blocks(sql)
    assert len(blocks) == 1
    top = blocks[0]
    assert (top["start"], top["end"], top["depth"]) == (0, len(sql), 0)
    assert top["tables"] == {"orders"}
    assert top["own_spans"] == [(0, len(sql))]


def test_derived_table_inner_block_owns_orders():
    sql = (
        "SELECT cnt FROM (SELECT COUNT(*) AS cnt FROM orders) AS t "
        "WHERE status = 'paid'"
    )
    blocks = split_query_blocks(sql)
    assert len(blocks) == 2
    top, inner = blocks
    assert top["tables"] == set()
    assert inner["tables"] == {"orders"}
    assert inner["depth"] == 1
    # 外层 WHERE 落在外层自己的区域；子查询内容不属于外层
    outer_where = sql.index("WHERE status")
    assert in_spans(top["own_spans"], outer_where)
    assert not in_spans(top["own_spans"], sql.index("FROM orders"))
    # 内层区域覆盖子查询文本
    assert in_spans(inner["own_spans"], sql.index("COUNT(*)"))
    assert not in_spans(inner["own_spans"], outer_where)


def test_cte_body_is_a_block_referencing_orders():
    sql = (
        "WITH t AS (SELECT channel, COUNT(*) AS cnt FROM orders GROUP BY channel) "
        "SELECT channel, cnt FROM t WHERE status = 'paid'"
    )
    blocks = split_query_blocks(sql)
    assert len(blocks) == 2
    top, cte = blocks
    assert top["tables"] == {"t"}
    assert cte["tables"] == {"orders"}


def test_in_subquery_is_a_block_even_under_function_parens():
    sql = (
        "SELECT sum(amount) FROM orders "
        "WHERE customer_id IN (SELECT id FROM customers WHERE city = '北京')"
    )
    blocks = split_query_blocks(sql)
    assert len(blocks) == 2
    top, sub = blocks
    assert top["tables"] == {"orders"}
    assert sub["tables"] == {"customers"}


def test_nested_subqueries_become_nested_blocks():
    sql = (
        "SELECT count(*) FROM orders WHERE customer_id IN "
        "(SELECT customer_id FROM (SELECT id AS customer_id FROM customers) x)"
    )
    blocks = split_query_blocks(sql)
    assert len(blocks) == 3
    top, middle, inner = blocks
    assert top["tables"] == {"orders"}
    # 中间层的 FROM 指向派生表括号，不直接引用任何登记表
    assert middle["tables"] == set()
    assert inner["tables"] == {"customers"}
    # 文档序：外层起点严格递增
    assert top["start"] < middle["start"] < inner["start"]


def test_schema_prefixed_table_is_recorded_bare():
    blocks = split_query_blocks("SELECT count(*) FROM main.orders")
    assert blocks[0]["tables"] == {"orders"}


def test_join_tables_all_recorded_for_owning_block():
    sql = "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id"
    blocks = split_query_blocks(sql)
    assert len(blocks) == 1
    assert blocks[0]["tables"] == {"orders", "customers"}


def test_paren_mismatch_is_scan_error():
    with pytest.raises(SqlScanError):
        split_query_blocks("SELECT count(*) FROM orders WHERE (amount > 0")
    with pytest.raises(SqlScanError):
        split_query_blocks("SELECT count(*) FROM orders WHERE amount > 0)")
