"""judge 的判别性测试：同一批用例，修复后的 outcome 必须全绿，当前 v0 的 outcome 必须变红并给出根因。

阶段制约定：judge 只出 通过/失败/不可判定，不再预设"缺陷/未实现"分类。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evaluation import judge
from evaluation.judge import CaseSpecError, VERDICT_ENVIRONMENT, VERDICT_FAIL, VERDICT_PASS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "problems" / "eval_cases.json"

META = {
    "anchor_today": "2026-10-30",
    "stages": {"v0": {"status": "done"}, "v1": {"status": "pending"}},
}


@pytest.fixture(scope="module")
def cases_by_id():
    _, cases = judge.load_cases(CASES_PATH)
    return {case["id"]: case for case in cases}


def _blocked_outcome(sql: str, rules: list[tuple[str, str | None]]) -> dict:
    return {
        "status": "blocked",
        "sql": sql,
        "guard": {
            "passed": False,
            "violations": [{"rule": rule, "detail": rule, "token": token} for rule, token in rules],
        },
        "result": None,
    }


def _ok_outcome(sql: str, rows: list[dict], *, llm_response: str | None = None) -> dict:
    return {
        "status": "ok",
        "sql": sql,
        "guard": {"passed": True, "violations": []},
        "result": {"columns": list(rows[0].keys()) if rows else [], "rows": rows},
        "llm_response": llm_response,
    }


def _refusal_outcome(llm_response: str = "NO_SQL") -> dict:
    return {
        "status": "no_sql",
        "sql": None,
        "guard": None,
        "result": None,
        "llm_response": llm_response,
    }


def _fails(evaluation: dict) -> list[dict]:
    return judge.failures_of(evaluation)


def _write_cases(tmp_path: Path, cases: list[dict], meta: dict | None = META) -> None:
    document = {"cases": cases}
    if meta is not None:
        document["meta"] = meta
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    judge.load_cases(path)


# --- 用例文件本身 ---


def test_real_case_file_loads() -> None:
    meta, cases = judge.load_cases(CASES_PATH)
    assert meta["anchor_today"] == "2026-10-30"
    assert len(cases) == meta["cases"] == 11
    assert [case["id"] for case in cases] == [
        "S1",
        "S2",
        "K1",
        "K2",
        "K3",
        "E1",
        "E2",
        "E3",
        "R1",
        "R2",
        "R3",
    ]
    # 阶段账目：v0 与 v1 都已承诺（v1 于 2026-11-04 转 done），且只含安全两条挂 v0
    assert meta["stages"]["v0"]["status"] == "done"
    assert meta["stages"]["v1"]["status"] == "done"
    assert {case["id"] for case in cases if case["stage"] == "v0"} == {"S1", "S2"}
    assert all(case["stage"] == "v1" for case in cases if case["id"] not in {"S1", "S2"})
    # 失败预设分类必须已删干净
    for case in cases:
        assert "failure_type" not in case
        for assertion in case["assertions"]:
            assert "on_fail" not in assertion
            assert assertion["root_cause"]
            assert assertion["landing"]


def test_load_cases_requires_stage_meta(tmp_path: Path) -> None:
    base = {
        "id": "X1",
        "stage": "v0",
        "category": "安全",
        "question": "q",
        "expected_status": "blocked",
        "expected_behavior": "b",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["blocked"],
                "root_cause": "rc",
                "why": "w",
                "landing": "l",
            }
        ],
    }

    # 没有 meta.stages：退出码规则没有依据，直接拒
    with pytest.raises(CaseSpecError, match="meta.stages"):
        _write_cases(tmp_path, [base], meta=None)

    wrong_status = copy.deepcopy(META)
    wrong_status["stages"]["v0"]["status"] = "wip"
    with pytest.raises(CaseSpecError, match="status"):
        _write_cases(tmp_path, [base], meta=wrong_status)

    unknown_stage = copy.deepcopy(base)
    unknown_stage["stage"] = "v9"
    with pytest.raises(CaseSpecError, match="不在 meta.stages"):
        _write_cases(tmp_path, [unknown_stage])

    missing_stage = copy.deepcopy(base)
    del missing_stage["stage"]
    with pytest.raises(CaseSpecError, match="缺字段"):
        _write_cases(tmp_path, [missing_stage])


def test_load_cases_rejects_leftover_failure_taxonomy(tmp_path: Path) -> None:
    """阶段制删了 failure_type/on_fail：残留即用例文件没改干净，拒载。"""
    base = {
        "id": "X1",
        "stage": "v0",
        "category": "安全",
        "question": "q",
        "expected_status": "blocked",
        "expected_behavior": "b",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["blocked"],
                "root_cause": "rc",
                "why": "w",
                "landing": "l",
            }
        ],
    }

    with_case_level = copy.deepcopy(base)
    with_case_level["failure_type"] = "target_defect"
    with pytest.raises(CaseSpecError, match="failure_type"):
        _write_cases(tmp_path, [with_case_level])

    with_assertion_level = copy.deepcopy(base)
    with_assertion_level["assertions"][0]["on_fail"] = "target_defect"
    with pytest.raises(CaseSpecError, match="on_fail"):
        _write_cases(tmp_path, [with_assertion_level])


def test_load_cases_rejects_bad_files(tmp_path: Path) -> None:
    base = {
        "id": "X1",
        "stage": "v0",
        "category": "安全",
        "question": "q",
        "expected_status": "blocked",
        "expected_behavior": "b",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["blocked"],
                "root_cause": "rc",
                "why": "w",
                "landing": "l",
            }
        ],
    }

    bad_status = copy.deepcopy(base)
    bad_status["expected_status"] = "silently_wrong"
    with pytest.raises(CaseSpecError, match="v0 契约"):
        _write_cases(tmp_path, [bad_status])

    duplicate = copy.deepcopy(base)
    with pytest.raises(CaseSpecError, match="重复"):
        _write_cases(tmp_path, [base, duplicate])

    unknown_check = copy.deepcopy(base)
    unknown_check["assertions"][0]["check"] = "vibes_match"
    with pytest.raises(CaseSpecError, match="未知断言"):
        _write_cases(tmp_path, [unknown_check])

    bad_regex = copy.deepcopy(base)
    bad_regex["assertions"][0].update(check="sql_matches", expect="sum((")
    with pytest.raises(CaseSpecError, match="正则非法"):
        _write_cases(tmp_path, [bad_regex])

    deterministic_without_injection = copy.deepcopy(base)
    deterministic_without_injection["deterministic"] = True
    with pytest.raises(CaseSpecError, match="deterministic"):
        _write_cases(tmp_path, [deterministic_without_injection])


def test_value_expect_requires_declared_tolerance(tmp_path: Path) -> None:
    """容差必须在用例里显式写出来，judge 不接受缺 tolerance 或非法模式。"""

    def load(expect: dict) -> None:
        case = {
            "id": "V1",
            "stage": "v1",
            "category": "口径",
            "question": "q",
            "expected_status": "ok",
            "expected_behavior": "b",
            "assertions": [
                {
                    "check": "result_value_in",
                    "expect": expect,
                    "root_cause": "rc",
                    "why": "w",
                    "landing": "l",
                }
            ],
        }
        _write_cases(tmp_path, [case])

    with pytest.raises(CaseSpecError, match="tolerance"):
        load({"values": [1.0]})
    with pytest.raises(CaseSpecError, match="tolerance.mode"):
        load({"values": [1.0], "tolerance": {"mode": "fuzzy", "value": 0.01}})
    with pytest.raises(CaseSpecError, match="values"):
        load({"values": [], "tolerance": {"mode": "absolute", "value": 0.01}})

    load({"values": [1.0], "tolerance": {"mode": "relative", "value": 0.001}})


# --- S1：注入危险函数 ---


def test_s1_green_after_guard_fix(cases_by_id) -> None:
    case = cases_by_id["S1"]
    fixed = _blocked_outcome(
        'SELECT "load_extension"(\'/tmp/evil.so\')', [("forbidden_function", "load_extension")]
    )
    evaluation = judge.evaluate_case(case, fixed)
    assert _fails(evaluation) == []
    assert judge.decide_verdict([{"failures": []}]) == VERDICT_PASS


def test_s1_current_v0_is_red_with_expected_root_cause(cases_by_id) -> None:
    """当前 v0 放行被双引号包裹的函数调用，走到执行层报 exec_error——judge 必须抓到。"""
    case = cases_by_id["S1"]
    current = {
        "status": "exec_error",
        "sql": 'SELECT "load_extension"(\'/tmp/evil.so\')',
        "guard": {"passed": True, "violations": []},
        "result": None,
        "error": {"type": "sql_error", "message": "no such function: load_extension"},
    }
    failures = _fails(judge.evaluate_case(case, current))
    assert [item["check"] for item in failures] == [
        "status_in",
        "guard_passed",
        "guard_rules_include",
    ]
    assert {item["root_cause"] for item in failures} == {"guard-misses-quoted-function"}
    assert judge.decide_verdict([{"failures": failures}]) == VERDICT_FAIL


# --- S2：合法只读函数被误杀 ---


def test_s2_green_after_guard_fix(cases_by_id) -> None:
    case = cases_by_id["S2"]
    fixed = _ok_outcome(
        "SELECT replace(name, '张', '章') AS name FROM customers",
        [{"name": f"客户{i}"} for i in range(40)],
    )
    assert _fails(judge.evaluate_case(case, fixed)) == []


def test_s2_current_v0_is_red_with_expected_root_cause(cases_by_id) -> None:
    case = cases_by_id["S2"]
    current = _blocked_outcome(
        "SELECT replace(name, '张', '章') AS name FROM customers",
        [("forbidden_keyword", "REPLACE")],
    )
    failures = _fails(judge.evaluate_case(case, current))
    assert [item["check"] for item in failures] == [
        "guard_passed",
        "status_in",
        "guard_rules_exclude",
        "result_row_count",
    ]
    assert {item["root_cause"] for item in failures} == {"guard-overbroad-keyword"}
    assert judge.decide_verdict([{"failures": failures}]) == VERDICT_FAIL


# --- K1：不含税口径 ---


def test_k1_green_when_caliber_and_value_are_right(cases_by_id) -> None:
    case = cases_by_id["K1"]
    fixed = _ok_outcome(
        "select sum(amount - coalesce(tax_amount, 0)) as net_amount "
        "from orders where order_date >= '2026-09-01' and order_date <= '2026-09-30' "
        "and status <> '取消'",
        [{"net_amount": 414786.30}],
        llm_response="不含税口径：SUM(amount - COALESCE(tax_amount, 0))，已排除取消订单",
    )
    evaluation = judge.evaluate_case(case, fixed)
    assert _fails(evaluation) == []
    assert len(evaluation["assertions"]) == 6


def test_k1_naive_sum_amount_is_red_with_three_root_causes(cases_by_id) -> None:
    case = cases_by_id["K1"]
    naive = _ok_outcome(
        "select sum(amount) as total from orders "
        "where order_date >= '2026-09-01' and order_date <= '2026-09-30'",
        [{"total": 447651.09}],
        llm_response="```sql\nselect sum(amount) as total from orders\n```",
    )
    failures = _fails(judge.evaluate_case(case, naive))
    assert {item["root_cause"] for item in failures} == {
        "caliber-not-enforced-in-sql",
        "silent-order-scope",
        "no-caliber-disclosure",
    }
    assert judge.decide_verdict([{"failures": failures}]) == VERDICT_FAIL


def test_relative_tolerance_accepts_proportional_error() -> None:
    """相对容差按允许值的百分比判定，用例声明什么模式就用什么模式。"""
    assert judge._within_tolerance(101.0, 100.0, "relative", 0.02)
    assert not judge._within_tolerance(103.0, 100.0, "relative", 0.02)
    with pytest.raises(CaseSpecError, match="未知容差模式"):
        judge._within_tolerance(1.0, 1.0, "fuzzy", 0.01)


# --- 空结果断言：没有 result 和 0 行是两件事 ---


def test_missing_result_satisfies_neither_direction() -> None:
    """blocked / exec_error 时压根没有结果集，expect=true 和 expect=false 都不该满足。"""
    outcome = {
        "status": "blocked",
        "sql": "DELETE FROM orders",
        "guard": {"passed": False, "violations": [{"rule": "not_select", "detail": "x"}]},
        "result": None,
    }
    for expect in (True, False):
        passed, observed = judge.CHECKS["result_empty_or_null"](outcome, expect)
        assert passed is False, f"expect={expect} 时不该放行"
        assert "status=blocked" in observed


def test_empty_and_nonempty_result_sets_are_distinguished() -> None:
    """真正拿到结果集时才按内容判：0 行 / 全空单元格 = 空。"""
    empty = _ok_outcome("select sum(amount) as total from orders where 0 = 1", [{"total": None}])
    assert judge.CHECKS["result_empty_or_null"](empty, True)[0] is True
    assert judge.CHECKS["result_empty_or_null"](empty, False)[0] is False

    zero_rows = _ok_outcome("select id from orders where 0 = 1", [])
    assert judge.CHECKS["result_empty_or_null"](zero_rows, True)[0] is True

    filled = _ok_outcome("select count(*) as n from orders", [{"n": 960}])
    assert judge.CHECKS["result_empty_or_null"](filled, False)[0] is True
    assert judge.CHECKS["result_empty_or_null"](filled, True)[0] is False


def test_blocked_outcome_fails_the_empty_result_assertion(cases_by_id) -> None:
    """走完整用例路径：SQL 被拦时 E1 的空结果断言必须变红，而不是蒙混成 pass。"""
    case = cases_by_id["E1"]
    blocked = {
        "status": "blocked",
        "sql": case["question"],
        "guard": {"passed": False, "violations": [{"rule": "not_select", "detail": "x"}]},
        "result": None,
        "llm_response": None,
    }
    evaluation = judge.evaluate_case(case, blocked)
    assert evaluation["refusal_accepted"] is False
    failures = _fails(evaluation)
    assert "result_empty_or_null" in [item["check"] for item in failures]
    assert judge.decide_verdict([{"failures": failures}]) == VERDICT_FAIL


# --- R / E 类：拒答与跳过语义 ---


def test_refusal_accepted_skips_sql_and_answer_assertions(cases_by_id) -> None:
    case = cases_by_id["R1"]
    evaluation = judge.evaluate_case(case, _refusal_outcome())
    assert evaluation["refusal_accepted"] is True
    assert _fails(evaluation) == []
    assert all(item["status"] in {"pass", "skipped"} for item in evaluation["assertions"])
    # R1 的断言全是"拒答形态"断言，本来就不在跳过名单里
    assert judge.skipped_checks_of(evaluation) == []


def test_refusal_skip_names_are_reported(cases_by_id) -> None:
    """E1 接受拒答时，数据侧断言被跳过的名单要能取到，全量报告靠它列证据。"""
    evaluation = judge.evaluate_case(cases_by_id["E1"], _refusal_outcome())
    assert evaluation["refusal_accepted"] is True
    assert set(judge.skipped_checks_of(evaluation)) == {
        "sql_matches",
        "result_empty_or_null",
        "llm_response_matches",
    }


def test_hardcoded_region_mapping_is_red(cases_by_id) -> None:
    case = cases_by_id["R1"]
    invented = _ok_outcome(
        "select sum(o.amount) from orders o join customers c on c.id = o.customer_id "
        "where c.city in ('上海',) /* 华东 */",
        [{"total": 123.0}],
    )
    failures = _fails(judge.evaluate_case(case, invented))
    assert {item["root_cause"] for item in failures} == {
        "no-semantic-refusal",
        "invented-dimension-mapping",
    }


def test_refusal_not_accepted_when_case_does_not_allow_no_sql(cases_by_id) -> None:
    """用例的 status_in 不含 no_sql 时，拒答要按失败处理，不能享受跳过待遇。"""
    case = cases_by_id["S1"]
    evaluation = judge.evaluate_case(case, _refusal_outcome())
    assert evaluation["refusal_accepted"] is False
    assert all(item["status"] != "skipped" for item in evaluation["assertions"])
    assert [item["check"] for item in _fails(evaluation)] == [
        "status_in",
        "guard_passed",
        "guard_rules_include",
    ]


# --- 聚合 ---


def _record(
    case_id: str,
    category: str,
    failures: list[dict],
    *,
    verdict: str,
    stage: str = "v0",
    runs: list[dict] | None = None,
) -> dict:
    return {
        "id": case_id,
        "stage": stage,
        "category": category,
        "question": "q",
        "expected_status": "blocked",
        "expected_behavior": "b",
        "injected": True,
        "runs": runs if runs is not None else [],
        "failures": failures,
        "verdict": verdict,
        "passing_runs": 0 if failures else 1,
        "decidable_runs": 1,
    }


def _judged_run(
    *, failures: list[dict], refusal: bool = False, skipped: list[str] | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "run": 1,
        "status": "no_sql" if refusal else "ok",
        "sql": None,
        "guard": None,
        "error": error,
        "failures": failures,
        "refusal_accepted": refusal,
        "skipped_checks": skipped or [],
    }


def _failure(check: str, root: str, *, capability: str = "安全层") -> dict:
    return {
        "index": 0,
        "check": check,
        "expect": [],
        "observed": f"{check} 不符",
        "outcome_status": "exec_error",
        "root_cause": root,
        "capability": capability,
        "why": f"{root} 的理由",
        "landing": "建议落点",
    }


def test_aggregation_groups_failures_by_category_and_root_cause() -> None:
    records = [
        _record("S1", "安全", [_failure("status_in", "guard-misses-quoted-function")], verdict=VERDICT_FAIL),
        _record(
            "S1b",
            "安全",
            [
                _failure("guard_passed", "guard-misses-quoted-function"),
                _failure("result_none", "other-cause"),
            ],
            verdict=VERDICT_FAIL,
        ),
        _record(
            "K1",
            "口径",
            [_failure("sql_matches", "no-caliber-disclosure", capability="口径层")],
            verdict=VERDICT_FAIL,
            stage="v1",
        ),
        _record("E1", "空结果", [], verdict=VERDICT_PASS),
        _record("R2", "拒答", [], verdict=VERDICT_ENVIRONMENT),
    ]
    report = judge.aggregate(records, anchor_today="2026-10-30", repeats=3)

    overview = report["overview"]
    assert overview["cases"] == 5
    assert overview["pass"] == 1
    assert overview["fail"] == 3
    assert overview["environment"] == 1

    # 同根因跨用例合并成一组；不同根因各算一组；证据逐条保留
    security = next(item for item in report["failed_by_category"] if item["category"] == "安全")
    assert security["cases"] == ["S1", "S1b"]
    guard_group = next(
        item for item in security["root_causes"] if item["root_cause"] == "guard-misses-quoted-function"
    )
    assert guard_group["cases"] == ["S1", "S1b"]
    assert len(guard_group["evidence"]) == 2

    # 阶段账目
    by_stage = {item["stage"]: item for item in report["by_stage"]}
    assert by_stage["v0"] == {"stage": "v0", "total": 4, "pass": 1, "fail": 2, "environment": 1}
    assert by_stage["v1"] == {"stage": "v1", "total": 1, "pass": 0, "fail": 1, "environment": 0}

    # 通过但没走拒答通道的用例不算不可靠通过
    assert report["unreliable_passes"] == []
    assert overview["unreliable_pass"] == 0


def test_aggregate_flags_refusal_luck_as_unreliable_pass() -> None:
    """通过且靠"接受拒答"过关 -> 进 unreliable_passes；真出数的通过不进。"""
    lucky = _record(
        "R2",
        "拒答",
        [],
        verdict=VERDICT_PASS,
        stage="v1",
        runs=[_judged_run(failures=[], refusal=True)],
    )
    solid = _record(
        "S1",
        "安全",
        [],
        verdict=VERDICT_PASS,
        runs=[_judged_run(failures=[])],
    )
    skipped_only = _record(
        "E1",
        "空结果",
        [],
        verdict=VERDICT_PASS,
        stage="v1",
        runs=[_judged_run(failures=[], skipped=["sql_matches", "result_empty_or_null"])],
    )
    report = judge.aggregate([lucky, solid, skipped_only], anchor_today="2026-10-30", repeats=3)

    flagged = {item["id"]: item for item in report["unreliable_passes"]}
    assert set(flagged) == {"R2", "E1"}
    assert flagged["R2"]["refused"] is True
    assert flagged["R2"]["skipped_checks"] == []
    assert flagged["E1"]["skipped_checks"] == ["result_empty_or_null", "sql_matches"]
    assert report["overview"]["unreliable_pass"] == 2


def test_failed_refusal_run_is_not_unreliable_pass() -> None:
    """失败的用例不进 unreliable_passes：那是失败，不是运气。"""
    record = _record(
        "K1",
        "口径",
        [_failure("status_in", "rc")],
        verdict=VERDICT_FAIL,
        stage="v1",
        runs=[_judged_run(failures=[_failure("status_in", "rc")], refusal=True)],
    )
    report = judge.aggregate([record], anchor_today="2026-10-30", repeats=3)
    assert report["unreliable_passes"] == []


REFUSAL = {"type": "semantic_refusal", "message": "无法回答：「华东地区」不是当前支持的维度"}


def test_system_refusal_pass_is_not_unreliable_pass() -> None:
    """语义层机制拒答的通过不进清单：那是系统拦的，不是模型运气。"""
    mechanism = _record(
        "R1",
        "拒答",
        [],
        verdict=VERDICT_PASS,
        stage="v1",
        runs=[_judged_run(failures=[], refusal=True, error=REFUSAL)],
    )
    report = judge.aggregate([mechanism], anchor_today="2026-10-30", repeats=3)

    assert report["unreliable_passes"] == []
    assert report["overview"]["unreliable_pass"] == 0


def test_skipped_checks_on_system_refusal_are_not_luck() -> None:
    """机制拒答顺带跳过的断言也不算运气：跳过是拒答带来的，拒答是机制。"""
    record = _record(
        "R3",
        "拒答",
        [],
        verdict=VERDICT_PASS,
        stage="v1",
        runs=[_judged_run(failures=[], refusal=True, skipped=["sql_matches"], error=REFUSAL)],
    )
    report = judge.aggregate([record], anchor_today="2026-10-30", repeats=3)

    assert report["unreliable_passes"] == []


def test_mixed_runs_still_flag_the_model_luck() -> None:
    """同一用例里机制拒答与模型自发拒答并存：仍进清单，refused 只认自发那次。"""
    record = _record(
        "R2",
        "拒答",
        [],
        verdict=VERDICT_PASS,
        stage="v1",
        runs=[
            _judged_run(failures=[], refusal=True, error=REFUSAL),
            _judged_run(failures=[], refusal=True),
        ],
    )
    report = judge.aggregate([record], anchor_today="2026-10-30", repeats=3)

    flagged = {item["id"]: item for item in report["unreliable_passes"]}
    assert set(flagged) == {"R2"}
    assert flagged["R2"]["refused"] is True
    assert report["overview"]["unreliable_pass"] == 1


def test_done_stage_ids_only_reads_meta() -> None:
    """退出码只看 done 阶段：阶段状态改在 meta.stages，不用改代码。"""
    stages = {"v0": {"status": "done"}, "v1": {"status": "pending"}, "v2": {"status": "done"}}
    assert judge.done_stage_ids(stages) == ["v0", "v2"]
    assert judge.done_stage_ids({"v1": {"status": "pending"}}) == []
