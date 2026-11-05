"""语义层第二步 · 小步一：时间边界校验。

K1 第 3 次失败的病根不是"时间字段选错"，是 DATETIME 字段套了 DATE 的闭区间写法：
`paid_at BETWEEN '2026-09-01' AND '2026-09-30'`——paid_at 带时分，9 月 30 日当天
00:00 之后支付的订单被整段漏掉（演示库上少 11214.73）。

本模块**只修边界写法，不替换时间字段**：用户点名 paid_at，改写后仍然是 paid_at，
只是把闭区间上界换成"< 次日"的半开区间。字段类型以库里的事实为准：声明类型是
DATETIME/TIMESTAMP 直接认定；SQLite 常见的 TEXT 声明就抽样看真实取值有没有时分。
判不出来（列找不到、库读不到、取值形态不认识）一律不动、不报错；边界本来就写对
（DATE 字段、上界已带时分、`DATE(列)` 包了一层）也不动。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from .sql_text import SqlScanError, in_spans, is_bare_column, protected_spans, referenced_tables

# 抽样条数上限：演示库单月 160 行，够判出取值形态，又不会拖慢链路。
_SAMPLE_LIMIT = 200

_DATETIME_HINTS = ("DATETIME", "TIMESTAMP", "TIME")

_DATE_TEXT = r"\d{4}-\d{2}-\d{2}"
_TIME_TEXT = r"(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
_DATE_ONLY_RE = re.compile(_DATE_TEXT)
# 判取值形态时时间部分必须出现，否则纯日期也会被认成带时分
_WITH_TIME_RE = re.compile(_DATE_TEXT + r"[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?")

# 谓词左边必须是裸列（可带表别名）；函数包裹（DATE(paid_at)）不在射程内。
_COLUMN_HEAD = (
    r"(?<![A-Za-z0-9_.])(?P<alias>[A-Za-z_][A-Za-z0-9_]*\.)?"
    r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)"
)
_BETWEEN_RE = re.compile(
    _COLUMN_HEAD
    + r"\s+between\s+'"
    + r"(?P<lower>" + _DATE_TEXT + r")"
    + r"(?P<lower_time>" + _TIME_TEXT + r")'"
    + r"\s+and\s+'"
    + r"(?P<upper>" + _DATE_TEXT + r")"
    + r"(?P<upper_time>" + _TIME_TEXT + r")'",
    re.IGNORECASE,
)
_LTE_RE = re.compile(
    _COLUMN_HEAD
    + r"\s*<=\s*'"
    + r"(?P<upper>" + _DATE_TEXT + r")"
    + r"(?P<upper_time>" + _TIME_TEXT + r")'"
)
# 谓词左边的裸列判定、FROM/JOIN 别名解析、保护区间判断都在 sql_text（二期 2c 下沉），
# 本模块只留"时间边界"本身的规则。


def fix_time_boundaries(sql: str, *, db_path: Path) -> tuple[str, dict | None]:
    """SQL 生成后、guard 前的时间边界检查：返回 (处理后的 SQL, time_fix 记录或 None)。

    SQL 里没有可识别的时间边界谓词时返回原文和 None（连库都不打开）。
    识别出谓词但字段是 DATE、上界已带时分、或类型判不出来，只记动作不改 SQL。
    库读不到（文件缺失、表不存在、列不存在）一律收敛成 `skipped` 动作，不抛异常。
    """
    if not sql:
        return sql, None
    try:
        predicates = _iter_predicates(sql)
    except SqlScanError as exc:
        # 统一口径（见 sql_text）：引号/注释未闭合=整句读不懂，一个谓词都不改
        return sql, {
            "changed": False,
            "actions": [
                {"type": "skipped", "detail": f"SQL 文本读不懂（{exc}），未做时间边界修正"}
            ],
        }
    if not predicates:
        return sql, None

    record: dict = {"changed": False, "actions": []}
    tables = referenced_tables(sql)
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            rewrites = _plan_rewrites(predicates, tables, conn, record["actions"])
    except sqlite3.Error as exc:
        record["actions"].append(
            {"type": "skipped", "detail": f"读不到库 {db_path}（{exc}），未做时间边界修正"}
        )
        return sql, record

    # 从右往左替换，前面的匹配位置才不会因为长度变化而失效
    for start, end, replacement in sorted(rewrites, key=lambda item: item[0], reverse=True):
        sql = sql[:start] + replacement + sql[end:]
        record["changed"] = True
    return sql, record


def _iter_predicates(sql: str) -> list[dict]:
    """找出 BETWEEN 闭区间与 `<=` 两类上界谓词；字符串/注释里的假谓词、被函数包裹的列都不算。

    protected_spans 扫不出（引号/注释未闭合）时向调用方抛 SqlScanError。
    """
    found: list[dict] = []
    protected = protected_spans(sql)
    for pattern, op in ((_BETWEEN_RE, "between"), (_LTE_RE, "lte")):
        for match in pattern.finditer(sql):
            # 位置按整段左端算：带别名时列名前面是 `o.` 的点，不是谓词起点
            lhs_at = match.start()
            if in_spans(protected, lhs_at) or not is_bare_column(sql, lhs_at):
                continue
            groups = match.groupdict()
            found.append(
                {
                    "op": op,
                    "start": match.start(),
                    "end": match.end(),
                    "text": match.group(0),
                    "alias": groups.get("alias") or "",
                    "column": match.group("column"),
                    "lower": groups.get("lower") or "",
                    "lower_time": groups.get("lower_time") or "",
                    "upper": match.group("upper"),
                    "upper_time": match.group("upper_time") or "",
                }
            )
    return found


def _plan_rewrites(
    predicates: list[dict], tables: list[tuple[str, str | None]], conn, actions: list[dict]
) -> list[tuple[int, int, str]]:
    """逐条谓词决定动不动：要改的返回替换区间，不动的把原因写进 actions。"""
    rewrites: list[tuple[int, int, str]] = []
    for item in predicates:
        column = item["column"]
        kind, reason = _resolve_kind(conn, tables, item["alias"], column)
        if kind == "unknown":
            actions.append(
                {"type": "skipped", "detail": f"时间字段 {column}：{reason}，未修正"}
            )
            continue
        if kind == "date":
            actions.append(
                {
                    "type": "already_correct",
                    "detail": f"{column} 只有日期，闭区间边界不需要改（{reason}）",
                }
            )
            continue
        if item["upper_time"]:
            actions.append(
                {
                    "type": "already_correct",
                    "detail": f"上界 '{item['upper']}{item['upper_time']}' 已带时分，未改写",
                }
            )
            continue
        try:
            exclusive_upper = _next_day(item["upper"])
        except ValueError:
            actions.append(
                {"type": "skipped", "detail": f"上界 '{item['upper']}' 不是合法日期，未改写"}
            )
            continue

        lhs = f"{item['alias']}{column}"
        if item["op"] == "between":
            replacement = (
                f"{lhs} >= '{item['lower']}{item['lower_time']}' AND {lhs} < '{exclusive_upper}'"
            )
        else:
            replacement = f"{lhs} < '{exclusive_upper}'"
        actions.append(
            {
                "type": "boundary_rewritten",
                "from": item["text"],
                "to": replacement,
                "detail": (
                    f"{column} 带时分，闭区间上界 '{item['upper']}' 会漏掉当天 00:00 之后"
                    f"的数据，改写为 {replacement}（{reason}）"
                ),
            }
        )
        rewrites.append((item["start"], item["end"], replacement))
    return rewrites


def _resolve_kind(conn, tables, alias: str, column: str) -> tuple[str, str]:
    """定位谓词列属于哪张表并判日期类型，返回 (date|datetime|unknown, 判定的依据)。"""
    candidates = tables
    if alias:
        wanted = alias.rstrip(".").lower()
        named = [
            (table, table_alias)
            for table, table_alias in candidates
            if (table_alias or table).lower() == wanted or table.lower() == wanted
        ]
        if not named:
            return "unknown", f"SQL 里没有别名为 {wanted} 的表"
        candidates = named

    hits = []
    for table, _alias in candidates:
        declared = _table_columns(conn, table).get(column.lower())
        if declared is not None:
            hits.append((table, declared))
    if not hits:
        return "unknown", f"列 {column} 在 SQL 引用的表里都不存在"
    if len(hits) > 1:
        names = "、".join(table for table, _ in hits)
        return "unknown", f"列 {column} 同时出现在 {names}，无法确定归属"
    return _kind_of(conn, hits[0][0], column, hits[0][1])


def _kind_of(conn, table: str, column: str, declared: str) -> tuple[str, str]:
    """先按声明类型判；声明是文本类型（SQLite 存日期时间的常态）就抽样看取值形态。

    返回的第二个值是"这条结论是怎么来的"，写进动作记录里备查。
    """
    upper = declared.upper()
    if any(hint in upper for hint in _DATETIME_HINTS):
        return "datetime", f"按 {table}.{column} 的声明类型 {declared} 判定"
    if upper.startswith("DATE"):
        return "date", f"按 {table}.{column} 的声明类型 {declared} 判定"

    values = _sample_values(conn, table, column)
    if values is None:
        return "unknown", f"读不到 {table}.{column} 的取值"
    kinds: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            return "unknown", f"{table}.{column} 的取值不是日期文本（样例 {value!r}）"
        text = value.strip()
        if _WITH_TIME_RE.fullmatch(text):
            kinds.add("datetime")
        elif _DATE_ONLY_RE.fullmatch(text):
            kinds.add("date")
        else:
            return "unknown", f"{table}.{column} 的取值形态不认识（样例 {text!r}）"
    if not kinds:
        return "unknown", f"{table}.{column} 没有非空取值，判不出类型"
    if "datetime" in kinds:
        return "datetime", f"抽样 {len(values)} 条 {table}.{column} 取值，带时分"
    return "date", f"抽样 {len(values)} 条 {table}.{column} 取值，只有日期"


def _table_columns(conn, table: str) -> dict[str, str]:
    """列名（小写）到声明类型的映射；表不存在或读不到时为空。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[1]).lower(): str(row[2] or "") for row in rows}


def _sample_values(conn, table: str, column: str) -> list | None:
    """取该列最多 _SAMPLE_LIMIT 个非空取值；读失败返回 None。"""
    query = (
        f"SELECT {_quote(column)} FROM {_quote(table)} "
        f"WHERE {_quote(column)} IS NOT NULL LIMIT {_SAMPLE_LIMIT}"
    )
    try:
        return [row[0] for row in conn.execute(query)]
    except sqlite3.Error:
        return None


def _next_day(day: str) -> str:
    """闭区间上界（含当天）换成半开区间的排他上界（次日 00:00）。"""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _quote(name: str) -> str:
    """标识符加双引号；标识符来自 SQL 文本，逐条转义引号后只用于只读查询。"""
    return '"' + str(name).replace('"', '""') + '"'
