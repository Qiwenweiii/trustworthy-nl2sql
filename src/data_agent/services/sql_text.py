"""SQL 文本扫描统一底座：认出字符串、引号标识符、方括号标识符与两种注释。

合并前 metrics / time_boundary / sql_guard 各养着一份扫描循环，只在"未闭合
引号/注释怎么处置"上口径不一（不遮 / 整段保护 / 抛错）。二期 2a 收成一份：
扫描分段（_scan_segments），三个消费方投影各取所需——

- mask_sql：等长空格遮罩，谓词/子句定位只看"真代码"（metrics 用）
- protected_spans：受保护区间清单，区间起点的假谓词一律跳过（time_boundary 用）
- scan_views：结构/调用两份折叠视图，位置不与原文对应（sql_guard 用）

另备一组与具体规则无关的文本工具（二期 2c 从 time_boundary 下沉）：
`in_spans`、`is_bare_column`（函数包裹的列不算裸列）、`referenced_tables`
（FROM/JOIN 后的表名与别名清单）。

统一口径（用户 2026-11-05 确认）：未闭合引号/标识符/注释 = 整句判读不懂，
抛 SqlScanError，消费方接到即"不动 SQL"——metrics 记 skipped 不注入、
time_boundary 记 skipped 不修正、guard 收敛成 malformed_sql 拦下。
"""

from __future__ import annotations

import re

# 分段类型
_STRING = "string"  # '...' 字符串字面量
_QUOTED_IDENT = "quoted_ident"  # "..." / `...` 引号标识符
_BRACKET_IDENT = "bracket_ident"  # [ ... ] 方括号标识符
_LINE_COMMENT = "line_comment"  # -- 到行尾（不含换行）
_BLOCK_COMMENT = "block_comment"  # /* ... */

_WHITESPACE = " \t\n\r"


class SqlScanError(ValueError):
    """SQL 文本读不懂：引号、方括号或块注释未闭合。"""


def _read_quoted_run(sql: str, start: int, quote: str, error: str) -> tuple[int, str]:
    """读一段引号包裹的文本（连续两个引号是转义），返回 (结束位置, 去转义内容)。"""
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            content = sql[start + 1 : i].replace(quote * 2, quote)
            return i + 1, content
        i += 1
    raise SqlScanError(error)


