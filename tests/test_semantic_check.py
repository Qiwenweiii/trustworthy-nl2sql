"""语义层拒答判定：纯字符串运算，不调模型、不连数据库。

用例问题直接读 problems/eval_cases.json，改了登记表就会在这里红，不用等花钱的评测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent.semantic_tables import (
    COLUMN_NOTES,
    DIMENSIONS,
    REASON_CODES,
)
from data_agent.services.semantic_check import (
    check_question,
    supported_dimensions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_CASES = json.loads((PROJECT_ROOT / "problems" / "eval_cases.json").read_text(encoding="utf-8"))["cases"]

# 真模型用例里该拒答的三条；S1/S2 走 injected_sql，问题层检查不参与
EXPECTED_REFUSAL = {"R1", "R2", "R3"}


def _non_injected_cases():
    return [case for case in EVAL_CASES if not case.get("injected_sql")]


@pytest.mark.parametrize("case", _non_injected_cases(), ids=lambda case: case["id"])
def test_eval_case_questions_get_expected_verdict(case):
    """11 条评测用例里每条问题都得判对：R 组拒答，K/E 组放行。"""
    verdict = check_question(case["question"])
    should_refuse = case["id"] in EXPECTED_REFUSAL
    assert verdict["allowed"] is not should_refuse, (
        f"{case['id']} 期望{'拒答' if should_refuse else '放行'}，实际 {verdict}"
    )


def test_refusal_reason_distinguishes_dimension_from_metric():
    """拒答原因要分得清缺的是维度还是指标——R1 缺维度，R2 缺指标。"""
    assert check_question("上个月华东地区的销售额是多少")["reason_code"] == "missing_dimension"
    assert check_question("上个月各产品线的毛利率是多少")["reason_code"] == "missing_metric"


def test_unregistered_qualifier_refused_even_when_metric_matched():
    """R3 的关键：指标「销售额」匹配得上，但"新客"没定义，照样拒答。"""
    verdict = check_question("上个月新客销售额是多少")
    assert verdict["allowed"] is False
    assert verdict["reason_code"] == "unknown_qualifier"
    assert verdict["metric"] == "销售额"
    assert [item["term"] for item in verdict["residues"]] == ["新客"]


@pytest.mark.parametrize("question", [
    "上个月销售额是多少",
    "上个月不含税销售额是多少",
    "上个月按支付时间算的不含税销售额是多少",
    "2025 年 1 月的销售额是多少",
    "抖音渠道的销售额是多少",
])
def test_registered_vocabulary_is_allowed(question):
    """口径词、时间字段说法、时间表达、维度词都在登记表里，不该被拦。"""
    assert check_question(question)["allowed"] is True


@pytest.mark.parametrize("question", [
    "各区域销售额是多少",
    "按省份看销售额",
    "上海地区的销售额是多少",
    "按产品线看销售额",
])
def test_unregistered_dimension_words_refused(question):
    """按什么拆的问题里出现了没登记的拆分口径，拒答而不是让模型猜映射。"""
    assert check_question(question)["reason_code"] == "missing_dimension"


@pytest.mark.parametrize("question", ["客单价是多少", "毛利率是多少", "老客复购率是多少"])
def test_unregistered_metric_words_refused(question):
    """没定义过的指标词直接拒答，不能让模型自己编公式。"""
    assert check_question(question)["reason_code"] == "missing_metric"


@pytest.mark.parametrize("question", [
    "前三个月的销售额是多少",
    "过去半年销售额是多少",
    "今年以来的销售额是多少",
    "最近两个月的订单数",
    "上半年销售额是多少",
])
def test_time_expressions_the_regex_misses_are_allowed(question):
    """时间正则没穷举到的说法不拒答：宁可放过，也不误杀能答的问题。"""
    assert check_question(question)["allowed"] is True


def test_time_like_residue_is_recorded_for_debug():
    """放行不等于无视：没解析成功的时间说法要留在 time_terms 里，调试看得见。"""
    verdict = check_question("过去半年的销售额是多少")
    assert verdict["allowed"] is True
    assert verdict["time_terms"]


@pytest.mark.parametrize("question", [
    "帮我看看上个月销售额怎么样",
    "统计一下上个月的订单数",
    "查一下客户",
    "订单明细有哪些",
])
def test_plain_spoken_phrasing_is_allowed(question):
    """口语说法不该被当成业务缺口。"""
    assert check_question(question)["allowed"] is True


def test_single_character_residue_is_ignored_not_refused():
    """单字残片（"怎么样"剩下的"样"）不参与拒答，只留痕。"""
    verdict = check_question("帮我看看上个月销售额怎么样")
    assert verdict["allowed"] is True
    assert verdict["ignored_fragments"] == ["样"]
    assert verdict["residues"] == []


@pytest.mark.parametrize("question", [
    "app 渠道的销售额是多少",
    "已支付订单的销售额是多少",
    "线下渠道销售额",
])
def test_registered_enum_values_never_trigger_refusal(question):
    """枚举取值属于下一步"维度取值校验"的范围，本步不拦。"""
    assert check_question(question)["allowed"] is True


def test_douyin_next_to_channel_word_passes_as_a_value_not_a_dimension():
    """E3 的关键：抖音不是已登记维度的取值，但它挨着"渠道"，当取值放过而不是拒答。"""
    verdict = check_question("抖音渠道的销售额是多少")
    assert verdict["allowed"] is True
    assert "抖音" in verdict["dimension_values"]


def test_empty_question_refused_with_metric_list():
    verdict = check_question("")
    assert verdict["allowed"] is False
    assert verdict["reason_code"] == "empty_question"


def test_refusal_message_names_the_missing_term_and_lists_supported_scope():
    """文案要具体：缺哪个词 + 现在能答什么，不能只说"无法回答"。"""
    message = check_question("上个月华东地区的销售额是多少")["message"]
    assert message.startswith("无法回答")
    assert "华东地区" in message
    assert "维度" in message
    for dim in supported_dimensions("销售额"):
        assert dim in message


def test_unknown_qualifier_message_names_metric_and_definition_gap():
    message = check_question("上个月新客销售额是多少")["message"]
    assert "新客" in message
    assert "销售额" in message
    assert "指标" in message or "口径" in message


# --- 登记表结构 ---


def test_supported_dimensions_comes_from_one_registry_only():
    """指标侧不存维度清单：支持列表是 DIMENSIONS 反查出来的，两处维护不可能发生。"""
    assert supported_dimensions("销售额") == [dim["name"] for dim in DIMENSIONS.values()]
    assert "metrics" not in next(iter(DIMENSIONS.values()))


def test_dimension_scoped_to_other_metric_is_refused_for_this_metric():
    """applies_to 限定归属时，指标不支持就拒答并说清支持哪些。"""
    dimensions = {
        "渠道": {"name": "渠道", "aliases": ["渠道"], "column": "orders.channel", "applies_to": ["订单数"]},
    }
    verdict = check_question("上个月按渠道看销售额是多少", dimensions=dimensions)
    assert verdict["allowed"] is False
    assert verdict["reason_code"] == "dimension_not_supported"
    assert verdict["unsupported_dimensions"] == ["渠道"]
    assert "订单数" in verdict["message"] or "支持" in verdict["message"]


@pytest.mark.parametrize("dim_name", sorted(DIMENSIONS))
def test_every_registered_dimension_column_exists_in_schema_notes(dim_name):
    """登记表里的列必须真在表结构说明里——防止登记了一个查无此列的维度。"""
    dim = DIMENSIONS[dim_name]
    assert dim["column"] in COLUMN_NOTES, f"{dim_name}.column={dim['column']} 不在 COLUMN_NOTES"


def test_schema_dimension_columns_are_registered():
    """反方向也要堵：表结构里能当维度的列没登记，加维度就会漏在这里。"""
    dimension_columns = {"orders.channel", "customers.city", "customers.segment", "orders.status", "orders.order_date"}
    registered = {dim["column"] for dim in DIMENSIONS.values()}
    assert dimension_columns <= registered


def test_reason_codes_are_a_closed_set():
    for case in _non_injected_cases():
        verdict = check_question(case["question"])
        assert verdict["reason_code"] in (None, *REASON_CODES)
