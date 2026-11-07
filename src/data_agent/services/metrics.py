"""语义层第一步：指标定义 + 订单范围强制注入。

K1 的教训是"订单范围靠模型自觉"不可靠：同一个问题有时写 status='paid'、有时不写。
本模块把必需条件变成系统行为——SQL 生成之后、静态校验与执行之前，按指标定义检查：
缺了就补进 WHERE，模型写了冲突取值（如 status='refunded'）就以指标定义为准改写。
每次处理都产出 enforcement 记录进 outcome，评测与调试能看到这条 SQL 被系统补过条件。

处理范围（本步）：只强制"订单范围"这一类条件；时间字段与口径公式在这里只登记，
边界写法由 time_boundary 模块校验（语义层第二步），口径公式仍不校验。
SQL 改写按查询块进行（阶段 4）：必需条件的**判定**只认直接引用指标表那个查询块内的
谓词，补写也只落进那个块；块外（如派生表外层）的错误位置谓词不删不动，交给执行
报错与回炉处理。形态读不懂（括号不配对）、块定位不唯一时宁可什么都不加，
也不做危险的猜测。
"""

from __future__ import annotations

import re

from ..semantic_tables import METRICS
from .sql_text import SqlScanError, in_spans, mask_sql, split_query_blocks

# 谓词位置用掩码文本定位（字符串/注释遮蔽、长度不变），谓词内容从原文切片取。
# 谓词正则直接扫原文：极端情况（字符串字面量里恰好出现 status='xxx'）会误判，
# 演示范围内接受这个代价，不做完整词法解析。扫描判读不懂（引号/注释未闭合）
# 时整条 SQL 不动，只记 skipped——统一口径，见 services/sql_text.py。
_PREDICATE_HEAD = r"(?<![A-Za-z0-9_.])(?P<alias>[A-Za-z_][A-Za-z0-9_]*\.)?{column}\b\s*"
_CLAUSE_KEYWORD_RE = re.compile(
    r"\bwhere\b|\bgroup\s+by\b|\bhaving\b|\border\s+by\b|\blimit\b", re.IGNORECASE
)


def match_metric_in(text: str, metrics: dict) -> dict | None:
    """按指标名/同义词在文本里找指标；多个命中时取最长词，避免"销售额"漏判。

    唯一的指标匹配实现：semantic_check 的拒答判定与本模块的强制注入共用这份判据，
    两处规则不可能再各自漂移。metrics 参数让测试能构造替身登记表。
    """
    lowered = (text or "").lower()
    best: dict | None = None
    best_len = 0
    for metric in metrics.values():
        for term in (metric["name"], *metric.get("synonyms", [])):
            if len(term) > best_len and term.lower() in lowered:
                best, best_len = metric, len(term)
    return best


def match_metric(question: str) -> dict | None:
    """按问题文本在登记表 METRICS 里找指标。"""
    return match_metric_in(question, METRICS)


def enforce_metric_filters(question: str, sql: str) -> tuple[str, dict | None]:
    """SQL 生成后、执行前的强制检查：返回 (处理后的 SQL, enforcement 记录或 None)。

    问题里没匹配到指标时记录为 None、SQL 原样；匹配到指标但 SQL 没引用其数据表时
    不注入，只记一条 skipped。changed=True 表示这条 SQL 被系统改写过。
    """
    if not sql:
        return sql, None
    metric = match_metric(question)
    if metric is None:
        return sql, None

    original_sql = sql
    record = {
        "metric": metric["name"],
        "required_filters": [flt["sql"] for flt in metric["required_filters"]],
        "changed": False,
        "actions": [],
    }
    try:
        if not _references_table(sql, metric["table"]):
            record["actions"].append(
                {"type": "skipped", "detail": f"SQL 未引用 {metric['table']} 表，未注入"}
            )
            return sql, record

        for flt in metric["required_filters"]:
            sql, actions = _ensure_filter(sql, flt, metric["table"])
            record["actions"].extend(actions)
            if any(action["type"] in ("added", "conflict_replaced") for action in actions):
                record["changed"] = True
    except SqlScanError as exc:
        # 判读不懂就不动：还原处理前的 SQL，痕迹只留一条 skipped
        return original_sql, {**record, "changed": False, "actions": [
            {"type": "skipped", "detail": f"SQL 文本读不懂（{exc}），未注入"}
        ]}
    return sql, record


