"""读取 SQLite 表结构，渲染成提示词里的 schema 说明。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

COLUMN_NOTES = {
    "customers.id": "客户ID",
    "customers.name": "客户名称",
    "customers.city": "所在城市",
    "customers.segment": "客户类型：企业/中小企业/个人",
    "orders.id": "订单ID",
    "orders.order_no": "订单号",
    "orders.customer_id": "关联 customers.id",
    "orders.order_date": "下单日期（YYYY-MM-DD）",
    "orders.paid_at": "支付时间，未支付为 NULL",
    "orders.status": "订单状态：paid=已支付, refunded=已退款, cancelled=已取消",
    "orders.amount": "订单金额（元，含税）",
    "orders.tax_amount": "税额（元），已取消订单为 NULL",
    "orders.channel": "下单渠道：app/web/miniapp/offline",
}


def describe_schema(db_path: Path) -> str:
    """读取表结构并渲染成带中文列说明的 schema 文本。"""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if not tables:
            raise RuntimeError(f"数据库 {db_path} 里没有业务表")

        blocks: list[str] = []
        for table in tables:
            lines = [f"表 {table}:"]
            for _cid, name, col_type, _notnull, _default, pk in conn.execute(
                f'PRAGMA table_info("{table}")'
            ):
                flags = " PK" if pk else ""
                note = COLUMN_NOTES.get(f"{table}.{name}", "")
                suffix = f"  -- {note}" if note else ""
                lines.append(f"  - {name} {col_type}{flags}{suffix}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
