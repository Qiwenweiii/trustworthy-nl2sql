from __future__ import annotations

from data_agent.services.llm_client import extract_caliber_note, extract_sql

SQL_ONLY = "```sql\nSELECT 1 AS a\n```"
WITH_NOTE = "```sql\nSELECT 1 AS a\n```\n口径：含税，按下单时间；已支付与已退款计入，取消不计入。"


def test_caliber_note_is_the_text_outside_the_sql_fence():
    """代码块之外的中文说明被当成口径声明，SQL 抽取不受影响。"""
    assert extract_sql(WITH_NOTE) == "SELECT 1 AS a"
    assert extract_caliber_note(WITH_NOTE) == "口径：含税，按下单时间；已支付与已退款计入，取消不计入。"


def test_missing_declaration_is_null_not_empty_string():
    """模型只回 SQL 时口径为 null，让下游能区分“没声明”和“声明是空串”。"""
    assert extract_caliber_note(SQL_ONLY) is None


def test_unfenced_reply_has_no_separable_declaration():
    """没有代码块就无法分离 SQL 与说明，不能把 SQL 本身当成口径声明交出去。"""
    assert extract_caliber_note("SELECT 1 AS a") is None


def test_refusal_has_no_declaration():
    """拒答只输出 NO_SQL，不进口径声明。"""
    assert extract_caliber_note("NO_SQL") is None
