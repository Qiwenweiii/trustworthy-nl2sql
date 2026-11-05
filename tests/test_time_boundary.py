"""时间边界校验的单元测试。

考点是"只修边界、不动字段"：DATETIME 字段的闭区间上界要改成半开区间，
DATE 字段、已带时分、函数包裹、判不出类型的都一律不动；
库读不到、列找不到只能是 skipped，不能抛异常。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from data_agent.services.time_boundary import fix_time_boundaries

SEPT_BETWEEN = "SELECT sum(amount) AS total FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"


@pytest.fixture()
def time_db(tmp_path):
    """列声明与演示库一致（TEXT），paid_at 带时分、order_date 只有日期。

    9 月 30 日当天有两条带时分的已支付订单：闭区间上界 '2026-09-30' 会把它们漏掉，
    这正是 K1 第 3 次失败的形态。
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
                amount REAL NOT NULL
            );
            INSERT INTO orders VALUES
                (1, '2026-09-01', '2026-09-01 09:10:00', 'paid', 100.0),
                (2, '2026-09-30', '2026-09-30 21:45:00', 'paid', 200.0),
                (3, '2026-09-30', '2026-09-30 00:30:00', 'paid', 50.0),
                (4, '2026-08-31', '2026-08-31 10:00:00', 'paid', 999.0),
                (5, '2026-09-30', NULL, 'cancelled', 400.0);
            CREATE TABLE refunds (paid_at TEXT, amount REAL);
            INSERT INTO refunds VALUES ('2026-09-30 12:00:00', 10.0);
            CREATE TABLE typed (stamp DATETIME, only_date DATE, num INTEGER);
            INSERT INTO typed VALUES ('2026-09-30 23:00:00', '2026-09-30', 7);
            CREATE TABLE messy (weird TEXT);
            INSERT INTO messy VALUES ('2026-09');
            """
        )
        conn.commit()
    return path


def _types(record: dict | None) -> list[str]:
    return [action["type"] for action in (record or {}).get("actions", [])]


def _value(db_path: Path, sql: str):
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return conn.execute(sql).fetchone()[0]


def test_datetime_closed_upper_bound_is_rewritten(time_db):
    out, record = fix_time_boundaries(SEPT_BETWEEN, db_path=time_db)
    assert out == (
        "SELECT sum(amount) AS total FROM orders "
        "WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    )
    assert record["changed"] is True
    assert _types(record) == ["boundary_rewritten"]
    assert record["actions"][0]["from"] == (
        "paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
    )


def test_rewrite_recovers_month_end_rows_dropped_by_between(time_db):
    """判别性用例：同一条 SQL，改写前漏掉 9 月 30 日当天 00:00 后的 250 元。"""
    out, record = fix_time_boundaries(SEPT_BETWEEN, db_path=time_db)
    assert record["changed"] is True
    assert _value(time_db, out) == 350.0
    assert _value(time_db, SEPT_BETWEEN) == 100.0


def test_time_fix_keeps_the_column_model_chose(time_db):
    """硬约束：只改边界写法，paid_at 不能被换成 order_date。"""
    out, _ = fix_time_boundaries(SEPT_BETWEEN, db_path=time_db)
    assert "order_date" not in out
    assert out.count("paid_at") == 2


def test_date_column_boundary_is_left_alone(time_db):
    sql = "SELECT sum(amount) FROM orders WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["already_correct"]


def test_upper_bound_with_time_is_left_alone(time_db):
    sql = (
        "SELECT sum(amount) FROM orders "
        "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30 23:59:59'"
    )
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["already_correct"]


def test_lte_date_only_is_rewritten(time_db):
    sql = "SELECT sum(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at <= '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == (
        "SELECT sum(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    )
    assert record["changed"] is True


def test_strict_less_than_is_not_touched(time_db):
    """`<` 本来就是排他上界，不属于"闭区间上界漏当天"，系统不替模型改口径。"""
    sql = "SELECT sum(amount) FROM orders WHERE paid_at < '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record is None


def test_month_end_rolls_into_next_year(time_db):
    sql = "SELECT sum(amount) FROM orders WHERE paid_at BETWEEN '2026-12-01' AND '2026-12-31'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert "< '2027-01-01'" in out
    assert record["changed"] is True


def test_function_wrapped_column_is_not_touched(time_db):
    """DATE(paid_at) 包一层已经是正确写法，不能二次改写。"""
    sql = "SELECT sum(amount) FROM orders WHERE DATE(paid_at) BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record is None


def test_grouping_parenthesis_still_counts_as_bare_column(time_db):
    sql = (
        "SELECT sum(amount) FROM orders "
        "WHERE (paid_at BETWEEN '2026-09-01' AND '2026-09-30') OR channel = 'app'"
    )
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert "paid_at >= '2026-09-01' AND paid_at < '2026-10-01'" in out
    assert record["changed"] is True


def test_alias_prefix_is_kept_in_rewrite(time_db):
    sql = (
        "SELECT sum(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
    )
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == (
        "SELECT sum(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'"
    )
    assert record["changed"] is True


def test_predicates_inside_literal_and_comment_are_ignored(time_db):
    literal = (
        "SELECT 'paid_at BETWEEN ''2026-09-01'' AND ''2026-09-30''' AS note FROM orders"
    )
    assert fix_time_boundaries(literal, db_path=time_db) == (literal, None)

    comment = (
        "-- paid_at BETWEEN '2026-09-01' AND '2026-09-30'\n"
        "SELECT count(*) AS n FROM orders"
    )
    assert fix_time_boundaries(comment, db_path=time_db) == (comment, None)


def test_multiple_predicates_are_all_rewritten(time_db):
    """两个谓词都要改：从右往左替换，左边的匹配位置不能因为长度变化而错位。"""
    sql = (
        "SELECT count(*) FROM orders WHERE status = 'paid' "
        "AND (paid_at <= '2026-09-30' OR paid_at <= '2026-08-31')"
    )
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out.count("< '") == 2
    assert "< '2026-10-01'" in out and "< '2026-09-01'" in out
    assert record["changed"] is True
    assert _types(record) == ["boundary_rewritten", "boundary_rewritten"]


def test_declared_types_are_used_without_sampling(tmp_path):
    """声明就是 DATETIME/DATE 时按声明判：不依赖取值形态。"""
    path = tmp_path / "typed.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE typed (stamp DATETIME, only_date DATE);
            INSERT INTO typed VALUES ('2026-09-30 23:00:00', '2026-09-30');
            """
        )
        conn.commit()
    stamp_sql = "SELECT count(*) FROM typed WHERE stamp BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(stamp_sql, db_path=path)
    assert "stamp >= '2026-09-01' AND stamp < '2026-10-01'" in out
    assert record["actions"][0]["to"].startswith("stamp >= ")

    date_sql = "SELECT count(*) FROM typed WHERE only_date BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(date_sql, db_path=path)
    assert out == date_sql
    assert record["changed"] is False


