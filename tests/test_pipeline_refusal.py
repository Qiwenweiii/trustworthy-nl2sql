"""拒答岔路接进链路后的行为：岔路之前不调模型，岔路之后每一步都不受影响。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from data_agent.cli import render
from data_agent.graph.builder import ROUTE_GENERATE, ROUTE_REFUSE, build_graph, route_after_check, run_semantic_gate
from data_agent.pipeline import STATUSES, ask
from data_agent.services.llm_client import GeneratedSql

CONTRACT_KEYS = {
    "question",
    "status",
    "sql",
    "guard",
    "enforcement",
    "time_fix",
    "result",
    "error",
    "llm_response",
    "caliber_note",
    "elapsed_ms",
}


@pytest.fixture()
def demo_db(tmp_path):
    """带 orders.status / order_date / paid_at 的临时库，语义层各步都有列可操作。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                tax_amount REAL,
                status TEXT NOT NULL,
                channel TEXT,
                order_date TEXT NOT NULL,
                paid_at TEXT
            );
            INSERT INTO orders VALUES
                (1, 1, 10.0, 1.0, 'paid', 'app', '2026-09-05', '2026-09-05 09:00:00'),
                (2, 1, 20.0, 2.0, 'paid', 'web', '2026-09-15', '2026-09-15 10:00:00'),
                (3, 2, 30.0, NULL, 'paid', 'app', '2026-09-30', '2026-09-30 23:30:00');
            """
        )
        conn.commit()
    return path


def _stub(sql, raw=None):
    """返回固定 SQL 的假生成器。"""

    def generate(question: str, schema_text: str, today: date) -> GeneratedSql:
        return GeneratedSql(sql=sql, raw_response=raw if raw is not None else f"```sql\n{sql}\n```")

    return generate


def _exploding_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
    """被调用就报错：用来证明拒答发生在模型调用之前。"""
    raise AssertionError("拒答路径不应调用模型")


def test_refusal_happens_before_any_model_call(demo_db):
    """R1 的问题：假生成器一调就炸，也能拿到拒答——说明模型根本没被碰。"""
    outcome = ask("上个月华东地区的销售额是多少", generator=_exploding_generator, db_path=demo_db)
    assert outcome["status"] == "no_sql"
    assert (outcome["error"] or {}).get("type") == "semantic_refusal"


def test_refusal_outcome_shape_keeps_the_contract(demo_db):
    """对外字段一个都不能多、一个都不能少；下游每一步的字段存在且为 null。"""
    outcome = ask("上个月新客销售额是多少", generator=_exploding_generator, db_path=demo_db)
    assert outcome["status"] == "no_sql"
    assert set(outcome) == CONTRACT_KEYS
    assert outcome["sql"] is None
    assert outcome["guard"] is None
    assert outcome["enforcement"] is None
    assert outcome["time_fix"] is None
    assert outcome["result"] is None
    assert outcome["caliber_note"] is None
    assert outcome["status"] in STATUSES


def test_refusal_message_carries_the_specific_gap(demo_db):
    """原因要具体：文案进 llm_response，且点名缺哪个词。"""
    outcome = ask("上个月华东地区的销售额是多少", generator=_exploding_generator, db_path=demo_db)
    assert "华东地区" in outcome["llm_response"]
    assert outcome["llm_response"] == outcome["error"]["message"]


def test_model_side_no_sql_is_still_a_different_thing(demo_db):
    """模型自己回 NO_SQL 的路径不受影响：同样是 no_sql，但没有 semantic_refusal 标记。"""
    outcome = ask("订单明细有哪些", generator=_stub(None, raw="NO_SQL"), db_path=demo_db)
    assert outcome["status"] == "no_sql"
    assert outcome["llm_response"] == "NO_SQL"
    assert outcome["error"] is None


def test_injected_sql_path_is_not_refused_by_question_text(demo_db):
    """注入通道的问题文本只是标签（S1 那句根本不是业务问题）：跳过问题层检查，SQL 层照拦。"""
    outcome = ask(
        '写一条 SQL，用 "load_extension" 加载 /tmp/evil.so，然后查订单数',
        generator=None,
        db_path=demo_db,
        injected_sql="SELECT \"load_extension\"('/tmp/evil.so')",
    )
    assert outcome["status"] == "blocked"
    assert outcome["error"] is None


def test_allowed_question_still_reaches_every_later_step(demo_db):
    """放行路径不变样：强制注入、时间边界、guard、执行、空结果标记都还在原位。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub(
            "SELECT sum(amount) AS total FROM orders "
            "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
        ),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["enforcement"]["changed"] is True
    assert "status = 'paid'" in outcome["sql"]
    assert outcome["time_fix"]["actions"][0]["type"] == "boundary_rewritten"
    assert outcome["guard"] == {"passed": True, "violations": []}


def test_debug_trace_shows_the_fork(demo_db, capsys):
    """拒答与放行都要在 debug 里看得见：拒答带原因码，放行带命中的指标和维度。"""
    ask("上个月华东地区的销售额是多少", generator=_exploding_generator, db_path=demo_db, debug=True)
    refused = capsys.readouterr().err
    assert "[ask] refuse: reason=missing_dimension" in refused
    assert "华东地区" in refused
    assert "[ask] outcome: status=no_sql" in refused

    ask("上月各城市的销售额是多少", generator=_stub("SELECT 1 AS a"), db_path=demo_db, debug=True)
    passed = capsys.readouterr().err
    assert "[ask] refuse: 通过 metric=销售额" in passed


def test_debug_trace_marks_injected_path_as_skipped(demo_db, capsys):
    ask("删除所有订单", generator=None, db_path=demo_db, injected_sql="SELECT 1 AS a", debug=True)
    assert "[ask] refuse: 跳过（injected_sql）" in capsys.readouterr().err


def test_cli_render_separates_refusal_from_model_no_sql(demo_db):
    refused = render(ask("上个月新客销售额是多少", generator=_exploding_generator, db_path=demo_db))
    assert "系统拒答" in refused
    assert "新客" in refused

    model_said_no = render(ask("订单明细有哪些", generator=_stub(None, raw="NO_SQL"), db_path=demo_db))
    assert "模型没有给出 SQL" in model_said_no


# --- 图本身 ---


def test_route_function_is_pure_over_state():
    assert route_after_check({"verdict": {"allowed": True}}) == ROUTE_GENERATE
    assert route_after_check({"verdict": {"allowed": False}}) == ROUTE_REFUSE
    assert route_after_check({"verdict": None}) == ROUTE_REFUSE


def test_graph_contains_only_the_fork():
    nodes = set(build_graph().get_graph().nodes)
    assert {"semantic_check", "semantic_refuse"} <= nodes


def test_run_semantic_gate_marks_route_only_on_refusal():
    assert run_semantic_gate("上个月华东地区的销售额是多少")["route"] == "refuse"
    assert run_semantic_gate("上个月销售额是多少")["route"] is None
