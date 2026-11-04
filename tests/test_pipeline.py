from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from data_agent.pipeline import ask
from data_agent.services.llm_client import GeneratedSql


@pytest.fixture()
def demo_db(tmp_path):
    """创建带 customers/orders 的临时演示库。

    orders.status 与真实演示库对齐：语义层的订单范围注入要有列可注入。
    """
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


def test_caliber_note_is_carried_into_outcome(demo_db):
    """模型在 SQL 之外给的口径声明要原样进 outcome：评测读得到，CLI 也打得出来。"""

    def generate(question: str, schema_text: str, today: date) -> GeneratedSql:
        """返回带口径声明的模型输出。"""
        sql = "SELECT count(*) AS n FROM orders WHERE amount > 0"
        raw = f"```sql\n{sql}\n```\n口径：含税，按下单时间；只算已支付，退款与取消不计入。"
        return GeneratedSql(
            sql=sql,
            raw_response=raw,
            caliber_note="口径：含税，按下单时间；只算已支付，退款与取消不计入。",
        )

    outcome = ask("上个月销售额", generator=generate, db_path=demo_db)
    assert outcome["status"] == "ok"
    assert outcome["caliber_note"] == "口径：含税，按下单时间；只算已支付，退款与取消不计入。"
    assert "含税" in outcome["llm_response"]


def test_caliber_note_is_null_when_model_gives_only_sql(demo_db):
    """模型只回 SQL 没声明口径时，caliber_note 是 null，评测能据此判定“没声明”。"""
    outcome = ask(
        "一共有多少订单",
        generator=_stub("SELECT count(*) AS n FROM orders"),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["caliber_note"] is None


def test_caliber_note_is_null_on_injected_path(demo_db):
    """注入路径没有模型回复，口径声明字段保持 null，但字段本身必须在。"""
    outcome = ask(
        "一共有多少订单",
        db_path=demo_db,
        injected_sql="SELECT count(*) AS n FROM orders",
    )
    assert outcome["status"] == "ok"
    assert "caliber_note" in outcome
    assert outcome["caliber_note"] is None


def test_caliber_note_survives_refusal(demo_db):
    """拒答分支只输出 NO_SQL：没有口径声明，字段仍为 null 而不是缺失。"""
    outcome = ask(
        "今天天气怎么样",
        generator=_stub(None, raw="NO_SQL"),
        db_path=demo_db,
    )
    assert outcome["status"] == "no_sql"
    assert outcome["caliber_note"] is None


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


def test_missing_order_scope_is_injected_and_recorded(demo_db):
    """问题命中"销售额"但 SQL 没写 status 条件：系统补进 WHERE，enforcement 留痕。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub("SELECT sum(amount) AS total FROM orders WHERE amount > 0"),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert "status = 'paid'" in outcome["sql"]
    assert outcome["result"]["rows"] == [{"total": 60.0}]
    enforcement = outcome["enforcement"]
    assert enforcement["metric"] == "销售额"
    assert enforcement["changed"] is True
    assert [a["type"] for a in enforcement["actions"]] == ["added"]


def test_conflicting_order_scope_is_replaced_by_definition(demo_db):
    """模型写了 status='refunded' 与指标定义冲突：以定义为准改写，并记录冲突。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub("SELECT sum(amount) AS total FROM orders WHERE status = 'refunded'"),
        db_path=demo_db,
    )
    assert outcome["status"] == "ok"
    assert "refunded" not in outcome["sql"]
    assert "status = 'paid'" in outcome["sql"]
    assert outcome["result"]["rows"] == [{"total": 60.0}]
    actions = outcome["enforcement"]["actions"]
    assert [a["type"] for a in actions] == ["conflict_replaced"]
    assert actions[0]["from"] == "status = 'refunded'"


def test_model_written_scope_left_untouched(demo_db):
    """模型已经写了必需条件：SQL 原样，changed=False，但记录仍在，评测可区分来源。"""
    sql = "SELECT sum(amount) AS total FROM orders WHERE status = 'paid'"
    outcome = ask("上个月销售额是多少", generator=_stub(sql), db_path=demo_db)
    assert outcome["sql"] == sql
    assert outcome["enforcement"]["changed"] is False
    assert [a["type"] for a in outcome["enforcement"]["actions"]] == ["already_present"]


def test_question_without_metric_has_no_enforcement(demo_db):
    """问题没匹配到指标：不注入，enforcement 为 null，SQL 原样执行。"""
    outcome = ask(
        "一共有多少订单",
        generator=_stub("SELECT count(*) AS n FROM orders"),
        db_path=demo_db,
    )
    assert outcome["enforcement"] is None
    assert outcome["sql"] == "SELECT count(*) AS n FROM orders"
    assert outcome["result"]["rows"] == [{"n": 3}]


def test_injected_sql_also_passes_scope_enforcement(demo_db):
    """所有 SQL 都过注入这道检查：injected_sql 路径缺条件同样被补上。"""
    outcome = ask(
        "上个月订单数是多少",
        db_path=demo_db,
        injected_sql="SELECT count(*) AS n FROM orders",
    )
    assert outcome["status"] == "ok"
    assert outcome["sql"] == "SELECT count(*) AS n FROM orders WHERE status = 'paid'"
    assert outcome["enforcement"]["changed"] is True
    assert outcome["result"]["rows"] == [{"n": 3}]