def test_unknown_column_is_skipped_without_error(time_db):
    sql = "SELECT sum(amount) FROM orders WHERE not_a_col BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["skipped"]
    assert "不存在" in record["actions"][0]["detail"]


def test_missing_table_is_skipped_without_error(time_db):
    sql = "SELECT sum(amount) FROM missing_table WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert _types(record) == ["skipped"]


def test_ambiguous_column_is_skipped(time_db):
    """列名在多张表里都有且没写别名时不猜归属，宁可不改。"""
    sql = (
        "SELECT count(*) FROM orders JOIN refunds ON refunds.paid_at = orders.paid_at "
        "WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
    )
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert _types(record) == ["skipped"]
    assert "无法确定归属" in record["actions"][0]["detail"]


def test_unrecognizable_value_shape_is_skipped(tmp_path):
    """取值不是完整日期形态时判不出类型，不动 SQL。"""
    path = tmp_path / "messy.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            "CREATE TABLE messy (weird TEXT); INSERT INTO messy VALUES ('2026-09');"
        )
        conn.commit()
    sql = "SELECT count(*) FROM messy WHERE weird BETWEEN '2026-09-01' AND '2026-09-30'"
    out, record = fix_time_boundaries(sql, db_path=path)
    assert out == sql
    assert _types(record) == ["skipped"]


def test_unreadable_db_is_skipped_without_error(tmp_path):
    """走 injected_sql 时库可能不存在：判型失败只能记 skipped，不能把链路炸了。"""
    sql = SEPT_BETWEEN
    out, record = fix_time_boundaries(sql, db_path=tmp_path / "nope.db")
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["skipped"]


def test_invalid_upper_date_is_skipped(time_db):
    sql = "SELECT sum(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-02-30'"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert _types(record) == ["skipped"]
    assert "不是合法日期" in record["actions"][0]["detail"]


def test_no_time_predicate_does_not_open_db(time_db, tmp_path):
    """没有时间边界可查时根本不碰数据库：库不存在也不该有动静。"""
    sql = "SELECT count(*) AS n FROM orders"
    assert fix_time_boundaries(sql, db_path=tmp_path / "nope.db") == (sql, None)
    assert fix_time_boundaries("", db_path=time_db) == ("", None)


def test_unclosed_quote_anywhere_blocks_all_rewriting(time_db):
    """统一口径：句中出现未闭合引号即整句判读不懂，连前面的 BETWEEN 也不改。

    合并前只有引号之后的区间受保护，位于引号之前的合法谓词照常被改写；
    两条口径都到不了执行（guard 必拦 malformed_sql），统一后痕迹只剩 skipped。
    """
    sql = SEPT_BETWEEN + " AND note='oops"
    out, record = fix_time_boundaries(sql, db_path=time_db)
    assert out == sql
    assert record["changed"] is False
    assert _types(record) == ["skipped"]
