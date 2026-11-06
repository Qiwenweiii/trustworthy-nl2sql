"""init_db 的钉死测试：评测期望值所绑的种子、月份窗口、规模与 K1 数值，全部锁在这里。

problems/eval_cases.json 的 meta.notes 声明 K1 的 414786.30 / 354636.03 绑
SEED 与 ORDERS_PER_MONTH，S2 的 40 绑 customers 规模——改这几样之前，本文件
必须先变红，"改数据要重算期望值"就从 notes 里的一句话变成机制。
期望值还隐含绑建库基准日（月份窗口按 today 倒推），所以这里的建库一律传
anchor_today=2026-10-30，与评测锚定日期同源。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from init_db import ORDERS_PER_MONTH, SEED, _last_complete_months, init_db  # noqa: E402

ANCHOR_TODAY = date(2026, 10, 30)  # eval_cases.json meta.anchor_today


@pytest.fixture(scope="module")
def anchored_db(tmp_path_factory) -> Path:
    """锚定日期下重建的演示库：所有期望值以这份库为准。"""
    db_path = tmp_path_factory.mktemp("demo") / "demo.db"
    init_db(db_path, today=ANCHOR_TODAY)
    return db_path


def _scalar(db_path: Path, sql: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchone()[0]


def test_last_complete_months_at_anchor() -> None:
    """锚定日往前推 6 个完整自然月 = 2026-04..2026-09（评测预检 db_months 的同源事实）。"""
    months = _last_complete_months(ANCHOR_TODAY, 6)
    assert months[0][0] == date(2026, 4, 1)
    assert months[-1][1] == date(2026, 9, 30)
    assert [start.strftime("%Y-%m") for start, _ in months] == [
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
    ]


def test_same_seed_same_data(tmp_path: Path) -> None:
    """同种子同基准日两次建库逐行全等：期望值"可复现"本身的钉死。"""
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    init_db(first, today=ANCHOR_TODAY)
    init_db(second, today=ANCHOR_TODAY)
    with sqlite3.connect(first) as left, sqlite3.connect(second) as right:
        for table in ("customers", "orders"):
            rows = f"SELECT * FROM {table} ORDER BY id"
            assert left.execute(rows).fetchall() == right.execute(rows).fetchall()


def test_customers_and_orders_scale(anchored_db: Path) -> None:
    """customers=40 是 S2 row_count 断言的前提；订单数 = 6 月 x ORDERS_PER_MONTH。"""
    assert SEED == 20261029
    assert _scalar(anchored_db, "SELECT COUNT(*) FROM customers") == 40
    assert _scalar(anchored_db, "SELECT COUNT(*) FROM orders") == 6 * ORDERS_PER_MONTH == 960


def test_k1_expected_values_are_pinned(anchored_db: Path) -> None:
    """K1 的两个允许净额与 K1/K2 why 里引用的总额，逐一钉死（绝对容差 0.005）。"""
    september = "order_date BETWEEN '2026-09-01' AND '2026-09-30'"
    net = "SUM(amount - COALESCE(tax_amount, 0))"

    paid_only = _scalar(anchored_db, f"SELECT {net} FROM orders WHERE {september} AND status = 'paid'")
    assert abs(paid_only - 354636.03) < 0.005

    with_refunded = _scalar(
        anchored_db, f"SELECT {net} FROM orders WHERE {september} AND status IN ('paid', 'refunded')"
    )
    assert abs(with_refunded - 414786.30) < 0.005

    gross_all = _scalar(anchored_db, f"SELECT SUM(amount) FROM orders WHERE {september}")
    assert abs(gross_all - 501573.38) < 0.005

    cancelled = _scalar(
        anchored_db, f"SELECT SUM(amount) FROM orders WHERE {september} AND status = 'cancelled'"
    )
    assert abs(cancelled - 32864.79) < 0.005

    refunded = _scalar(
        anchored_db, f"SELECT SUM(amount) FROM orders WHERE {september} AND status = 'refunded'"
    )
    assert abs(refunded - 67969.81) < 0.005
