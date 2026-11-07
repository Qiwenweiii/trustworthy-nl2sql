"""订单范围注入的块感知测试（阶段 4，KD-001 清偿）。

考点是"条件要落在真正看得见那一列的查询块里"：判定 already_present 只认
直接引用指标表那个块内的谓词；补写也只补进那个块。外层错误位置的谓词
**不删**——那是模型的产物，交给报错回炉处理（方案 §5.4 权限边界）。
F1/F2/F3/F5/F6 与回炉端到端在旧文本级实现上必须先红。
"""

from __future__ import annotations

from data_agent.services.metrics import enforce_metric_filters

_QUESTION = "今年订单数最多的渠道的订单数是多少"


def _types(record: dict) -> list[str]:
    return [action["type"] for action in record["actions"]]


def test_f1_outer_predicate_no_longer_counts_as_present():
    """D15 未救回现场（复盘 003）：谓词写在外层、内层缺条件——旧实现误判
    already_present 不动手，块感知后必须补进内层，外层原文一字不动。"""
    sql = (
        "SELECT channel, cnt FROM "
        "(SELECT channel, COUNT(*) AS cnt FROM orders GROUP BY channel) AS t "
        "WHERE status = 'paid' ORDER BY cnt DESC LIMIT 1"
    )
    out, record = enforce_metric_filters(_QUESTION, sql)
    assert _types(record) == ["added"]
    assert record["changed"] is True
    assert "status = 'paid' GROUP BY channel" in out
    assert out.count("status = 'paid'") == 2
    assert "WHERE status = 'paid' ORDER BY cnt DESC LIMIT 1" in out


def test_f2_repair_recheck_appends_inside_block_not_top():
    """回炉删除式修复后的复核：谓词全没了，补写点必须是内层块——
    旧实现补到顶层 WHERE，正是"补回同一错位置"的第二次红。"""
    sql = (
        "SELECT cnt FROM (SELECT COUNT(*) AS cnt FROM orders) AS t "
        "ORDER BY cnt DESC LIMIT 1"
    )
    out, record = enforce_metric_filters(_QUESTION, sql)
    assert _types(record) == ["added"]
    assert record["changed"] is True
    assert "(SELECT COUNT(*) AS cnt FROM orders WHERE status = 'paid') AS t" in out


def test_f3_cte_shape_injects_into_cte_body():
    """同病另一皮：WITH 支里查 orders，外层对着别名写谓词——补写进 WITH 支。"""
    sql = (
        "WITH t AS (SELECT channel, COUNT(*) AS cnt FROM orders GROUP BY channel) "
        "SELECT channel, cnt FROM t WHERE status = 'paid' ORDER BY cnt DESC LIMIT 1"
    )
    out, record = enforce_metric_filters(_QUESTION, sql)
    assert _types(record) == ["added"]
    assert "FROM orders WHERE status = 'paid' GROUP BY channel" in out
    assert out.count("status = 'paid'") == 2


def test_f4_top_level_shape_unchanged():
    """现网主流形态（单块）行为逐字不变——兼容性哨兵，实现前后都该绿。"""
    sql = "SELECT sum(amount) AS total FROM orders"
    out, record = enforce_metric_filters("上个月销售额是多少", sql)
    assert out == "SELECT sum(amount) AS total FROM orders WHERE status = 'paid'"
    assert _types(record) == ["added"]


def test_f4b_inner_predicate_is_genuine_already_present():
    """谓词本来就在正确的块里——不该被误伤重补（防过度修复的哨兵）。"""
    sql = (
        "SELECT cnt FROM (SELECT COUNT(*) AS cnt FROM orders WHERE status = 'paid') AS t"
    )
    out, record = enforce_metric_filters(_QUESTION, sql)
    assert out == sql
    assert _types(record) == ["already_present"]
    assert record["changed"] is False


def test_f5_multi_block_reference_skips():
    """两块各自直接引用 orders——位置不唯一，宁可不动（旧纪律的新形态）。"""
    sql = (
        "SELECT a.cnt, b.cnt FROM (SELECT COUNT(*) AS cnt FROM orders) a "
        "JOIN (SELECT COUNT(*) AS cnt FROM orders WHERE status = 'paid') b ON a.cnt = b.cnt"
    )
    out, record = enforce_metric_filters(_QUESTION, sql)
    assert out == sql
    assert _types(record) == ["skipped"]
    assert record["changed"] is False


def test_f6_paren_mismatch_skips_and_keeps_sql():
    """括号不配对 = 判读不懂，整条 SQL 一字不动（读不懂不动手通道）。"""
    sql = "SELECT sum(amount) FROM orders WHERE (amount > 0"
    out, record = enforce_metric_filters("销售额是多少", sql)
    assert out == sql
    assert _types(record) == ["skipped"]
    assert record["changed"] is False


def test_added_detail_names_the_block_for_non_top():
    """证据话术：补进非顶层块时 detail 说清补在哪一层，前端原样展示。"""
    sql = (
        "SELECT cnt FROM (SELECT COUNT(*) AS cnt FROM orders) AS t "
        "ORDER BY cnt DESC LIMIT 1"
    )
    _out, record = enforce_metric_filters(_QUESTION, sql)
    detail = record["actions"][0]["detail"]
    assert "status = 'paid'" in detail
    assert "子查询" in detail