def _ensure_filter(sql: str, flt: dict, table: str) -> tuple[str, list[dict]]:
    """保证单个必需条件在"直接引用指标表的查询块"里成立：缺失补上，冲突改写，已满足不动。

    块外（如派生表外层）的同列谓词一律不计数、不改动——那是模型的产物，
    执行报错后回炉处理（阶段 4 裁决：注入不删模型谓词）。
    """
    targets = [
        block for block in split_query_blocks(sql) if table.lower() in block["tables"]
    ]
    if len(targets) != 1:
        if targets:
            return sql, [
                {
                    "type": "skipped",
                    "detail": f"多个查询块直接引用 {table} 表，条件位置不唯一，未注入 {flt['sql']}",
                }
            ]
        return sql, [
            {
                "type": "skipped",
                "detail": f"未能把 {table} 表引用归位到查询块，未注入 {flt['sql']}",
            }
        ]
    block = targets[0]

    predicates = [
        item
        for item in _iter_predicates(sql, flt)
        if in_spans(block["own_spans"], item["start"])
    ]
    if not predicates:
        new_sql = _append_where_condition(sql, flt["sql"], block)
        if new_sql is None:
            return sql, [
                {"type": "skipped", "detail": f"无法定位 WHERE 插入点，未注入 {flt['sql']}"}
            ]
        detail = f"缺少必需条件，系统补入 {flt['sql']}"
        if not block["is_top"]:
            detail += f"（补进直接查询 {table} 表的子查询块内）"
        return new_sql, [{"type": "added", "to": flt["sql"], "detail": detail}]

    unsatisfied = [item for item in predicates if not item["satisfied"]]
    if not unsatisfied:
        return sql, [
            {"type": "already_present", "to": flt["sql"], "detail": f"已含 {flt['sql']}，未改写"}
        ]

    actions = []
    # 从右往左替换，前面的匹配位置才不会因为长度变化而失效
    for item in sorted(unsatisfied, key=lambda x: x["start"], reverse=True):
        replacement = f"{item['alias'] or ''}{flt['column']} = '{flt['value']}'"
        sql = sql[: item["start"]] + replacement + sql[item["end"] :]
        actions.append(
            {
                "type": "conflict_replaced",
                "from": item["text"],
                "to": replacement,
                "detail": f"模型写的 {item['text']} 与指标定义冲突，以定义为准改写为 {replacement}",
            }
        )
    return sql, actions


def _iter_predicates(sql: str, flt: dict):
    """找出 SQL 里对必需列的全部比较谓词（= / <> / != / IN / NOT IN），并判是否等于必需值。

    <>、!=、NOT IN 是排除式写法，与"只算某个状态"的定义不是同一个口径，一律按冲突改写。
    """
    column = flt["column"]
    pattern = re.compile(
        _PREDICATE_HEAD.format(column=re.escape(column))
        + r"(?:=\s*'(?P<eq>[^']*)'"
        + r"|(?:<>|!=)\s*'(?P<neq>[^']*)'"
        + r"|not\s+in\s*\((?P<notin>[^)]*)\)"
        + r"|in\s*\((?P<in>[^)]*)\))",
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        groups = match.groupdict()
        if groups["eq"] is not None:
            satisfied = groups["eq"] == flt["value"]
        elif groups["in"] is not None:
            values = set(re.findall(r"'([^']*)'", groups["in"]))
            satisfied = values == {flt["value"]}
        else:
            satisfied = False
        yield {
            "start": match.start(),
            "end": match.end(),
            "text": match.group(0),
            "alias": groups.get("alias"),
            "satisfied": satisfied,
        }


def _references_table(sql: str, table: str) -> bool:
    """SQL 是否引用了该表（FROM/JOIN 之后直接出现表名，允许 schema.表 前缀）。"""
    masked = mask_sql(sql)
    return bool(
        re.search(rf"\b(?:from|join)\s+(?:\w+\.)?{re.escape(table)}\b", masked, re.IGNORECASE)
    )


def _token_depths(masked: str) -> list[int]:
    """逐字符记录括号深度（该字符之前的深度），用于只在顶层定位子句关键字。"""
    depths = []
    depth = 0
    for ch in masked:
        depths.append(depth)
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
    return depths


def _append_where_condition(sql: str, fragment: str, block: dict) -> str | None:
    """把条件并进指定查询块自己的 WHERE；块内没有 WHERE 就在其 GROUP BY/HAVING/
    ORDER BY/LIMIT 之前（或块尾）插入。

    返回 None 表示找不到安全的插入点（如 WHERE 后接的是括号残片），调用方应保持原样。
    """
    masked = mask_sql(sql)
    depths = _token_depths(masked)
    tokens = [
        (m.start(), m.end(), m.group(0))
        for m in _CLAUSE_KEYWORD_RE.finditer(masked)
        if depths[m.start()] == block["depth"] and in_spans(block["own_spans"], m.start())
    ]

    where_pos = next((t for t in tokens if re.fullmatch(r"(?i)where", t[2])), None)
    if where_pos:
        clause_end = next(
            (t[0] for t in tokens if t[0] > where_pos[1]),
            block["end"],
        )
        expr = sql[where_pos[1] : clause_end].strip()
        if clause_end == len(sql):
            # 语句结尾的分号可以去尾（guard 本来就会剥）；
            # 句子中间的分号必须留着，让 guard 照常报 multiple_statements
            while expr.endswith(";"):
                expr = expr[:-1].rstrip()
        if not expr:
            return None
        # 原条件整体加括号再 AND，防止 a OR b 被 AND 抢优先级
        rest = sql[clause_end : block["end"]].strip()
        joiner = " " if rest else ""
        head = f"{sql[: where_pos[0]]}WHERE ({expr}) AND {fragment}"
        return f"{head}{joiner}{rest}{sql[block['end'] :]}"

    insert_at = min(
        (t[0] for t in tokens if block["start"] <= t[0] < block["end"] and t[0] > block["start"]),
        default=block["end"],
    )
    head = sql[block["start"] : insert_at].rstrip()
    tail = sql[insert_at : block["end"]].strip()
    if not tail:
        while head.endswith(";"):
            head = head[:-1].rstrip()
    if not head:
        return None
    joined = f"{head} WHERE {fragment}"
    segment = f"{joined} {tail}" if tail else joined
    return f"{sql[: block['start']]}{segment}{sql[block['end'] :]}"
