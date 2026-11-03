#!/usr/bin/env python3
"""生成 v0 演示库：orders + customers，数据按运行当天倒推生成近 6 个完整月。"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "demo.db"
SEED = 20261029

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京"]
SEGMENTS = ["企业", "中小企业", "个人"]
SURNAMES = ["张", "王", "李", "赵", "陈", "刘", "杨", "黄", "周", "吴"]
GIVEN_NAMES = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "艳", "勇", "杰", "娟", "涛"]
CHANNELS = ["app", "web", "miniapp", "offline"]
STATUSES = ["paid", "refunded", "cancelled"]
STATUS_WEIGHTS = [0.82, 0.10, 0.08]
ORDERS_PER_MONTH = 160

SCHEMA_SQL = """
CREATE TABLE customers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    city    TEXT NOT NULL,
    segment TEXT NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    order_no    TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date  TEXT NOT NULL,
    paid_at     TEXT,
    status      TEXT NOT NULL,
    amount      REAL NOT NULL,
    tax_amount  REAL,
    channel     TEXT NOT NULL
);

CREATE INDEX idx_orders_order_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
"""


def _shift_month(day: date, delta: int) -> date:
    """返回 day 所在月份偏移 delta 个月后的当月 1 日。"""
    total = day.year * 12 + (day.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def _last_complete_months(today: date, count: int) -> list[tuple[date, date]]:
    """返回最近 count 个完整自然月的 (start, end) 列表。"""
    first_this_month = today.replace(day=1)
    months: list[tuple[date, date]] = []
    for offset in range(count, 0, -1):
        start = _shift_month(first_this_month, -offset)
        end = _shift_month(start, 1) - timedelta(days=1)
        months.append((start, end))
    return months


def _build_customers(rng: random.Random, size: int = 40) -> list[tuple]:
    """按固定随机种子生成 customers 行。"""
    rows = []
    for cid in range(1, size + 1):
        name = f"{rng.choice(SURNAMES)}{rng.choice(GIVEN_NAMES)}"
        rows.append((cid, name, rng.choice(CITIES), rng.choices(SEGMENTS, weights=[0.3, 0.5, 0.2])[0]))
    return rows


def _build_orders(rng: random.Random, months: list[tuple[date, date]], customer_count: int) -> list[tuple]:
    """按月份生成 orders 行，日期、状态和金额可复现。"""
    rows = []
    next_id = 1
    for start, end in months:
        days = (end - start).days + 1
        for _ in range(ORDERS_PER_MONTH):
            order_day = start + timedelta(days=rng.randrange(days))
            status = rng.choices(STATUSES, weights=STATUS_WEIGHTS)[0]
            amount = round(rng.uniform(80, 6000), 2)
            tax_amount = round(amount - amount / 1.13, 2) if status != "cancelled" else None
            paid_at = None
            if status != "cancelled":
                paid_at = datetime.combine(
                    order_day, datetime.min.time()
                ) + timedelta(hours=rng.randrange(8, 23), minutes=rng.randrange(60))
                paid_at = paid_at.strftime("%Y-%m-%d %H:%M:%S")
            rows.append(
                (
                    next_id,
                    f"ORD-{next_id:06d}",
                    rng.randint(1, customer_count),
                    order_day.isoformat(),
                    paid_at,
                    status,
                    amount,
                    tax_amount,
                    rng.choices(CHANNELS, weights=[0.4, 0.3, 0.2, 0.1])[0],
                )
            )
            next_id += 1
    return rows


def init_db(db_path: Path, force: bool = False, today: date | None = None) -> dict:
    """创建演示库并灌入样例数据，返回行数与月份摘要。"""
    if db_path.exists() and not force:
        raise FileExistsError(f"{db_path} 已存在；如需重建请加 --force")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    rng = random.Random(SEED)
    today = today or date.today()
    months = _last_complete_months(today, 6)
    customers = _build_customers(rng)
    orders = _build_orders(rng, months, len(customers))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", customers)
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", orders
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "db_path": str(db_path),
        "customers": len(customers),
        "orders": len(orders),
        "months": [(s.isoformat(), e.isoformat()) for s, e in months],
    }


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数、生成演示库并打印摘要。"""
    parser = argparse.ArgumentParser(description="生成 v0 演示库")
    parser.add_argument(
        "--db",
        default=os.environ.get("DATA_AGENT_DB_PATH") or str(DEFAULT_DB_PATH),
        help="输出路径（默认 %(default)s）",
    )
    parser.add_argument("--force", action="store_true", help="已存在时覆盖重建")
    args = parser.parse_args(argv)

    try:
        summary = init_db(Path(args.db).expanduser(), force=args.force)
    except FileExistsError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    last_start, last_end = summary["months"][-1]
    print(f"已生成 {summary['db_path']}")
    print(f"  customers: {summary['customers']} 行")
    print(f"  orders:    {summary['orders']} 行（{summary['months'][0][0]} ~ {last_end}）")
    print(f"  最近完整月: {last_start} ~ {last_end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
