from __future__ import annotations

import pytest

from data_agent.services.sql_guard import check_sql


def _rules(sql: str) -> set[str]:
    """提取 SQL 命中的规则名集合。"""
    return {item["rule"] for item in check_sql(sql).violations}


@pytest.mark.parametrize(
    ("sql", "expected_rules"),
    [
        ("DELETE FROM orders", {"not_select", "forbidden_keyword"}),
        ("update orders set amount = 0", {"not_select", "forbidden_keyword"}),
        ("INSERT INTO orders (amount) VALUES (1)", {"not_select", "forbidden_keyword"}),
        ("DROP TABLE orders", {"not_select", "forbidden_keyword"}),
        ("PRAGMA table_info(orders)", {"not_select", "forbidden_keyword"}),
        ("ATTACH DATABASE '/tmp/x.db' AS x", {"not_select", "forbidden_keyword"}),
        ("SELECT 1; DELETE FROM orders", {"multiple_statements", "forbidden_keyword"}),
        ("WITH x AS (SELECT 1) DELETE FROM orders", {"forbidden_keyword"}),
        ("SELECT load_extension('/tmp/evil.so')", {"forbidden_function"}),
        ('SELECT "load_extension"(\'/tmp/evil.so\')', {"forbidden_function"}),
        ("SELECT [load_extension]('/tmp/evil.so')", {"forbidden_function"}),
        ('SELECT `edit`(\'/tmp/x\')', {"forbidden_function"}),
        ("REPLACE INTO orders (id) VALUES (1)", {"not_select", "forbidden_keyword"}),
        ("SELECT 1 WHERE replace INTO orders", {"forbidden_keyword"}),
        ("", {"empty_sql"}),
        ("SELECT 'unterminated", {"malformed_sql"}),
    ],
)
def test_blocked_sql_reports_rule(sql, expected_rules):
    """危险语句必须被拦，并给出预期的规则组合。"""
    result = check_sql(sql)
    assert result.passed is False
    assert _rules(sql) == expected_rules
    assert all(item["detail"] for item in result.violations)


def test_dangerous_function_reports_token():
    """危险函数拦截要带出具体函数名。"""
    result = check_sql("SELECT load_extension('/tmp/evil.so')")
    violation = result.violations[0]
    assert violation["rule"] == "forbidden_function"
    assert violation["token"] == "load_extension"


def test_quoted_dangerous_function_call_is_blocked_with_token():
    """S1 回归：函数名被双引号包裹仍算函数调用，静态层必须拦下并带 token。"""
    result = check_sql('SELECT "load_extension"(\'/tmp/evil.so\')')
    assert result.passed is False
    hits = {(v["rule"], v.get("token")) for v in result.violations}
    assert hits == {("forbidden_function", "load_extension")}


def test_readonly_replace_function_is_allowed():
    """S2 回归：replace() 是只读函数，不得按 forbidden_keyword 误杀。"""
    result = check_sql("SELECT replace(name, '张', '章') AS name FROM customers")
    assert result.passed is True
    assert result.violations == []


def test_replace_into_is_blocked_with_pair_token():
    """REPLACE INTO 是写语句，必须拦且 token 为 REPLACE INTO。"""
    result = check_sql("SELECT 1 WHERE replace INTO orders")
    assert result.passed is False
    hits = {(v["rule"], v.get("token")) for v in result.violations}
    assert hits == {("forbidden_keyword", "REPLACE INTO")}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, amount FROM orders",
        "select count(*) from orders;",
        "WITH recent AS (SELECT id FROM orders) SELECT count(*) FROM recent",
        "SELECT 'delete; drop table orders' AS note",
        "SELECT id FROM orders -- delete everything\n",
        'SELECT "delete" FROM orders',
        "SELECT id FROM orders /* DROP TABLE orders */ WHERE id > 1",
    ],
)
def test_allowed_select(sql):
    """合法的只读查询不应被误杀。"""
    result = check_sql(sql)
    assert result.passed is True
    assert result.violations == []


def test_trailing_semicolon_is_removed_but_literal_kept():
    """末尾分号被去掉，但字符串里的分号保留。"""
    result = check_sql("SELECT ';' AS x;")
    assert result.passed is True
    assert result.sql == "SELECT ';' AS x"