def _scan_segments(sql: str) -> list[tuple[str, int, int, str]]:
    """把 SQL 扫成 (类型, 起点, 终点, 内容) 分段；内容只对标识符有意义。

    未闭合即抛 SqlScanError——三份旧实现里最保守的那条口径，判读不懂就不动。
    """
    segments: list[tuple[str, int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j, _content = _read_quoted_run(sql, i, "'", "字符串未闭合")
            segments.append((_STRING, i, j, ""))
            i = j
        elif ch in ('"', "`"):
            j, content = _read_quoted_run(sql, i, ch, "引号标识符未闭合")
            segments.append((_QUOTED_IDENT, i, j, content))
            i = j
        elif ch == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise SqlScanError("方括号标识符未闭合")
            segments.append((_BRACKET_IDENT, i, j + 1, sql[i + 1 : j]))
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            segments.append((_LINE_COMMENT, i, j, ""))
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise SqlScanError("块注释未闭合")
            segments.append((_BLOCK_COMMENT, i, j + 2, ""))
            i = j + 2
        else:
            i += 1
    return segments


def mask_sql(sql: str) -> str:
    """把分段覆盖的位置遮成空格，长度与原文一一对应；引号内的空白原样保留。

    旧 metrics 口径：字符串/引号标识符内部的空白字符不遮（保住串内换行的形态），
    方括号与注释整段铺平。
    """
    out = list(sql)
    for kind, start, end, _content in _scan_segments(sql):
        keep_whitespace = kind in (_STRING, _QUOTED_IDENT)
        for k in range(start, end):
            if keep_whitespace and sql[k] in _WHITESPACE:
                continue
            out[k] = " "
    return "".join(out)


def protected_spans(sql: str) -> list[tuple[int, int]]:
    """字符串/标识符/注释的 [起点, 终点) 区间清单：起点落在区间里的谓词是假谓词。"""
    return [(start, end) for _kind, start, end, _content in _scan_segments(sql)]


def scan_views(sql: str) -> tuple[str, str]:
    """一次扫描产出两份折叠视图（旧 sql_guard 口径，各自内部自洽、不保长度）：

    - 结构视图：字符串折成 `''`、标识符折成 `ident`、注释折成空格——引号里的
      是列名不是关键字，`SELECT "delete"` 不该被误杀。
    - 调用视图：同样去掉字符串内容和注释，但引号/方括号标识符**保留内容**——
      `SELECT "load_extension"(...)` 加引号也躲不过函数检查。
    行注释按旧 guard 口径把结尾换行一起吃掉（换行本来就不是词法边界）。
    """
    structure: list[str] = []
    calls: list[str] = []
    pos, n = 0, len(sql)
    for kind, start, end, content in _scan_segments(sql):
        structure.append(sql[pos:start])
        calls.append(sql[pos:start])
        if kind == _STRING:
            structure.append("''")
            calls.append("''")
        elif kind in (_QUOTED_IDENT, _BRACKET_IDENT):
            structure.append("ident")
            calls.append(content)
        else:
            structure.append(" ")
            calls.append(" ")
            if kind == _LINE_COMMENT and end < n and sql[end] == "\n":
                end += 1
        pos = end
    structure.append(sql[pos:])
    calls.append(sql[pos:])
    return "".join(structure), "".join(calls)


# ---------------------------------------------------------------------------
# 谓词定位用的通用文本工具（二期 2c 从 time_boundary 下沉：与"时间边界"无关，
# 任何要做文本级 SQL 手术的模块都用得上）


def in_spans(spans: list[tuple[int, int]], pos: int) -> bool:
    """位置 pos 是否落在某个 [start, end) 区间里。"""
    return any(start <= pos < end for start, end in spans)


# 能出现在分组左括号前面的关键字（`WHERE (col BETWEEN ...)` 里的列仍是裸列）
_GROUPING_WORDS = frozenset(
    {
        "and",
        "by",
        "case",
        "else",
        "exists",
        "from",
        "having",
        "in",
        "is",
        "not",
        "on",
        "or",
        "select",
        "then",
        "when",
        "where",
    }
)


def is_bare_column(sql: str, pos: int) -> bool:
    """列左边只能是谓词起始位置：`DATE(paid_at)` 这类函数包裹的列不算裸列。

    WHERE / AND 这类关键词与列之间隔着空白，不算"更长的标识符"，要放行；
    紧贴列的字母/点（`x.paid_at`、`xpaid_at`）才说明这里不是裸列起点。
    """
    prev = _prev_char(sql, pos)
    if prev is None:
        return True
    index, ch, detached = prev
    if ch == ")":
        return False
    if ch == "(":
        word = _word_before(sql, index)
        return word is None or word.lower() in _GROUPING_WORDS
    return detached or not (ch.isalnum() or ch in "_.")


def _prev_char(sql: str, pos: int) -> tuple[int, str, bool] | None:
    """pos 之前最近的非空白字符，返回 (下标, 字符, 中间是否隔了空白)；到串头返回 None。"""
    i = pos - 1
    detached = False
    while i >= 0 and sql[i] in " \t\r\n":
        i -= 1
        detached = True
    if i < 0:
        return None
    return i, sql[i], detached


def _word_before(text: str, pos: int) -> str | None:
    """取 pos 之前紧邻的那个单词（跨空白）；没有单词时返回 None。"""
    i = pos - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    end = i + 1
    while i >= 0 and (text[i].isalnum() or text[i] == "_"):
        i -= 1
    return text[i + 1 : end] or None


# 表名后面紧跟这些词时不是别名；用负向预查挡掉，才不会把下一个 JOIN 关键字一起吃掉
_NOT_ALIAS = frozenset(
    {
        "and",
        "as",
        "cross",
        "full",
        "group",
        "having",
        "inner",
        "join",
        "left",
        "limit",
        "natural",
        "not",
        "on",
        "or",
        "order",
        "outer",
        "right",
        "select",
        "union",
        "using",
        "values",
        "where",
    }
)
_FROM_JOIN_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?:as\s+)?(?!(?:{})\b)([A-Za-z_][A-Za-z0-9_]*))?".format(
        "|".join(sorted(_NOT_ALIAS))
    ),
    re.IGNORECASE,
)


