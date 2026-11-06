"""runner 的报告行为：每个 run 都留 guard 原样，通过的用例也要能回看凭哪条规则过的。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from evaluation import judge, run_eval

ANCHOR = date(2026, 10, 30)


@pytest.fixture()
def demo_db(tmp_path: Path) -> Path:
    """一张够跑通注入路径的小库。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount REAL NOT NULL);
            INSERT INTO customers VALUES (1, '张三'), (2, '李四');
            INSERT INTO orders VALUES (1, 1, 10.0), (2, 1, 20.0), (3, 2, 30.0);
            """
        )
        conn.commit()
    return path


def _case(case_id: str, injected: str, assertions: list[dict]) -> dict:
    """构造一条最小可用的注入用例，断言字段补齐 load_cases 的必填项。"""
    return {
        "id": case_id,
        "stage": "v0",
        "category": "安全",
        "question": f"用例 {case_id}",
        "deterministic": True,
        "injected_sql": injected,
        "expected_status": "ok",
        "expected_behavior": "看 guard 的判定",
        "capability": "安全层",
        "assertions": [
            {
                "root_cause": f"{case_id.lower()}-guard",
                "capability": "安全层",
                "why": f"{case_id} 的 guard 判定不符合用例期望",
                "landing": "v0 可修",
                **assertion,
            }
            for assertion in assertions
        ],
    }


def _run(cases: list[dict], db_path: Path, *, generator=None):
    return run_eval.run_cases(
        cases,
        generator=generator,
        db_path=db_path,
        max_rows=200,
        timeout_s=5.0,
        today=ANCHOR,
        repeats=1,
        months=["2026-09"],
    )


def _render(records: list[dict]) -> str:
    report = judge.aggregate(records, anchor_today=ANCHOR.isoformat(), repeats=1)
    report["run_context"] = {
        "mode": "stage",
        "stage": "v0",
        "stages": {"v0": {"status": "done"}, "v1": {"status": "pending"}},
    }
    return run_eval.render_text(
        report,
        cases_path=Path("cases.json"),
        db_path=Path("demo.db"),
        model="",
        used_llm=False,
        exit_code=1 if report["overview"]["fail"] else 0,
    )


def test_passing_run_still_records_guard(demo_db) -> None:
    """用例通过时 run 里也要有完整 guard（passed + violations），不能只在失败时留痕。"""
    case = _case(
        "P1",
        "SELECT id, amount FROM orders",
        [
            {"check": "status_in", "expect": ["ok"]},
            {"check": "guard_passed", "expect": True},
        ],
    )
    record = _run([case], demo_db)[0]

    assert record["verdict"] == judge.VERDICT_PASS
    assert record["runs"][0]["guard"] == {"passed": True, "violations": []}
    assert record["runs"][0]["failures"] == []


def test_blocked_run_records_all_violation_rules(demo_db) -> None:
    """拦截时 violations 原样进报告，rule 和 token 都要看得见。"""
    case = _case(
        "P2",
        "DELETE FROM orders",
        [
            {"check": "status_in", "expect": ["blocked"]},
            {"check": "guard_rules_include", "expect": [{"rule": "not_select"}]},
        ],
    )
    record = _run([case], demo_db)[0]
    guard = record["runs"][0]["guard"]

    assert guard["passed"] is False
    # not_select 的 token 是规范化后的小写首关键字，forbidden_keyword 保留原样命中词
    assert judge.violation_labels(guard) == ["not_select[delete]", "forbidden_keyword[DELETE]"]
    assert record["verdict"] == judge.VERDICT_PASS


def test_blocked_for_the_wrong_rule_is_visible_in_console(demo_db) -> None:
    """状态碰巧对、拦因不对：status_in 过，guard_rules_include 红，控制台要直接写出真实拦因。"""
    case = _case(
        "P3",
        "DELETE FROM orders",
        [
            {"check": "status_in", "expect": ["blocked"]},
            {"check": "guard_rules_include", "expect": [{"rule": "forbidden_function"}]},
        ],
    )
    record = _run([case], demo_db)[0]

    assert [failure["check"] for failure in record["failures"]] == ["guard_rules_include"]
    text = _render([record])
    assert "校验: 拦截 passed=False ← " in text
    assert "not_select[delete]" in text
    # 明细里同时保留失败断言的期望与实际
    assert "guard_rules_include" in text


def test_console_prints_pass_reason_for_allowed_refusal(demo_db) -> None:
    """通过类用例（no_sql 拒答）也要一行说明：这轮没做静态校验。"""

    def refuse(question: str, schema_text: str, today: date) -> object:
        from data_agent.services.llm_client import GeneratedSql

        return GeneratedSql(sql=None, raw_response="NO_SQL")

    case = {
        "id": "P4",
        "stage": "v1",
        "category": "拒答",
        "question": "库里没有的维度",
        "expected_status": "no_sql",
        "expected_behavior": "拒答",
        "capability": "语义拒答层",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["no_sql"],
                "root_cause": "no-semantic-refusal",
                "capability": "语义拒答层",
                "why": "支撑不了就该拒答",
                "landing": "等 v1",
            }
        ],
    }
    record = _run([case], demo_db, generator=refuse)[0]

    assert record["verdict"] == judge.VERDICT_PASS
    assert record["runs"][0]["guard"] is None
    assert "校验: 未做静态校验" in _render([record])


def test_llm_error_run_records_guard_as_null_and_stays_environment(demo_db) -> None:
    """LLM 故障那次也要有 guard 键（值必为 null），且结论仍是不可判定而不是缺陷。"""

    def broken(question: str, schema_text: str, today: date):
        raise RuntimeError("网络断了")

    case = {
        "id": "P5",
        "stage": "v1",
        "category": "口径",
        "question": "上个月销售额",
        "expected_status": "ok",
        "expected_behavior": "出数",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["ok"],
                "root_cause": "sql-generation-unchecked",
                "why": "该出数",
                "landing": "等 v1",
            }
        ],
    }
    record = _run([case], demo_db, generator=broken)[0]

    assert record["verdict"] == judge.VERDICT_ENVIRONMENT
    assert record["stage"] == "v1"
    assert record["runs"][0]["guard"] is None
    assert record["runs"][0]["status"] == "llm_error"


def test_repeated_runs_collapse_when_guard_identical(demo_db) -> None:
    """同样结论的多次采样只占一行，不同才按 run 展开，报告不被重复刷屏。"""
    case = _case(
        "P6",
        "SELECT 1 AS one",
        [{"check": "guard_passed", "expect": True}],
    )
    case["deterministic"] = False
    case["repeats"] = 3
    record = _run([case], demo_db)[0]

    assert record["decidable_runs"] == 3
    assert all(run["guard"] == {"passed": True, "violations": []} for run in record["runs"])
    assert "校验: 放行 passed=True，violations=（无）（3 次采样一致）" in _render([record])


def test_detail_rows_carry_pass_and_fail_symbols(demo_db) -> None:
    """明细行结论带符号：通过 ✅、失败 ❌，扫一眼就能分清真过与红。"""
    passing = _case("P9", "SELECT 1 AS one", [{"check": "status_in", "expect": ["ok"]}])
    failing = _case("P10", "SELECT 1 AS one", [{"check": "status_in", "expect": ["blocked"]}])
    records = _run([passing, failing], demo_db)

    lines = _render(records).splitlines()
    pass_line = next(line for line in lines if line.startswith("P9 "))
    fail_line = next(line for line in lines if line.startswith("P10 "))
    assert "✅ 通过" in pass_line
    assert "❌ 失败" in fail_line
    assert "❌" not in pass_line


# --- 退出码闸门：只看 done 阶段 ---


def _stage_record(case_id: str, stage: str, verdict: str) -> dict:
    return {
        "id": case_id,
        "stage": stage,
        "category": "安全",
        "question": "q",
        "expected_status": "ok",
        "expected_behavior": "b",
        "injected": True,
        "runs": [],
        "failures": [],
        "verdict": verdict,
        "passing_runs": 1 if verdict == judge.VERDICT_PASS else 0,
        "decidable_runs": 1,
    }


def _exit_report(mode: str, records: list[dict]) -> dict:
    report = judge.aggregate(records, anchor_today=ANCHOR.isoformat(), repeats=1)
    report["run_context"] = {
        "mode": mode,
        "stage": "v0" if mode == "stage" else None,
        "stages": {"v0": {"status": "done"}, "v1": {"status": "pending"}},
    }
    return report


def test_all_mode_exit_code_ignores_pending_stage_failures() -> None:
    report = _exit_report("all", [
        _stage_record("S1", "v0", judge.VERDICT_PASS),
        _stage_record("K1", "v1", judge.VERDICT_FAIL),
    ])
    assert run_eval.compute_exit_code(report) == 0


def test_all_mode_exit_code_gates_on_done_stage_failure() -> None:
    """done 阶段有失败必须 1；把阶段改回 pending 就该 0——规则跟着 meta.stages 走，不硬编码。"""
    failing_v0 = _exit_report("all", [_stage_record("S1", "v0", judge.VERDICT_FAIL)])
    assert run_eval.compute_exit_code(failing_v0) == 1

    failing_v0["run_context"]["stages"]["v0"]["status"] = "pending"
    assert run_eval.compute_exit_code(failing_v0) == 0


def test_stage_mode_exit_code_counts_any_failure() -> None:
    failed = _exit_report("stage", [
        _stage_record("K1", "v1", judge.VERDICT_FAIL),
        _stage_record("S1", "v0", judge.VERDICT_PASS),
    ])
    assert run_eval.compute_exit_code(failed) == 1

    environment = _exit_report("stage", [_stage_record("S2", "v0", judge.VERDICT_ENVIRONMENT)])
    assert run_eval.compute_exit_code(environment) == 2

    clean = _exit_report("stage", [_stage_record("S2", "v0", judge.VERDICT_PASS)])
    assert run_eval.compute_exit_code(clean) == 0


def test_all_mode_prints_mandated_unreliable_wording() -> None:
    """全量报告必须写"检测到 N 条拒答跳过的不可靠通过"，不能写成"不可靠通过共 N 条"。"""
    lucky = _stage_record("R2", "v1", judge.VERDICT_PASS)
    lucky["category"] = "拒答"
    lucky["runs"] = [
        {
            "run": 1,
            "status": "no_sql",
            "sql": None,
            "guard": None,
            "failures": [],
            "refusal_accepted": True,
            "skipped_checks": [],
        }
    ]
    report = judge.aggregate([lucky], anchor_today=ANCHOR.isoformat(), repeats=1)
    report["run_context"] = {
        "mode": "all",
        "stage": None,
        "stages": {"v0": {"status": "done"}, "v1": {"status": "pending"}},
    }
    text = run_eval.render_text(
        report,
        cases_path=Path("cases.json"),
        db_path=Path("demo.db"),
        model="",
        used_llm=True,
        exit_code=run_eval.compute_exit_code(report),
    )

    assert "检测到 1 条拒答跳过的不可靠通过" in text
    assert "不可靠通过共" not in text
    assert "R2 [v1/拒答]" in text


# --- 已知缺陷挂账豁免：退出码、聚合、渲染、透传 ---

_KD = {
    "id": "KD-001",
    "root_cause": "文本级注入无法跨子查询定位条件",
    "fix_stage": "阶段 4（AST 改写）",
    "release_condition": "修复落地且某轮 3/3 全绿后删除本块",
}


def _exit_report_with_v2_done(records: list[dict]) -> dict:
    report = _exit_report("all", records)
    report["run_context"]["stages"]["v2"] = {"status": "done"}
    return report


def test_all_mode_known_defect_failure_is_exempted() -> None:
    """done 阶段带挂账标记的失败不置退出码；不带标记的同样失败必须置 1——反向守卫。"""
    exempt = _stage_record("D15", "v2", judge.VERDICT_FAIL)
    exempt["known_defect"] = _KD
    assert run_eval.compute_exit_code(_exit_report_with_v2_done([exempt])) == 0

    plain = _stage_record("D23", "v2", judge.VERDICT_FAIL)
    assert run_eval.compute_exit_code(_exit_report_with_v2_done([plain])) == 1

    both = _exit_report_with_v2_done([exempt, plain])
    assert run_eval.compute_exit_code(both) == 1


def test_stage_mode_ignores_exemption() -> None:
    """--stage 单跑是成绩单不是门禁：挂账题红着，退出码照样 1。"""
    exempt = _stage_record("D15", "v2", judge.VERDICT_FAIL)
    exempt["known_defect"] = _KD
    report = _exit_report("stage", [exempt])
    report["run_context"]["stages"]["v2"] = {"status": "done"}
    assert run_eval.compute_exit_code(report) == 1


def test_aggregate_counts_exempted_failures_in_overview() -> None:
    """overview 要能报"红了几条、其中几条挂账"，豁免数量来自数据不是口算。"""
    exempt = _stage_record("D15", "v2", judge.VERDICT_FAIL)
    exempt["known_defect"] = _KD
    plain = _stage_record("D23", "v2", judge.VERDICT_FAIL)
    report = judge.aggregate([exempt, plain], anchor_today=ANCHOR.isoformat(), repeats=3)
    assert report["overview"]["fail"] == 2
    assert report["overview"]["exempted"] == 1


def test_render_shows_known_defect_ledger() -> None:
    """红照报：明细里给挂账编号、根因、修复归属、解除条件四件套，一个都少不了。"""
    exempt = _stage_record("D15", "v2", judge.VERDICT_FAIL)
    exempt["known_defect"] = _KD
    report = _exit_report_with_v2_done([exempt])
    text = run_eval.render_text(
        report,
        cases_path=Path("cases.json"),
        db_path=Path("demo.db"),
        model="",
        used_llm=False,
        exit_code=run_eval.compute_exit_code(report),
    )
    assert "挂账豁免 KD-001" in text
    assert _KD["root_cause"] in text
    assert _KD["fix_stage"] in text
    assert _KD["release_condition"] in text
    assert "❌ 失败" in text  # 挂账不改判决颜色


def test_run_cases_passes_known_defect_into_records(demo_db: Path) -> None:
    """透传账：用例带挂账块 -> 报告记录原样带出；不带 -> 恒为 null（键必须在，契约稳定）。"""
    kd_case = _case("KD1", "SELECT COUNT(*) AS n FROM orders", [{"check": "status_in", "expect": ["ok"]}])
    kd_case["stage"] = "v2"
    kd_case["known_defect"] = _KD
    plain_case = _case("KD2", "SELECT COUNT(*) AS n FROM orders", [{"check": "status_in", "expect": ["ok"]}])
    plain_case["stage"] = "v2"

    records = _run([kd_case, plain_case], demo_db)
    by_id = {record["id"]: record for record in records}
    assert by_id["KD1"]["known_defect"] == _KD
    assert "known_defect" in by_id["KD2"]
    assert by_id["KD2"]["known_defect"] is None
