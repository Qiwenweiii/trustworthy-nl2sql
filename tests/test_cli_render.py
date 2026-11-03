from __future__ import annotations

from data_agent.cli import render


def _ok_outcome(result: dict, llm_response: str | None = None) -> dict:
    """构造 status=ok 的 outcome，用于渲染断言。"""
    return {
        "question": "2025 年 1 月的销售额是多少",
        "status": "ok",
        "sql": "SELECT sum(amount) AS total FROM orders",
        "guard": {"passed": True, "violations": []},
        "result": result,
        "error": None,
        "llm_response": llm_response,
        "elapsed_ms": 1.0,
    }


def _result(rows: list[dict], *, empty_result: bool) -> dict:
    return {
        "columns": ["total"],
        "rows": rows,
        "row_count": len(rows),
        "empty_result": empty_result,
        "truncated": False,
        "max_rows": 200,
        "elapsed_ms": 0.5,
    }


def test_render_shows_no_data_notice_for_empty_result():
    """空结果标记为 True 时，人类可读输出要明说当前条件下没有数据。"""
    text = render(_ok_outcome(_result([{"total": None}], empty_result=True)))
    assert "当前条件下没有数据" in text


def test_render_keeps_normal_output_without_notice():
    """正常有数据的渲染保持原样：表格照出，不出现无数据提示。"""
    text = render(_ok_outcome(_result([{"total": 123.0}], empty_result=False)))
    assert "当前条件下没有数据" not in text
    assert "123.0" in text


def test_render_shows_caliber_declaration():
    """模型声明了口径，人类可读输出要把这行原样带给用户。"""
    outcome = _ok_outcome(_result([{"total": 123.0}], empty_result=False))
    outcome["caliber_note"] = "口径：不含税，按下单时间；取消订单不计入。"
    text = render(outcome)
    assert "口径：不含税，按下单时间；取消订单不计入。" in text


def test_render_omits_caliber_section_when_not_declared():
    """没有口径声明时不打空区块。"""
    text = render(_ok_outcome(_result([{"total": 123.0}], empty_result=False)))
    assert "口径" not in text
