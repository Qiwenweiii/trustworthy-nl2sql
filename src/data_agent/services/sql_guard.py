"""SQL 静态校验：只放行单条只读 SELECT，并给出结构化的拦截原因。

violations[].rule 的取值是评测系统依赖的稳定契约，见 README。
文本扫描用 services/sql_text.py 的统一底座（结构/调用双视图）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sql_text import SqlScanError, scan_views

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
        structure, calls = scan_views(raw)
    except SqlScanError as exc:
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
