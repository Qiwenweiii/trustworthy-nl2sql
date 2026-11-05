"""语义层指标定义与订单范围强制注入的单元测试。

考点是"不靠模型自觉"：缺条件要补上、写冲突要以定义为准改写、处理痕迹进记录。
"""

from __future__ import annotations

from data_agent.services.metrics import enforce_metric_filters, match_metric


def _types(record: dict) -> list[str]:
    return [action["type"] for action in record["actions"]]


def test_metric_matched_by_name_and_synonym():
    assert match_metric("上个月不含税销售额是多少")["name"] == "销售额"
    assert match_metric("本月订单量是多少")["name"] == "订单数"
    assert match_metric("上月 GMV 有多少")["name"] == "销售额"
    assert match_metric("把客户姓名里的张替换成章") is None


def test_no_metric_question_leaves_sql_untouched():
    sql = "SELECT count(*) FROM customers"
    out, record = enforce_metric_filters("查一下客户情况", sql)
    assert out == sql
    assert record is None


def test_missing_filter_appended_when_no_where_clause():
    out, record = enforce_metric_filters(
        "上个月销售额是多少", "SELECT sum(amount) AS total FROM orders"
    )
    assert out == "SELECT sum(amount) AS total FROM orders WHERE status = 'paid'"
    assert record["changed"] is True
    assert _types(record) == ["added"]


def test_missing_filter_appended_before_group_by():
    out, record = enforce_metric_filters(
        "上个月订单数按渠道分组",
        "SELECT channel, count(*) FROM orders GROUP BY channel ORDER BY 2",
    )
    assert (
        out
        == "SELECT channel, count(*) FROM orders WHERE status = 'paid' GROUP BY channel ORDER BY 2"
    )
    assert record["changed"] is True


def test_existing_where_is_wrapped_before_and():
    """原条件整体加括号再 AND：a OR b 不能被 AND 抢优先级。"""
    out, record = enforce_metric_filters(
        "销售额是多少",
        "SELECT sum(amount) FROM orders WHERE channel = 'app' OR channel = 'web'",
    )
    assert out == (
        "SELECT sum(amount) FROM orders "
        "WHERE (channel = 'app' OR channel = 'web') AND status = 'paid'"
    )
    assert record["changed"] is True


def test_where_with_trailing_semicolon_keeps_semicolon_last():
    out, _ = enforce_metric_filters(
        "销售额是多少", "SELECT sum(amount) FROM orders WHERE amount > 0;"
    )
    assert out.endswith("AND status = 'paid'")
    assert ";" not in out


def test_model_written_required_filter_is_not_touched():
    sql = (
        "SELECT sum(amount - COALESCE(tax_amount, 0)) FROM orders "
        "WHERE order_date LIKE '2026-09%' AND status = 'paid'"
    )
    out, record = enforce_metric_filters("上个月不含税销售额是多少", sql)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["already_present"]


def test_single_value_in_paid_counts_as_satisfied():
    sql = "SELECT count(*) FROM orders WHERE status IN ('paid')"
    out, record = enforce_metric_filters("订单数是多少", sql)
    assert out == sql
    assert record["changed"] is False


def test_conflicting_status_is_replaced_by_definition():
    out, record = enforce_metric_filters(
        "上个月销售额是多少",
        "SELECT sum(amount) AS total FROM orders WHERE status = 'refunded'",
    )
    assert out == "SELECT sum(amount) AS total FROM orders WHERE status = 'paid'"
    assert record["changed"] is True
    assert record["actions"][0]["type"] == "conflict_replaced"
    assert record["actions"][0]["from"] == "status = 'refunded'"


def test_inclusive_in_list_is_narrowed_to_required_value():
    """IN ('paid','refunded') 与定义"只算 paid"冲突：以指标定义为准改写。"""
    out, record = enforce_metric_filters(
        "销售额是多少", "SELECT sum(amount) FROM orders WHERE status IN ('paid', 'refunded')"
    )
    assert out == "SELECT sum(amount) FROM orders WHERE status = 'paid'"
    assert record["changed"] is True
    assert _types(record) == ["conflict_replaced"]


def test_exclusion_style_is_replaced_by_definition():
    out, record = enforce_metric_filters(
        "销售额是多少", "SELECT sum(amount) FROM orders WHERE status <> 'cancelled'"
    )
    assert out == "SELECT sum(amount) FROM orders WHERE status = 'paid'"
    assert record["changed"] is True


def test_alias_prefix_is_kept_when_replacing():
    out, record = enforce_metric_filters(
        "销售额是多少",
        "SELECT sum(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = 'refunded'",
    )
    assert "o.status = 'paid'" in out
    assert "refunded" not in out
    assert record["changed"] is True


def test_subquery_where_is_not_the_injection_target():
    """外层没有顶层 WHERE 时，条件补在外层末尾，不能钻进子查询的 WHERE。"""
    out, _ = enforce_metric_filters(
        "销售额是多少",
        "SELECT sum(amount) FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE city = '北京')",
    )
    assert out.endswith("AND status = 'paid'")
    assert "(customer_id IN (SELECT id FROM customers WHERE city = '北京'))" in out


def test_metric_matched_but_table_not_referenced_skips_injection():
    sql = "SELECT count(*) FROM customers"
    out, record = enforce_metric_filters("销售额是多少", sql)
    assert out == sql
    assert record is not None
    assert record["changed"] is False
    assert _types(record) == ["skipped"]


def test_record_carries_metric_and_required_filters():
    _, record = enforce_metric_filters("销售额是多少", "SELECT sum(amount) FROM orders")
    assert record["metric"] == "销售额"
    assert record["required_filters"] == ["status = 'paid'"]


def test_unparseable_sql_is_never_injected():
    """统一口径：引号未闭合=整句读不懂，不注入、原 SQL 一字不动。

    合并前 metrics 的遮罩对未闭合引号"不遮"，会在半坏 SQL 里照样补条件；
    这类 SQL 最终必然被 guard 拦成 malformed_sql，端到端状态不变，
    但 enforcement 不该留"改了坏 SQL"的脏痕迹。
    """
    sql = "SELECT sum(amount) FROM orders WHERE note='oops"
    out, record = enforce_metric_filters("销售额是多少", sql)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["skipped"]
