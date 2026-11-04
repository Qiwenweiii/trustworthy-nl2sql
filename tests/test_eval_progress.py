"""进度展示：每次采样一条事件、预检跳过单独一条、写到调用方指定的流（stderr）。"""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from evaluation import run_eval

ANCHOR = date(2026, 10, 30)


@pytest.fixture()
def demo_db(tmp_path: Path) -> Path:
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount REAL NOT NULL,
                                 order_date TEXT NOT NULL);
            INSERT INTO customers VALUES (1, '张三'), (2, '李四');
            INSERT INTO orders VALUES (1, 1, 10.0, '2026-09-05'), (2, 1, 20.0, '2026-09-15'),
                                      (3, 2, 30.0, '2026-09-20');
            """
        )
        conn.commit()
    return path


def _injected_case(case_id: str, injected: str, *, repeats: int | None = None) -> dict:
    case = {
        "id": case_id,
        "stage": "v0",
        "category": "安全",
        "question": f"用例 {case_id}",
        "deterministic": True,
        "injected_sql": injected,
        "expected_status": "ok",
        "expected_behavior": "看 guard",
        "capability": "安全层",
        "assertions": [
            {
                "check": "guard_passed",
                "expect": True,
                "root_cause": f"{case_id.lower()}-guard",
                "capability": "安全层",
                "why": "guard 判定要符合期望",
                "landing": "v0 可修",
            }
        ],
    }
    if repeats is not None:
        case["deterministic"] = False
        case["repeats"] = repeats
    return case


def _model_case(case_id: str, *, months: list[str] | None = None) -> dict:
    case = {
        "id": case_id,
        "stage": "v1",
        "category": "口径",
        "question": "上个月销售额是多少",
        "expected_status": "ok",
        "expected_behavior": "出数",
        "capability": "语义层",
        "assertions": [
            {
                "check": "status_in",
                "expect": ["ok"],
                "root_cause": "sql-generation",
                "capability": "语义层",
                "why": "该出数",
                "landing": "等 v1",
            }
        ],
    }
    if months:
        case["requires_data_months"] = months
    return case


def _collect(cases: list[dict], db_path: Path, *, generator=None, repeats: int = 1, months=None):
    events: list[dict] = []
    records = run_eval.run_cases(
        cases,
        generator=generator,
        db_path=db_path,
        max_rows=200,
        timeout_s=5.0,
        today=ANCHOR,
        repeats=repeats,
        months=["2026-09"] if months is None else months,
        on_run=events.append,
    )
    return records, events


def test_one_event_per_run_with_sequence_and_total(demo_db) -> None:
    """每个采样一条事件；seq 从 1 连续递增，planned 是全部用例的计划采样总数。"""
    cases = [
        _injected_case("A1", "SELECT 1 AS one"),
        _injected_case("A2", "SELECT 2 AS two", repeats=3),
    ]
    _, events = _collect(cases, demo_db)

    run_events = [e for e in events if e["kind"] == "run"]
    assert len(run_events) == 4
    assert [e["seq"] for e in run_events] == [1, 2, 3, 4]
    assert all(e["planned"] == 4 for e in run_events)
    assert [e["case_id"] for e in run_events] == ["A1", "A2", "A2", "A2"]
    assert [(e["run"], e["total_runs"]) for e in run_events] == [(1, 1), (1, 3), (2, 3), (3, 3)]


def test_event_carries_status_guard_and_failed_count(demo_db) -> None:
    """事件里要能直接拼出进度行：status、guard 原样、失败断言条数。"""
    blocking = _injected_case("B1", "DELETE FROM orders")
    blocking["expected_status"] = "blocked"
    blocking["assertions"][0] = {
        "check": "guard_rules_include",
        "expect": [{"rule": "forbidden_function"}],
        "root_cause": "b1-rule",
        "capability": "安全层",
        "why": "拦因要含指定规则",
        "landing": "v0 可修",
    }
    _, events = _collect([blocking], demo_db)

    event = events[0]
    assert event["status"] == "blocked"
    assert event["guard"]["passed"] is False
    assert event["failed_assertions"] == 1
    assert event["injected"] is True


def test_llm_error_emits_event_with_failed_none(demo_db) -> None:
    """模型故障也算一次采样：状态 llm_error，不做判定所以失败数为 None。"""

    def broken(question: str, schema_text: str, today: date):
        raise RuntimeError("网络断了")

    _, events = _collect([_model_case("C1")], demo_db, generator=broken)

    assert len(events) == 1
    assert events[0]["status"] == "llm_error"
    assert events[0]["guard"] is None
    assert events[0]["failed_assertions"] is None


def _refusal_case(case_id: str) -> dict:
    """表里没有的维度：语义层在模型之前就拒答，全程 0 次模型调用。"""
    case = _model_case(case_id)
    case["category"] = "拒答"
    case["question"] = "上个月华东地区的销售额是多少"
    case["expected_status"] = "no_sql"
    case["assertions"][0]["expect"] = ["no_sql"]
    return case


def test_refusal_run_record_keeps_error_type(demo_db) -> None:
    """run 记录要带 error：报告得能分辨"系统机制拒的"和"模型自己拒的"。"""

    def exploding(*args):
        raise AssertionError("语义拒答不该调用模型")

    records, _ = _collect([_refusal_case("R1")], demo_db, generator=exploding)

    run = records[0]["runs"][0]
    assert run["status"] == "no_sql"
    assert run["error"]["type"] == "semantic_refusal"
    assert "华东地区" in run["error"]["message"]


def test_llm_error_run_record_keeps_error(demo_db) -> None:
    """模型故障的报错也进同一个 error 键，不再另立 environment 键。"""

    def broken(question: str, schema_text: str, today: date):
        raise RuntimeError("网络断了")

    records, _ = _collect([_model_case("C1")], demo_db, generator=broken)

    run = records[0]["runs"][0]
    assert run["status"] == "llm_error"
    assert run["error"] == {"type": "llm_error", "message": "网络断了"}
    assert "environment" not in run


def test_precheck_skip_emits_single_skipped_event(demo_db) -> None:
    """预检不通过的用例只发一条 skipped 事件，不产生采样事件。"""
    _, events = _collect(
        [_model_case("D1", months=["2026-08"])], demo_db, generator=lambda *a: None
    )

    assert [e["kind"] for e in events] == ["skipped"]
    assert events[0]["case_id"] == "D1"
    assert "2026-08" in events[0]["reason"]


def test_progress_line_contains_sequence_case_and_guard_reason() -> None:
    event = {
        "kind": "run",
        "seq": 5,
        "planned": 27,
        "case_id": "K1",
        "category": "口径",
        "injected": False,
        "run": 2,
        "total_runs": 3,
        "status": "blocked",
        "elapsed_ms": 3200.0,
        "guard": {
            "passed": False,
            "violations": [{"rule": "not_select", "detail": "x", "token": "delete"}],
        },
        "failed_assertions": 2,
    }
    line = run_eval._progress_line(event)

    assert line.startswith("[ 5/27] K1 [口径] run2/3 模型 -> status=blocked")
    assert "耗时 3.2s" in line
    assert "失败断言 2" in line
    assert "拦截 passed=False ← not_select[delete]" in line


def test_skipped_line_reports_reason() -> None:
    line = run_eval._progress_line(
        {"kind": "skipped", "case_id": "D1", "category": "口径", "planned": 4, "reason": "缺月份"}
    )
    assert "D1" in line and "缺月份" in line


def test_writer_writes_only_to_given_stream() -> None:
    """进度写到调用方传入的流（stderr），不能污染 stdout 上的报告。"""
    stream = io.StringIO()
    writer = run_eval.make_progress_writer(stream)
    writer(
        {
            "kind": "run",
            "seq": 1,
            "planned": 1,
            "case_id": "S1",
            "category": "安全",
            "injected": True,
            "run": 1,
            "total_runs": 1,
            "status": "blocked",
            "elapsed_ms": 1.0,
            "guard": {"passed": False, "violations": []},
            "failed_assertions": 0,
        }
    )
    assert " 1/1] S1" in stream.getvalue()


def test_plan_summary_splits_injected_model_and_skipped() -> None:
    cases = [
        _injected_case("A1", "SELECT 1 AS one"),
        _injected_case("A2", "SELECT 2 AS two", repeats=2),
        _model_case("M1"),
        _model_case("M2", months=["2026-08"]),
    ]
    plan = run_eval.plan_summary(cases, repeats=3, months=["2026-09"])

    assert plan == {
        "cases": 4,
        "injected_runs": 3,  # A1 恒 1 + A2 用例级 repeats=2
        "model_runs": 3,  # M1 走 --repeats=3；M2 被预检跳过不计
        "skipped_cases": 1,
        "planned_runs": 6,
    }
