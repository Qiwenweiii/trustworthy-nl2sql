"""SQL 静态校验：只放行单条只读 SELECT，并给出结构化的拦截原因。

violations[].rule 的取值是评测系统依赖的稳定契约，见 README。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "attach",
        "detach",
        "pragma",
        "vacuum",
        "reindex",
        "analyze",
        "trigger",
        "grant",
        "revoke",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "release",
    }
)

# REPLACE 单看是只读标量函数名，只有和 INTO 相邻才构成写语句，所以按词组成对匹配。
WRITE_PHRASES = (("replace", "into"),)

FORBIDDEN_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit"})

ALLOWED_FIRST_KEYWORDS = frozenset({"select", "with"})

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FUNCTION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SELECT_RE = re.compile(r"\bselect\b", re.IGNORECASE)


class _ScanError(Exception):
    """SQL 词法扫描失败，通常是引号或注释未闭合。"""


@dataclass(frozen=True)
class GuardResult:
    """一次静态校验的结果：是否通过、违规明细、规范化后的 SQL。"""

    passed: bool
    violations: list[dict]
    sql: str

    def as_dict(self) -> dict:
        """返回可直接序列化进 outcome 的校验结果。"""
        return {"passed": self.passed, "violations": self.violations}


def _violation(rule: str, detail: str, token: str | None = None) -> dict:
    """构造一条结构化的拦截记录。"""
    item = {"rule": rule, "detail": detail}
    if token is not None:
        item["token"] = token
    return item


def strip_trailing_semicolons(sql: str) -> str:
    """去掉末尾空白和分号；执行器包 LIMIT 时也用同一份规范化结果。"""
    normalized = sql.strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _scan_views(sql: str) -> tuple[str, str]:
    """一次扫描产出两份文本，各自服务于一条规则。

    - 结构视图：注释与字符串字面量遮蔽，引号标识符折成 `ident`。
      用于判断语句数量、首关键字和写关键字——引号里的是列名不是关键字，
      所以 `SELECT "delete" FROM orders` 不该被误杀。
    - 调用视图：同样遮蔽注释和字符串字面量，但引号标识符保留内容。
      用于判断函数调用——`SELECT "load_extension"(...)` 加引号也躲不过。

    两份文本各自内部自洽，检查时不需要在它们之间做位置对应。
    """
    structure: list[str] = []
    calls: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            end = _read_escaped_run(sql, i + 1, "'", scan_error="字符串未闭合")
            structure.append("''")
            calls.append("''")
            i = end
        elif ch in ('"', "`"):
            end, content = _read_quoted(sql, i, ch)
            structure.append("ident")
            calls.append(content)
            i = end
        elif ch == "[":
            end = sql.find("]", i + 1)
            if end == -1:
                raise _ScanError("方括号标识符未闭合")
            structure.append("ident")
            calls.append(sql[i + 1 : end])
            i = end + 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            newline = sql.find("\n", i + 2)
            i = n if newline == -1 else newline + 1
            structure.append(" ")
            calls.append(" ")
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                raise _ScanError("块注释未闭合")
            i = end + 2
            structure.append(" ")
            calls.append(" ")
        else:
            structure.append(ch)
            calls.append(ch)
            i += 1
    return "".join(structure), "".join(calls)


def _read_quoted(sql: str, start: int, quote: str) -> tuple[int, str]:
    """读一段引号标识符，返回 (结束位置, 去掉引号后的内容)。"""
    i = start + 1
    while i < len(sql):
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:  # 连续两个引号是转义
                i += 2
                continue
            content = sql[start + 1 : i].replace(quote * 2, quote)
            return i + 1, content
        i += 1
    raise _ScanError("引号标识符未闭合")


def _read_escaped_run(sql: str, start: int, quote: str, scan_error: str) -> int:
    """读一段字符串字面量，返回结束位置；内容不参与词法判断所以不必取出。"""
    i = start
    while i < len(sql):
        if sql[i] == quote:
            if i + 1 < len(sql) and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise _ScanError(scan_error)


def _keyword_hits(structure: str) -> set[str]:
    """结构视图里命中的写语句关键字。

    紧跟左括号的词算函数调用名，不参与关键字匹配——`replace()` 只做文本替换，
    不写数据；真正的写语句是 `REPLACE INTO`，由 WRITE_PHRASES 成对识别。
    """
    hits: set[str] = set()
    words = list(_WORD_RE.finditer(structure))
    for index, match in enumerate(words):
        word = match.group(0).lower()
        if structure[match.end() :].lstrip().startswith("("):
            continue
        if word in FORBIDDEN_KEYWORDS:
            hits.add(word)
        following = words[index + 1] if index + 1 < len(words) else None
        for first, second in WRITE_PHRASES:
            if (
                word == first
                and following is not None
                and following.group(0).lower() == second
                and structure[match.end() : following.start()].strip() == ""
            ):
                hits.add(f"{first} {second}")
    return hits


def check_sql(sql: str) -> GuardResult:
    """校验 SQL 是否为单条只读 SELECT，返回结构化拦截原因。"""
    raw = (sql or "").strip()
    if not raw:
        return GuardResult(False, [_violation("empty_sql", "模型没有返回可执行的 SQL")], "")

    try:
        structure, calls = _scan_views(raw)
    except _ScanError as exc:
        return GuardResult(
            False, [_violation("malformed_sql", f"SQL 无法解析：{exc}")], raw
        )

    structure = strip_trailing_semicolons(structure)
    violations: list[dict] = []

    if ";" in structure:
        violations.append(
            _violation("multiple_statements", "只允许单条 SQL，检测到语句分隔符 ;")
        )

    match = _WORD_RE.search(structure)
    first_keyword = match.group(0).lower() if match else ""
    if first_keyword not in ALLOWED_FIRST_KEYWORDS:
        violations.append(
            _violation(
                "not_select",
                f"只允许 SELECT 查询，实际首关键字为 {first_keyword or '空'}",
                first_keyword or None,
            )
        )
    elif first_keyword == "with" and not _SELECT_RE.search(structure):
        violations.append(_violation("not_select", "WITH 子句中没有 SELECT"))

    for keyword in sorted(_keyword_hits(structure)):
        violations.append(
            _violation(
                "forbidden_keyword",
                f"不允许出现写操作/管理关键字 {keyword.upper()}",
                keyword.upper(),
            )
        )

    called = {name.lower() for name in _FUNCTION_RE.findall(calls)}
    for function in sorted(called & FORBIDDEN_FUNCTIONS):
        violations.append(
            _violation(
                "forbidden_function",
                f"不允许调用危险函数 {function}()",
                function,
            )
        )

    return GuardResult(not violations, violations, strip_trailing_semicolons(raw))