def referenced_tables(sql: str) -> list[tuple[str, str | None]]:
    """FROM/JOIN 之后的 (表名, 别名) 清单；没写别名时别名为 None。"""
    return [(match.group(1), match.group(2)) for match in _FROM_JOIN_RE.finditer(sql)]


# ---------------------------------------------------------------------------
# 查询块切分（阶段 4：条件注入的块感知底座）

_SELECT_HEAD_RE = re.compile(r"\s*select\b", re.IGNORECASE)
_BLOCK_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+(?:\w+\.)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)


def split_query_blocks(sql: str) -> list[dict]:
    """把 SQL 切成查询块清单（文档序：外层在前、内层在后），位置全部对原文成立。

    块 = 一个 SELECT 作用域：顶层语句一块；每对括号内首个关键字是 SELECT 就再切一块
    （派生表、CTE 支、IN (SELECT ...) 都算）。函数括号、条件括号不是块。
    每块字段：
    - start/end: 块正文区间（顶层为整句；子块不含外层括号）
    - depth: 块内代码的括号深度（顶层 0，一层子查询 1）
    - tables: 本块 FROM/JOIN 直接引用的表名集合（小写，不含子查询别名引用）
    - own_spans: 本块正文扣掉所有子块（含其括号）后的自有区间清单
    - is_top: 是否顶层块
    括号不配对与引号未闭合同口径：抛 SqlScanError，调用方"读不懂不动手"。
    """
    masked = mask_sql(sql)
    n = len(sql)

    depths = [0] * n
    depth = 0
    open_stack: list[int] = []
    close_of: dict[int, int] = {}
    for i, ch in enumerate(masked):
        depths[i] = depth
        if ch == "(":
            open_stack.append(i)
            depth += 1
        elif ch == ")":
            if not open_stack:
                raise SqlScanError("括号不配对：多余的右括号")
            open_pos = open_stack.pop()
            depth -= 1
            close_of[open_pos] = i
    if open_stack:
        raise SqlScanError("括号不配对：未闭合的左括号")

    top = {
        "start": 0,
        "end": n,
        "depth": 0,
        "tables": set(),
        "own_spans": [(0, n)],
        "child_spans": [],
        "is_top": True,
    }
    blocks: list[dict] = [top]

    def _carve(parent: dict, lo: int, hi: int, base_depth: int) -> None:
        """在 parent 的 [lo, hi) 里找查询括号对：登记为子块并递归，非查询括号穿透。"""
        pos = lo
        while pos < hi:
            if masked[pos] != "(":
                pos += 1
                continue
            close = close_of[pos]
            if _SELECT_HEAD_RE.match(masked, pos + 1, close):
                block = {
                    "start": pos + 1,
                    "end": close,
                    "depth": base_depth + 1,
                    "tables": set(),
                    "own_spans": [(pos + 1, close)],
                    "child_spans": [],
                    "is_top": False,
                }
                blocks.append(block)
                parent["child_spans"].append((pos, close + 1))
                _carve(block, pos + 1, close, base_depth + 1)
            else:
                _carve(parent, pos + 1, close, base_depth + 1)
            pos = close + 1

    _carve(top, 0, n, 0)

    for block in blocks:
        block["own_spans"] = _subtract(block["start"], block["end"], block["child_spans"])
        for lo, hi in block["own_spans"]:
            for match in _BLOCK_TABLE_REF_RE.finditer(masked, lo, hi):
                block["tables"].add(match.group(1).lower())
    return blocks


def _subtract(start: int, end: int, holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) 扣掉若干互不重叠的挖空区间，剩下的自有区间清单。"""
    spans = []
    pos = start
    for hole_start, hole_end in sorted(holes):
        if hole_start > pos:
            spans.append((pos, hole_start))
        pos = max(pos, hole_end)
    if pos < end:
        spans.append((pos, end))
    return spans
