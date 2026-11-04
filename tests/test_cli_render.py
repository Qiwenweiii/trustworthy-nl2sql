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


def test_render_shows_metric_enforcement_when_sql_was_patched():
    """SQL 被系统补过/改过条件时，人类可读输出要说明是哪个指标、做了什么。"""
    outcome = _ok_outcome(_result([{"total": 123.0}], empty_result=False))
    outcome["enforcement"] = {
        "metric": "销售额",
        "required_filters": ["status = 'paid'"],
        "changed": True,
        "actions": [
            {"type": "added", "to": "status = 'paid'", "detail": "缺少必需条件，系统补入 status = 'paid'"}
        ],
    }
    text = render(outcome)
    assert "指标强制（销售额）" in text
    assert "系统补入 status = 'paid'" in text


def test_render_keeps_quiet_when_nothing_was_injected():
    """没改 SQL（changed=False）或没有 enforcement 字段时，不打指标强制区块。"""
    outcome = _ok_outcome(_result([{"total": 123.0}], empty_result=False))
    outcome["enforcement"] = {
        "metric": "销售额",
        "required_filters": ["status = 'paid'"],
        "changed": False,
        "actions": [
            {"type": "already_present", "to": "status = 'paid'", "detail": "已含 status = 'paid'，未改写"}
        ],
    }
    assert "指标强制" not in render(outcome)


def test_render_shows_time_boundary_fix():
    """时间边界被系统改过时，人类可读输出要说清改了哪条边界。"""
    outcome = _ok_outcome(_result([{"total": 123.0}], empty_result=False))
    outcome["time_fix"] = {
        "changed": True,
        "actions": [
            {
                "type": "boundary_rewritten",
                "from": "paid_at BETWEEN '2026-09-01' AND '2026-09-30'",
                "to": "paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
                "detail": "paid_at 带时分，闭区间上界会漏掉当天数据，改写为半开区间",
            }
        ],
    }
    text = render(outcome)
    assert "时间边界修正" in text
    assert "闭区间上界会漏掉当天数据" in text


def test_render_keeps_quiet_when_boundary_needs_no_fix():
    """没改（DATE 字段本来就对）或没有 time_fix 字段时，不打时间边界区块。"""
    outcome = _ok_outcome(_result([{"total": 123.0}], empty_result=False))
    outcome["time_fix"] = {
        "changed": False,
        "actions": [
            {"type": "already_correct", "detail": "order_date 只有日期，闭区间边界不需要改"}
        ],
    }
    assert "时间边界" not in render(outcome)
    assert "时间边界" not in render(_ok_outcome(_result([{"total": 123.0}], empty_result=False)))
