from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from data_agent.pipeline import ask
from data_agent.services.llm_client import GeneratedSql


@pytest.fixture()
def demo_db(tmp_path):
    """创建带 customers/orders 的临时演示库。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL
            );
            INSERT INTO customers VALUES (1, '张三'), (2, '李四');
            INSERT INTO orders VALUES (1, 1, 10.0), (2, 1, 20.0), (3, 2, 30.0);
            """
        )
        conn.commit()
    return path


def _stub(sql: str | None, raw: str | None = None):
    """构造返回固定 SQL 的假生成器。"""

    def generate(question: str, schema_text: str, today: date) -> GeneratedSql:
        """返回桩 SQL 和对应的原始回复。"""
        return GeneratedSql(
            sql=sql,
            raw_response=raw if raw is not None else f"```sql\n{sql}\n```",
        )

    return generate


def test_safe_query_end_to_end(demo_db):
    """安全 SQL 能一路执行到 ok 并返回结果。"""
    outcome = ask(
        "一共有多少订单",
        generator=_stub("SELECT count(*) AS n FROM orders"),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["guard"] == {"passed": True, "violations": []}
    assert outcome["result"]["rows"] == [{"n": 3}]
    assert outcome["result"]["row_count"] == 1
    assert outcome["result"]["truncated"] is False
    assert outcome["error"] is None


def test_dangerous_function_sql_is_blocked(demo_db):
    """评测第一条用例：模型被诱导调用危险函数，必须被拦且不触碰数据。"""
    dangerous = "SELECT load_extension('/tmp/evil.so')"
    outcome = ask("加载扩展", generator=_stub(dangerous), db_path=demo_db)

    assert outcome["status"] == "blocked"
    assert outcome["guard"]["passed"] is False
    assert [item["rule"] for item in outcome["guard"]["violations"]] == [
        "forbidden_function"
    ]
    assert outcome["guard"]["violations"][0]["token"] == "load_extension"
    assert outcome["result"] is None
    assert outcome["sql"] == dangerous

    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3


def test_delete_sql_is_blocked(demo_db):
    """DELETE 语句被拦，且订单数据不变。"""
    outcome = ask(
        "删除所有订单",
        generator=_stub("DELETE FROM orders"),
        db_path=demo_db,
    )
    assert outcome["status"] == "blocked"
    assert {item["rule"] for item in outcome["guard"]["violations"]} == {
        "not_select",
        "forbidden_keyword",
    }
    assert outcome["result"] is None

    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3


def test_no_sql_from_model(demo_db):
    """模型回复 NO_SQL 时状态为 no_sql。"""
    outcome = ask(
        "今天天气怎么样",
        generator=_stub(None, raw="NO_SQL"),
        db_path=demo_db,
    )
    assert outcome["status"] == "no_sql"
    assert outcome["sql"] is None
    assert outcome["guard"] is None
    assert outcome["llm_response"] == "NO_SQL"
    assert outcome["result"] is None


def test_sql_execution_error_is_reported(demo_db):
    """表不存在时返回 exec_error 和原始报错。"""
    outcome = ask(
        "查一张不存在的表",
        generator=_stub("SELECT * FROM missing_table"),
        db_path=demo_db,
    )
    assert outcome["status"] == "exec_error"
    assert outcome["error"]["type"] == "sql_error"
    assert "missing_table" in outcome["error"]["message"]


def test_llm_failure_is_reported(demo_db):
    """LLM 调用异常时返回 llm_error。"""

    def broken_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
        """模拟 LLM 连接失败的生成器。"""
        raise RuntimeError("connection refused")

    outcome = ask("随便问问", generator=broken_generator, db_path=demo_db)
    assert outcome["status"] == "llm_error"
    assert outcome["guard"] is None
    assert outcome["error"] == {"type": "llm_error", "message": "connection refused"}


def test_injected_sql_skips_generator_entirely(demo_db):
    """注入 SQL 时不得触碰模型：给一个会炸的生成器也能跑通。"""

    def exploding_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
        """被调用就报错，用于证明注入路径跳过了 LLM。"""
        raise AssertionError("生成器不应被调用")

    outcome = ask(
        "一共有多少订单",
        generator=exploding_generator,
        db_path=demo_db,
        injected_sql="SELECT count(*) AS n FROM orders",
    )
    assert outcome["status"] == "ok"
    assert outcome["result"]["rows"] == [{"n": 3}]
    assert outcome["llm_response"] is None


def test_injected_sql_is_still_guarded(demo_db):
    """注入只替代模型输出，静态校验与只读执行链路照常生效。"""
    outcome = ask(
        "删除所有订单",
        db_path=demo_db,
        injected_sql="DELETE FROM orders",
    )
    assert outcome["status"] == "blocked"
    assert {item["rule"] for item in outcome["guard"]["violations"]} == {
        "not_select",
        "forbidden_keyword",
    }
    assert outcome["result"] is None

    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3


def test_injected_sql_logs_generator_ignored(demo_db, caplog):
    """同时传 generator 和 injected_sql 时，注入优先并在日志里说明生成器被忽略。"""
    with caplog.at_level("WARNING", logger="data_agent.pipeline"):
        ask(
            "一共有多少订单",
            generator=_stub("SELECT 1 AS a"),
            db_path=demo_db,
            injected_sql="SELECT count(*) AS n FROM orders",
        )
    assert "generator ignored due to injected_sql" in caplog.text


def test_empty_result_is_flagged_and_notified(demo_db):
    """全 NULL / 0 行结果：result.empty_result=True，llm_response 保留原始回复并追加无数据提示。"""
    outcome = ask(
        "没有数据的月份销售额",
        generator=_stub(
            "SELECT sum(amount) AS total FROM orders WHERE customer_id = 99",
            raw="```sql\nSELECT sum(amount) AS total FROM orders WHERE customer_id = 99\n```",
        ),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["result"]["rows"] == [{"total": None}]
    assert outcome["result"]["empty_result"] is True
    assert outcome["llm_response"].startswith("```sql")
    assert "当前条件下没有数据" in outcome["llm_response"]

    zero_rows = ask(
        "不存在的订单",
        generator=_stub("SELECT id FROM orders WHERE id = 999"),
        db_path=demo_db,
    )
    assert zero_rows["result"]["empty_result"] is True
    assert "当前条件下没有数据" in zero_rows["llm_response"]


def test_empty_result_notice_appended_on_injected_path(demo_db):
    """注入路径本来 llm_response 为 null；空结果时提示要能出现在最终输出里。"""

    def exploding_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
        """被调用就报错，用于证明注入路径跳过了 LLM。"""
        raise AssertionError("生成器不应被调用")

    outcome = ask(
        "不存在的订单",
        generator=exploding_generator,
        db_path=demo_db,
        injected_sql="SELECT sum(amount) AS total FROM orders WHERE id = 999",
    )
    assert outcome["status"] == "ok"
    assert outcome["result"]["empty_result"] is True
    assert outcome["llm_response"] == "当前条件下没有数据"


def test_nonempty_result_has_no_notice(demo_db):
    """有数据的正常结果：不标空、不追加提示，llm_response 保持模型原始回复。"""
    outcome = ask(
        "一共有多少订单",
        generator=_stub("SELECT count(*) AS n FROM orders"),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["result"]["empty_result"] is False
    assert outcome["llm_response"] == "```sql\nSELECT count(*) AS n FROM orders\n```"
    assert "当前条件下没有数据" not in outcome["llm_response"]


def test_today_is_passed_through_to_generator(demo_db):
    """ask(today=...) 原样传给生成器，用于评测冻结基准日期。"""
    seen = {}

    def record_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
        """记录收到的日期并返回固定 SQL。"""
        seen["today"] = today
        return GeneratedSql(sql="SELECT count(*) AS n FROM orders", raw_response="x")

    ask("一共有多少订单", generator=record_generator, db_path=demo_db, today=date(2026, 10, 30))
    assert seen["today"] == date(2026, 10, 30)


def test_today_defaults_to_current_date(demo_db, monkeypatch):
    """不传 today 时使用当前日期。"""
    seen = {}

    def record_generator(question: str, schema_text: str, today: date) -> GeneratedSql:
        """记录收到的日期并返回固定 SQL。"""
        seen["today"] = today
        return GeneratedSql(sql="SELECT count(*) AS n FROM orders", raw_response="x")

    monkeypatch.setattr("data_agent.pipeline.date", _FixedDate)
    ask("一共有多少订单", generator=record_generator, db_path=demo_db)
    assert seen["today"] == _FixedDate.today()


def test_generator_required_when_no_injected_sql(demo_db):
    """既不给生成器也不给注入 SQL 属于调用方写错，直接抛 ValueError。"""
    with pytest.raises(ValueError, match="至少提供一个"):
        ask("随便问问", db_path=demo_db)


class _FixedDate:
    """替换 pipeline 里的 date，使默认日期可预测。"""

    @staticmethod
    def today():
        """返回一个固定的“今天”。"""
        return date(2026, 10, 30)
