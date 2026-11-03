"""用例加载、断言判定与失败聚合：输入 outcome 字典，输出判定结果，不碰被测系统。

阶段制约定：用例不预设"失败是什么性质"（旧版 failure_type / on_fail 已删除）。
判定只出 通过 / 失败 / 不可判定 三种结论；报告按阶段过滤、失败按能力域分组，
原因由人拿着证据事后判断。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from data_agent.pipeline import STATUSES

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_ENVIRONMENT = "environment"
VERDICTS = (VERDICT_PASS, VERDICT_FAIL, VERDICT_ENVIRONMENT)

STAGE_STATUSES = ("done", "pending")

REQUIRED_CASE_KEYS = (
    "id",
    "stage",
    "category",
    "question",
    "expected_status",
    "expected_behavior",
    "assertions",
)
REQUIRED_ASSERTION_KEYS = ("check", "root_cause", "why", "landing")

# 模型拒答且用例接受拒答时，只看 SQL / 结果 / 答案文本的断言不参与判定
SKIP_ON_ACCEPTED_REFUSAL = frozenset(
    {
        "sql_matches",
        "result_empty_or_null",
        "result_row_count",
        "result_value_in",
        "llm_response_matches",
    }
)


class CaseSpecError(ValueError):
    """用例文件不合法：字段缺失、状态取值超出 v0 契约、正则写错等。"""


TOLERANCE_MODES = ("absolute", "relative")


def _validate_value_expect(case_id: str, expect: Any) -> None:
    """result_value_in 必须自带 values 与显式容差，judge 不补默认值。"""
    if not isinstance(expect, dict):
        raise CaseSpecError(f"用例 {case_id}.result_value_in 的 expect 必须是对象")
    values = expect.get("values")
    if not isinstance(values, list) or not values:
        raise CaseSpecError(f"用例 {case_id}.result_value_in 缺非空 values 列表")
    tolerance = expect.get("tolerance")
    if not isinstance(tolerance, dict):
        raise CaseSpecError(
            f"用例 {case_id}.result_value_in 缺 tolerance：容差必须在用例里显式声明，judge 不内置默认值"
        )
    if tolerance.get("mode") not in TOLERANCE_MODES:
        raise CaseSpecError(
            f"用例 {case_id}.result_value_in 的 tolerance.mode 必须是 {TOLERANCE_MODES}"
        )
    if not isinstance(tolerance.get("value"), (int, float)) or isinstance(
        tolerance.get("value"), bool
    ):
        raise CaseSpecError(f"用例 {case_id}.result_value_in 的 tolerance.value 必须是数字")


def _violations(outcome: dict) -> list[dict]:
    """取 guard.violations 原始记录；没有做校验时为空。"""
    return (outcome.get("guard") or {}).get("violations") or []


def _rule_label(rule: str, token: str | None = None) -> str:
    """拦截原因的展示形式 `rule[token]`，与 README 的取值表对齐。"""
    return f"{rule}[{token}]" if token else rule


def violation_labels(guard: dict | None) -> list[str]:
    """把一次校验结果的 violations 压成 `rule[token]` 列表；未校验或放行时为空。"""
    return [
        _rule_label(violation["rule"], violation.get("token"))
        for violation in (guard or {}).get("violations") or []
    ]


def _matches_violation(violation: dict, wanted: dict) -> bool:
    """用例只写 rule 时按 rule 匹配；额外写了 token 就必须连 token 一起对上。"""
    if violation["rule"] != wanted["rule"]:
        return False
    return not wanted.get("token") or violation.get("token") == wanted["token"]


def _normalized_sql(outcome: dict) -> str | None:
    """SQL 归一化：小写 + 空白折叠，便于正则断言。"""
    sql = outcome.get("sql")
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip().lower()


def _rows(outcome: dict) -> list[dict] | None:
    """结果行；没有 result 时返回 None。"""
    result = outcome.get("result")
    return result["rows"] if result else None


def _is_blank(value: Any) -> bool:
    """把 None / 0 / 空串视为"没有数据"，覆盖聚合查询返回 NULL 与 COALESCE 成 0 两种写法。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip() in {"", "0", "0.0", "null", "NULL"}
    return False


def _first_value(rows: list[dict]) -> tuple[str, Any] | None:
    """取结果里第一个标量取值，用于数值断言。"""
    for row in rows:
        for key, value in row.items():
            if isinstance(value, (int, float, str)) or value is None:
                return key, value
    return None


def _check_status_in(outcome: dict, expect: list[str]) -> tuple[bool, str]:
    status = outcome.get("status")
    return status in expect, f"status={status}"


def _check_guard_passed(outcome: dict, expect: bool) -> tuple[bool, str]:
    guard = outcome.get("guard")
    passed = guard["passed"] if guard else None
    return passed == expect, f"guard.passed={passed}"


