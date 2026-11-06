"""报错回炉（阶段 1）的判别性测试。

约定：数据库执行报错（exec_error）时，把失败 SQL + 报错原文拼回问题，请模型修一次；
修好的 SQL 重走全部安检（指标注入 / 时间修正 / guard / 只读执行），最多回炉一次。
timeout 不回炉（重试只会再慢一次）；injected_sql 复放通道没有模型可问，不回炉。
这些测试在实现前必须跑红——旧实现遇到 exec_error 直接收尾，既没有第二次调用也没有 retry 字段。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from data_agent import pipeline_steps
from data_agent.pipeline import ask
from data_agent.services.executor import QueryTimeout
from data_agent.services.llm_client import GeneratedSql


@pytest.fixture()
def demo_db(tmp_path):
    """带 customers/orders 的临时演示库，orders.status 与真实库对齐。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO customers VALUES (1, '张三'), (2, '李四');
            INSERT INTO orders VALUES
                (1, 1, 10.0, 'paid'), (2, 1, 20.0, 'paid'), (3, 2, 30.0, 'paid');
            """
        )
        conn.commit()
    return path


class ScriptedGenerator:
    """按脚本依次产出回复的假生成器；记录每次调用收到的问题文本与 schema。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, question: str, schema_text: str, today: date) -> GeneratedSql:
        item = self.script.pop(0)
        self.calls.append((question, schema_text))
        if isinstance(item, Exception):
            raise item
        return GeneratedSql(sql=item, raw_response=f"```sql\n{item}\n```") if item is not None else GeneratedSql(
            sql=None, raw_response="NO_SQL：这些问题我用现有的表答不了"
        )


def _ask(generator, db_path, question="一共有多少订单"):
    return ask(question, generator=generator, db_path=db_path, today=date(2026, 10, 30))


def test_exec_error_is_retried_once_and_fixed(demo_db):
    """第一次报错、第二次修好：终态 ok，retry 留痕，模型恰好被调两次。"""
    gen = ScriptedGenerator(["SELECT nope FROM orders", "SELECT COUNT(*) AS n FROM orders"])
    outcome = _ask(gen, demo_db)
    assert gen.script == []
    assert len(gen.calls) == 2
    assert outcome["status"] == "ok"
    assert outcome["result"]["rows"] == [{"n": 3}]
    retry = outcome["retry"]
    assert retry is not None and retry["attempted"] is True
    assert retry["result"] == "ok"
    assert "nope" in retry["failed_sql"]
    assert "no such column" in retry["db_error"]
    assert retry["regenerated_sql"] == "SELECT COUNT(*) AS n FROM orders"


def test_repair_prompt_carries_question_sql_and_error(demo_db):
    """回炉的问题文本必须带上原问题、闯祸 SQL 和数据库报错原文。"""
    gen = ScriptedGenerator(["SELECT nope FROM orders", "SELECT COUNT(*) AS n FROM orders"])
    _ask(gen, demo_db)
    repair_question = gen.calls[1][0]
    assert "一共有多少订单" in repair_question
    assert "SELECT nope FROM orders" in repair_question
    assert "no such column" in repair_question.lower()


def test_retry_output_still_passes_all_gates(demo_db):
    """修好的 SQL 不享有豁免：第二次写出危险函数照样 blocked，数据一根毛都没动。"""
    gen = ScriptedGenerator(
        ["SELECT nope FROM orders", "SELECT load_extension('/tmp/evil.so')"]
    )
    outcome = _ask(gen, demo_db)
    assert outcome["status"] == "blocked"
    assert {item["rule"] for item in outcome["guard"]["violations"]} == {"forbidden_function"}
    assert outcome["retry"]["result"] == "blocked"
    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_retry_reenters_semantic_steps(demo_db):
    """回炉后的 SQL 重过指标注入，且口径判定用的是原问题而不是修复提示词。"""
    gen = ScriptedGenerator(
        ["SELECT SUM(nope) AS s FROM orders", "SELECT SUM(amount) AS s FROM orders"]
    )
    outcome = _ask(gen, demo_db, question="总销售额是多少")
    assert outcome["status"] == "ok"
    assert "status = 'paid'" in outcome["sql"]
    assert outcome["enforcement"] is not None and outcome["enforcement"]["changed"] is True
    assert outcome["retry"]["result"] == "ok"


def test_second_exec_error_stays_exec_error(demo_db):
    """修了还是错：认输，终态 exec_error，且绝不第三次调用模型。"""
    gen = ScriptedGenerator(["SELECT nope FROM orders", "SELECT also_missing FROM orders"])
    outcome = _ask(gen, demo_db)
    assert gen.script == []  # 只许调两次，脚本没多备的
    assert len(gen.calls) == 2
    assert outcome["status"] == "exec_error"
    assert outcome["error"]["type"] == "sql_error"
    assert outcome["retry"]["result"] == "exec_error"


def test_no_retry_when_first_execution_succeeds(demo_db):
    """一次就对就不该多花一次模型调用：retry 为 None，只调一次。"""
    gen = ScriptedGenerator(["SELECT COUNT(*) AS n FROM orders"])
    outcome = _ask(gen, demo_db)
    assert len(gen.calls) == 1
    assert outcome["retry"] is None
    assert outcome["status"] == "ok"


def test_injected_channel_never_retries(demo_db):
    """复放通道没有模型可问：exec_error 原样收尾，retry 为 None。"""
    outcome = ask(
        "标签问题",
        db_path=demo_db,
        injected_sql="SELECT nope FROM orders",
        today=date(2026, 10, 30),
    )
    assert outcome["status"] == "exec_error"
    assert outcome["retry"] is None


def test_repair_refused_by_model_keeps_exec_error(demo_db):
    """模型拒绝修改（回 NO_SQL）：终态保持第一次的 exec_error，retry 记 no_sql。"""
    gen = ScriptedGenerator(["SELECT nope FROM orders", None])
    outcome = _ask(gen, demo_db)
    assert outcome["status"] == "exec_error"
    assert outcome["retry"]["result"] == "no_sql"
    assert outcome["retry"]["regenerated_sql"] is None


def test_repair_call_failure_keeps_exec_error(demo_db):
    """回炉那次调用模型本身挂了：不叠第二层错误，终态还是 exec_error，retry 记 llm_error。"""
    gen = ScriptedGenerator(["SELECT nope FROM orders", ConnectionError("网断了")])
    outcome = _ask(gen, demo_db)
    assert outcome["status"] == "exec_error"
    assert outcome["error"]["type"] == "sql_error"
    assert outcome["retry"]["result"] == "llm_error"


def test_timeout_is_not_retried(demo_db, monkeypatch):
    """超时不回炉：重试大概率再超时一次，白烧一次调用。"""

    def fake_run_query(*args, **kwargs):
        raise QueryTimeout("查询超过 5.0s")

    monkeypatch.setattr(pipeline_steps, "run_query", fake_run_query)
    gen = ScriptedGenerator(["SELECT COUNT(*) AS n FROM orders"])
    outcome = _ask(gen, demo_db)
    assert outcome["status"] == "timeout"
    assert outcome["retry"] is None
    assert len(gen.calls) == 1
