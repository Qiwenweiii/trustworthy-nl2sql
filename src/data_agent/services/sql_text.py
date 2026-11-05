"""SQL 文本扫描统一底座：认出字符串、引号标识符、方括号标识符与两种注释。

合并前 metrics / time_boundary / sql_guard 各养着一份扫描循环，只在"未闭合
引号/注释怎么处置"上口径不一（不遮 / 整段保护 / 抛错）。二期 2a 收成一份：
扫描分段（_scan_segments），三个消费方投影各取所需——

- mask_sql：等长空格遮罩，谓词/子句定位只看"真代码"（metrics 用）
- protected_spans：受保护区间清单，区间起点的假谓词一律跳过（time_boundary 用）
- scan_views：结构/调用两份折叠视图，位置不与原文对应（sql_guard 用）

统一口径（用户 2026-11-05 确认）：未闭合引号/标识符/注释 = 整句判读不懂，
抛 SqlScanError，消费方接到即"不动 SQL"——metrics 记 skipped 不注入、
time_boundary 记 skipped 不修正、guard 收敛成 malformed_sql 拦下。
"""

from __future__ import annotations

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