def _check_guard_rules_include(outcome: dict, expect: list[dict]) -> tuple[bool, str]:
    """要求拦截记录里出现指定的 rule（可指定 token）。"""
    violations = _violations(outcome)
    missing = [
        _rule_label(wanted["rule"], wanted.get("token"))
        for wanted in expect
        if not any(_matches_violation(violation, wanted) for violation in violations)
    ]
    return not missing, f"violations={violation_labels(outcome.get('guard')) or '（无）'}"


def _check_guard_rules_exclude(outcome: dict, expect: list[str]) -> tuple[bool, str]:
    """要求拦截记录里不出现指定的 rule。"""
    rules = {violation["rule"] for violation in _violations(outcome)}
    hit = [rule for rule in expect if rule in rules]
    return not hit, f"violations={violation_labels(outcome.get('guard')) or '（无）'}"


def _check_sql_null(outcome: dict, expect: bool) -> tuple[bool, str]:
    sql = outcome.get("sql")
    return (sql is None) == expect, f"sql={'NULL' if sql is None else sql[:120]}"


def _check_sql_matches(outcome: dict, expect: str) -> tuple[bool, str]:
    sql = _normalized_sql(outcome)
    if sql is None:
        return False, "sql=NULL，无法匹配"
    return bool(re.search(expect, sql)), f"SQL 归一化后={'命中' if re.search(expect, sql) else '未命中'} {expect}"


def _check_sql_not_matches(outcome: dict, expect: str) -> tuple[bool, str]:
    sql = _normalized_sql(outcome)
    if sql is None:
        return True, "sql=NULL，不存在违规内容"
    hit = bool(re.search(expect, sql))
    return not hit, f"SQL 归一化后={'命中' if hit else '未命中'} {expect}"


def _check_result_none(outcome: dict, expect: bool) -> tuple[bool, str]:
    result_none = outcome.get("result") is None
    return result_none == expect, f"result={'NULL' if result_none else '非空'}"


def _check_result_row_count(outcome: dict, expect: int) -> tuple[bool, str]:
    rows = _rows(outcome)
    if rows is None:
        return False, f"无 result（status={outcome.get('status')}）"
    return len(rows) == expect, f"row_count={len(rows)}"


def _check_result_empty_or_null(outcome: dict, expect: bool) -> tuple[bool, str]:
    rows = _rows(outcome)
    if rows is None:
        return False, f"无 result（status={outcome.get('status')}），既不是空结果集也谈不上非空"
    if not rows:
        empty = True
        observed = "0 行"
    else:
        cells = [value for row in rows for value in row.values()]
        empty = all(_is_blank(cell) for cell in cells)
        observed = f"{len(rows)} 行，取值 {cells[:3]}"
    return empty == expect, observed


def _check_result_value_in(outcome: dict, expect: dict) -> tuple[bool, str]:
    rows = _rows(outcome)
    if rows is None:
        return False, f"无 result（status={outcome.get('status')}）"
    picked = _first_value(rows)
    if picked is None:
        return False, "结果里没有可比较的取值"
    column, value = picked
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False, f"{column}={value!r} 不是数值"
    tolerance = expect["tolerance"]
    within = any(
        _within_tolerance(value, allowed, tolerance["mode"], tolerance["value"])
        for allowed in expect["values"]
    )
    return (
        within,
        f"{column}={value}（容差 {tolerance['mode']}={tolerance['value']}，允许 {expect['values']}）",
    )


def _within_tolerance(value: float, allowed: float, mode: str, amount: float) -> bool:
    """按用例声明的容差模式比较数值，judge 不内置默认容差。"""
    if mode == "absolute":
        return abs(value - allowed) <= amount
    if mode == "relative":
        return abs(value - allowed) / max(abs(allowed), 1e-9) <= amount
    raise CaseSpecError(f"未知容差模式: {mode}")


def _check_llm_response_matches(outcome: dict, expect: str) -> tuple[bool, str]:
    text = outcome.get("llm_response") or ""
    return bool(re.search(expect, text, re.IGNORECASE)), f"llm_response={text[:160]!r}"


CHECKS: dict[str, Callable[[dict, Any], tuple[bool, str]]] = {
    "status_in": _check_status_in,
    "guard_passed": _check_guard_passed,
    "guard_rules_include": _check_guard_rules_include,
    "guard_rules_exclude": _check_guard_rules_exclude,
    "sql_null": _check_sql_null,
    "sql_matches": _check_sql_matches,
    "sql_not_matches": _check_sql_not_matches,
    "result_none": _check_result_none,
    "result_row_count": _check_result_row_count,
    "result_empty_or_null": _check_result_empty_or_null,
    "result_value_in": _check_result_value_in,
    "llm_response_matches": _check_llm_response_matches,
}