def test_enforcement_runs_before_guard(demo_db):
    """补条件发生在校验之前：guard 看到并放行的就是最终执行的 SQL。"""
    outcome = ask(
        "销售额是多少",
        generator=_stub("SELECT sum(amount) FROM orders"),
        db_path=demo_db,
    )
    assert outcome["guard"]["passed"] is True
    assert "status = 'paid'" in outcome["sql"]


def test_no_sql_carries_null_enforcement(demo_db):
    """拒答路径没有 SQL，也就没有注入：字段存在但为 null。"""
    outcome = ask("今天天气怎么样", generator=_stub(None, raw="NO_SQL"), db_path=demo_db)
    assert outcome["status"] == "no_sql"
    assert "enforcement" in outcome
    assert outcome["enforcement"] is None


@pytest.fixture()
def time_db(tmp_path):
    """带 order_date（只有日期）和 paid_at（带时分）的临时库，列声明与演示库一致。

    9 月 30 日那条带时分的已支付订单是 K1 第 3 次失败的关键：闭区间上界会把它漏掉。
    """
    path = tmp_path / "time.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                order_date TEXT NOT NULL,
                paid_at TEXT,
                status TEXT NOT NULL,
                amount REAL NOT NULL,
                tax_amount REAL
            );
            INSERT INTO orders VALUES
                (1, '2026-09-01', '2026-09-01 09:10:00', 'paid', 100.0, 10.0),
                (2, '2026-09-30', '2026-09-30 21:45:00', 'paid', 200.0, 20.0),
                (3, '2026-08-31', '2026-08-31 09:00:00', 'paid', 999.0, 99.0);
            """
        )
        conn.commit()
    return path


def test_datetime_closed_upper_bound_is_fixed_before_execution(time_db):
    """模型把 DATETIME 字段当 DATE 写闭区间：系统改边界、不改字段，且发生在执行之前。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub(
            "SELECT sum(amount) AS total FROM orders "
            "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
        ),
        db_path=time_db,
    )
    assert outcome["status"] == "ok"
    assert "paid_at" in outcome["sql"] and "order_date" not in outcome["sql"]
    assert "paid_at < '2026-10-01'" in outcome["sql"]
    assert outcome["result"]["rows"] == [{"total": 300.0}]
    time_fix = outcome["time_fix"]
    assert time_fix["changed"] is True
    assert [a["type"] for a in time_fix["actions"]] == ["boundary_rewritten"]
    assert time_fix["actions"][0]["from"] == "paid_at BETWEEN '2026-09-01' AND '2026-09-30'"


def test_time_fix_and_enforcement_are_recorded_separately(time_db):
    """订单范围补进 WHERE 之后，时间修正看到的是补过的 SQL：两道痕迹各自独立。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub(
            "SELECT sum(amount) AS total FROM orders "
            "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
        ),
        db_path=time_db,
    )
    assert outcome["enforcement"]["changed"] is True
    assert [a["type"] for a in outcome["enforcement"]["actions"]] == ["added"]
    assert "status = 'paid'" in outcome["sql"]
    assert "< '2026-10-01'" in outcome["sql"]
    assert outcome["guard"]["passed"] is True


def test_date_field_boundary_is_left_untouched(time_db):
    """order_date 只有日期：闭区间写法本来就对，SQL 原样，只留 already_correct 痕迹。"""
    sql = (
        "SELECT sum(amount) AS total FROM orders "
        "WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30' AND status = 'paid'"
    )
    outcome = ask("上个月销售额是多少", generator=_stub(sql), db_path=time_db)
    assert outcome["sql"] == sql
    assert outcome["time_fix"]["changed"] is False
    assert [a["type"] for a in outcome["time_fix"]["actions"]] == ["already_correct"]
    assert outcome["result"]["rows"] == [{"total": 300.0}]


def test_question_without_time_condition_has_no_time_fix(time_db):
    """SQL 里没有时间条件时 time_fix 为 null，不影响执行。"""
    outcome = ask(
        "一共有多少订单",
        generator=_stub("SELECT count(*) AS n FROM orders"),
        db_path=time_db,
    )
    assert "time_fix" in outcome
    assert outcome["time_fix"] is None
    assert outcome["result"]["rows"] == [{"n": 3}]


def test_injected_sql_also_passes_time_boundary_check(time_db):
    """注入路径同样过时间校验：缺 key 也能跑，但边界照样按字段类型修正。"""
    outcome = ask(
        "上个月销售额是多少",
        db_path=time_db,
        injected_sql=(
            "SELECT sum(amount) AS total FROM orders "
            "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30' AND status = 'paid'"
        ),
    )
    assert outcome["status"] == "ok"
    assert "paid_at >= '2026-09-01' AND paid_at < '2026-10-01'" in outcome["sql"]
    assert outcome["time_fix"]["changed"] is True
    assert outcome["result"]["rows"] == [{"total": 300.0}]


def test_time_fix_never_blocks_a_safe_sql(time_db):
    """时间校验只改写法，不参与拦截：guard 看到的仍是可放行的只读 SELECT。"""
    outcome = ask(
        "上个月销售额是多少",
        generator=_stub(
            "SELECT sum(amount) AS total FROM orders "
            "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
        ),
        db_path=time_db,
    )
    assert outcome["status"] == "ok"
    assert outcome["guard"] == {"passed": True, "violations": []}


class _FixedDate:
    """替换 pipeline 里的 date，使默认日期可预测。"""

    @staticmethod
    def today():
        """返回一个固定的“今天”。"""
        return date(2026, 10, 30)