def load_cases(path: Path) -> tuple[dict, list[dict]]:
    """读用例文件并做结构校验，返回 (meta, cases)。"""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise CaseSpecError(f"{path} 顶层必须是带 cases 数组的对象")

    cases = document["cases"]
    if not cases:
        raise CaseSpecError(f"{path} 里没有用例")

    meta = document.get("meta") or {}
    stages = meta.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise CaseSpecError("meta.stages 必须是非空对象：每个阶段的 status(done/pending) 与承诺范围")
    for stage_name, stage_info in stages.items():
        status = (stage_info or {}).get("status")
        if status not in STAGE_STATUSES:
            raise CaseSpecError(
                f"meta.stages.{stage_name}.status 必须是 {STAGE_STATUSES}，实际是 {status!r}"
            )

    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id", "（缺 id）")
        missing = [key for key in REQUIRED_CASE_KEYS if key not in case]
        if missing:
            raise CaseSpecError(f"用例 {case_id} 缺字段: {missing}")
        if case_id in seen:
            raise CaseSpecError(f"用例 id 重复: {case_id}")
        seen.add(case_id)
        if case["stage"] not in stages:
            raise CaseSpecError(
                f"用例 {case_id} 的 stage={case['stage']!r} 不在 meta.stages {list(stages)} 里"
            )
        if case["expected_status"] not in STATUSES:
            raise CaseSpecError(
                f"用例 {case_id} 的 expected_status={case['expected_status']} 不在 v0 契约 {STATUSES} 内"
            )
        if not case["assertions"]:
            raise CaseSpecError(f"用例 {case_id} 没有断言")
        if case.get("deterministic") and not case.get("injected_sql"):
            raise CaseSpecError(f"用例 {case_id} 标了 deterministic 但没有 injected_sql，无法保证可复现")
        for forbidden in ("failure_type", "on_fail"):
            if forbidden in case:
                raise CaseSpecError(f"用例 {case_id} 还带着 {forbidden}：阶段制已删除失败预设分类")

        for assertion in case["assertions"]:
            miss = [key for key in REQUIRED_ASSERTION_KEYS if key not in assertion]
            if miss:
                raise CaseSpecError(f"用例 {case_id} 有断言缺字段: {miss}")
            if "on_fail" in assertion:
                raise CaseSpecError(f"用例 {case_id} 的断言还带着 on_fail：阶段制已删除失败预设分类")
            check = assertion["check"]
            if check not in CHECKS:
                raise CaseSpecError(f"用例 {case_id} 用了未知断言 {check}")
            if "expect" not in assertion:
                raise CaseSpecError(f"用例 {case_id}.{check} 缺 expect")
            if check in {"sql_matches", "sql_not_matches", "llm_response_matches"}:
                try:
                    re.compile(assertion["expect"])
                except re.error as exc:
                    raise CaseSpecError(f"用例 {case_id}.{check} 正则非法: {exc}") from exc
            if check == "result_value_in":
                _validate_value_expect(case_id, assertion["expect"])

    return meta, cases


def allowed_statuses(case: dict) -> set[str]:
    """用例里 status_in 断言接受的状态集合。"""
    allowed: set[str] = set()
    for assertion in case["assertions"]:
        if assertion["check"] == "status_in":
            allowed.update(assertion["expect"])
    return allowed


def evaluate_case(case: dict, outcome: dict) -> dict:
    """按用例断言逐条判定一次 outcome，返回每条断言的结论。"""
    refusal_accepted = outcome.get("status") == "no_sql" and "no_sql" in allowed_statuses(case)
    results = []
    for index, assertion in enumerate(case["assertions"]):
        check = assertion["check"]
        expect = assertion["expect"]
        item = {
            "index": index,
            "check": check,
            "expect": expect,
            "root_cause": assertion["root_cause"],
            "capability": assertion.get("capability", case.get("capability", "（未标注）")),
            "why": assertion["why"],
            "landing": assertion["landing"],
        }

        if refusal_accepted and check in SKIP_ON_ACCEPTED_REFUSAL:
            item.update(status="skipped", observed="模型拒答且用例接受拒答，跳过")
            results.append(item)
            continue

        try:
            passed, observed = CHECKS[check](outcome, expect)
        except re.error as exc:
            raise CaseSpecError(f"用例 {case['id']} 的 {check} 正则非法: {exc}") from exc
        item.update(status="pass" if passed else "fail", observed=observed)
        results.append(item)

    return {
        "case_id": case["id"],
        "outcome_status": outcome.get("status"),
        "refusal_accepted": refusal_accepted,
        "assertions": results,
    }


def failures_of(evaluation: dict) -> list[dict]:
    """取判定结果里的失败断言。"""
    return [item for item in evaluation["assertions"] if item["status"] == "fail"]


def skipped_checks_of(evaluation: dict) -> list[str]:
    """取因"接受拒答"被跳过的断言名：一次通过若靠它们拿到，就是不可靠通过。"""
    return [
        item["check"] for item in evaluation["assertions"] if item["status"] == "skipped"
    ]


def decide_verdict(reports: list[dict]) -> str:
    """由多次已判定采样决定用例结论：任一失败即失败，否则通过。"""
    if any(report["failures"] for report in reports):
        return VERDICT_FAIL
    return VERDICT_PASS


def done_stage_ids(stages: dict) -> list[str]:
    """status=done 的阶段名清单，--all 的退出码只看这些阶段。"""
    return [
        name
        for name, info in (stages or {}).items()
        if (info or {}).get("status") == "done"
    ]


def aggregate(case_records: list[dict], *, anchor_today: str, repeats: int) -> dict:
    """把逐用例记录聚合成报告结构：总览、按阶段、失败按能力域分组、不可靠通过清单。"""
    verdicts = {record["verdict"] for record in case_records}
    passed = [record for record in case_records if record["verdict"] == VERDICT_PASS]
    failed = [record for record in case_records if record["verdict"] == VERDICT_FAIL]
    environment = [
        record for record in case_records if record["verdict"] == VERDICT_ENVIRONMENT
    ]

    # 拒答导致的不可靠通过：通过的采样里存在"用例接受拒答且模型真拒答了"的
    # v0 没有保证拒答发生的组件，这种通过是运气不是机制（R2 碰巧拒答即属此类）
    unreliable = [
        record
        for record in passed
        if any(
            not run.get("failures") and (
                run.get("refusal_accepted") or run.get("skipped_checks")
            )
            for run in record["runs"]
            if "failures" in run
        )
    ]

    by_stage: dict[str, dict] = {}
    for record in case_records:
        bucket = by_stage.setdefault(
            record["stage"],
            {"stage": record["stage"], "total": 0, "pass": 0, "fail": 0, "environment": 0},
        )
        bucket["total"] += 1
        bucket[record["verdict"]] += 1

    # 失败断言按（类别, root_cause）分组：跨用例同根因只算一组，证据逐条保留
    groups: dict[tuple[str, str], dict] = {}
    for record in failed:
        for failure in record["failures"]:
            key = (record["category"], failure["root_cause"])
            group = groups.setdefault(
                key,
                {
                    "root_cause": failure["root_cause"],
                    "capability": failure["capability"],
                    "cases": [],
                    "whys": [],
                    "landings": [],
                    "evidence": [],
                },
            )
            if record["id"] not in group["cases"]:
                group["cases"].append(record["id"])
            if failure["why"] not in group["whys"]:
                group["whys"].append(failure["why"])
            if failure["landing"] not in group["landings"]:
                group["landings"].append(failure["landing"])
            group["evidence"].append(
                {
                    "case": record["id"],
                    "check": failure["check"],
                    "expect": failure["expect"],
                    "observed": failure["observed"],
                    "outcome_status": failure["outcome_status"],
                }
            )

    failed_by_category: dict[str, dict] = {}
    for (category, _root_cause), group in groups.items():
        bucket = failed_by_category.setdefault(
            category, {"category": category, "cases": [], "root_causes": []}
        )
        bucket["root_causes"].append(group)
        for case_id in group["cases"]:
            if case_id not in bucket["cases"]:
                bucket["cases"].append(case_id)
    for bucket in failed_by_category.values():
        bucket["root_causes"].sort(key=lambda item: item["root_cause"])

    return {
        "overview": {
            "anchor_today": anchor_today,
            "repeats": repeats,
            "cases": len(case_records),
            "pass": len(passed),
            "fail": len(failed),
            "environment": len(environment),
            "unreliable_pass": len(unreliable),
            "verdict_values": sorted(verdicts),
        },
        "by_stage": sorted(by_stage.values(), key=lambda item: item["stage"]),
        "failed_by_category": sorted(
            failed_by_category.values(), key=lambda item: item["category"]
        ),
        "unreliable_passes": [
            {
                "id": record["id"],
                "stage": record["stage"],
                "category": record["category"],
                "refused": any(
                    run.get("refusal_accepted")
                    for run in record["runs"]
                    if "failures" in run and not run["failures"]
                ),
                "skipped_checks": sorted(
                    {
                        check
                        for run in record["runs"]
                        if "failures" in run and not run["failures"]
                        for check in run.get("skipped_checks") or []
                    }
                ),
            }
            for record in unreliable
        ],
        "cases": case_records,
    }
