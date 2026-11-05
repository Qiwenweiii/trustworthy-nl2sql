"""语义层第三步：指标/维度存在性检查——撑不住的问题就拒答，不要硬编 SQL。

R1/R3 的教训是"没定义也能算"不可靠：表里没有区域字段，模型就自己编一串城市名做
CASE 映射；指标里没有"新客"，模型就用首单日期现造一个口径。这两种答案看着像真的，
其实是猜的。本模块在生成 SQL 之前先判一次：问题里的每个业务词，能不能被登记好的
指标定义或维度定义解释掉；解释不掉的残片就是缺口，直接拒答并说清缺哪个。

判据是"反向覆盖"而不是"正向匹配"——R3「上个月新客销售额」能正常匹配到指标「销售额」，
只看"有没有匹配到指标"永远抓不到它。缺口藏在没被词表涂到的残片里。

三条放行规则（宁可放过能答的问题，不误杀）：
1. 残片是某个已登记维度的取值（app / 已支付 / 小程序）——取值合不合法属于下一步的
   "维度取值校验"，本步不拦；
2. 残片紧邻已登记维度词（「抖音渠道」里的"抖音"）——当取值看待；
3. 整段残片都长得像时间表达（每个字都在时间线索字符集里，如"前三个月""过去半年"）——
   时间正则没穷举到，但拦下来只会误杀能答的问题，交给模型。

判定只读问题文本和两份静态登记表，不调模型、不连数据库，可以单独测试。
"""

from __future__ import annotations

import re

# 登记表与词表都在 semantic_tables.py；本模块只放判定逻辑。
from ..semantic_tables import (
    CALIBER_ALIASES,
    DIMENSIONS,
    DIMENSION_HINT_WORDS,
    ENTITY_NOUNS,
    GRAMMAR_WORDS,
    METRICS,
    SKIPPABLE_CHARS as _SKIPPABLE_CHARS,
    TIME_HINT_CHARS,
    TIME_PATTERNS,
    TIME_PHRASES,
)
from .metrics import match_metric_in  # 指标匹配唯一实现（二期 2b 去重），拒答与注入共用


def _iter_alias_spans(text: str, words) -> list[tuple[int, int, str]]:
    """按词表在问题里定位所有出现位置（同一位置多次命中取最长词）。"""
    spans: list[tuple[int, int, str]] = []
    for word in sorted(set(words), key=len, reverse=True):
        if not word:
            continue
        for match in re.finditer(re.escape(word), text, re.IGNORECASE):
            spans.append((match.start(), match.end(), word))
    return spans


def _metric_aliases(metrics: dict) -> list[str]:
    words: list[str] = []
    for metric in metrics.values():
        words.extend([metric["name"], *metric.get("synonyms", [])])
    return words


def _dimension_words(dimensions: dict) -> list[str]:
    words: list[str] = []
    for dim in dimensions.values():
        words.extend([dim["name"], *dim.get("aliases", [])])
    return words


def _all_enum_values(dimensions: dict) -> set[str]:
    return {str(value).lower() for dim in dimensions.values() for value in dim.get("enum", [])}


def _covered(text: str, spans) -> list[bool]:
    flags = [False] * len(text)
    for start, end, _label in spans:
        for i in range(start, end):
            flags[i] = True
    return flags


def _residue_runs(text: str, covered: list[bool]) -> list[tuple[int, int, str]]:
    """把没被涂到、又不是标点空白的连续片段切出来。"""
    runs: list[tuple[int, int, str]] = []
    start = None
    for i, ch in enumerate(text):
        keep = not covered[i] and ch not in _SKIPPABLE_CHARS
        if keep and start is None:
            start = i
        elif not keep and start is not None:
            runs.append((start, i, text[start:i]))
            start = None
    if start is not None:
        runs.append((start, len(text), text[start:]))
    return runs


def _gap_is_trivial(text: str, lo: int, hi: int) -> bool:
    """两段之间的间隙只有空白/标点时算"紧邻"。"""
    return all(ch in _SKIPPABLE_CHARS for ch in text[lo:hi])


def _touches_dimension(text: str, run: tuple[int, int, str], dim_spans) -> bool:
    start, end, _ = run
    for d_start, d_end, _label in dim_spans:
        if d_end <= start and _gap_is_trivial(text, d_end, start):
            return True
        if d_start >= end and _gap_is_trivial(text, end, d_start):
            return True
    return False


def supported_dimensions(metric_name: str | None, dimensions: dict | None = None) -> list[str]:
    """某指标当前支持的维度清单——从 DIMENSIONS 单向反查，不在指标侧另存一份。"""
    dimensions = DIMENSIONS if dimensions is None else dimensions
    names: list[str] = []
    for dim in dimensions.values():
        applies = dim.get("applies_to", "*")
        if applies == "*" or (metric_name and metric_name in applies):
            names.append(dim["name"])
    return names


def _defined_metric_names(metrics: dict) -> str:
    return "、".join(metric["name"] for metric in metrics.values())


def _dim_list_text(metric_name: str | None, dimensions: dict) -> str:
    return "、".join(supported_dimensions(metric_name, dimensions)) or "（暂无登记维度）"


def _build_message(code: str, verdict: dict, metrics: dict, dimensions: dict) -> str:
    """拒答文案：点名缺哪个词、是指标还是维度，再列出当前能答的范围。"""
    terms = "".join(f"「{item['term']}」" for item in verdict["residues"]) or "（未登记的说法）"
    dims = _dim_list_text(verdict["metric"], dimensions)
    if code == "empty_question":
        return "无法回答：问题为空，看不出演哪个指标。当前已定义指标：" + _defined_metric_names(metrics) + "。"
    if code == "missing_metric":
        return (
            f"无法回答：{terms}在指标定义里没有登记，算它需要哪个字段、什么公式都没定义，"
            f"系统不替你猜。当前已定义指标：{_defined_metric_names(metrics)}；支持的维度：{dims}。"
        )
    if code == "missing_dimension":
        return (
            f"无法回答：{terms}不是当前支持的维度，维度定义表里没有它对应的字段，"
            f"按它拆分只能靠自造映射。当前「{verdict['metric']}」支持的维度：{dims}。"
        )
    if code == "dimension_not_supported":
        missing = "".join(f"「{name}」" for name in verdict["unsupported_dimensions"])
        return (
            f"无法回答：{missing}维度没有登记为指标「{verdict['metric']}」的可用拆分维度。"
            f"「{verdict['metric']}」当前支持的维度：{dims}。"
        )
    return (
        f"无法回答：{terms}是未登记的限定词，指标「{verdict['metric']}」的定义里没有它的口径"
        f"（怎么判、按哪个字段判都没写），系统不替你发明算法。"
        f"当前已定义指标：{_defined_metric_names(metrics)}；支持的维度：{dims}。"
    )


def check_question(
    question: str,
    *,
    metrics: dict | None = None,
    dimensions: dict | None = None,
) -> dict:
    """判一次问题能否被现有指标/维度定义支撑，返回结构化结论（不调模型、不连库）。

    metrics/dimensions 可注入，用于单测构造"某维度只对某个指标可用"这类登记表；
    不传时用模块级 METRICS / DIMENSIONS。
    """
    metrics = METRICS if metrics is None else metrics
    dimensions = DIMENSIONS if dimensions is None else dimensions
    text = (question or "").strip()

    verdict = {
        "allowed": True,
        "reason_code": None,
        "metric": None,
        "matched_dimensions": [],
        "unsupported_dimensions": [],
        "residues": [],
        "dimension_values": [],
        "time_terms": [],
        "ignored_fragments": [],
        "message": None,
    }
    if not text:
        verdict.update(allowed=False, reason_code="empty_question")
        verdict["message"] = _build_message("empty_question", verdict, metrics, dimensions)
        return verdict

    metric = match_metric_in(text, metrics)
    verdict["metric"] = metric["name"] if metric else None

    vocab_spans = (
        _iter_alias_spans(text, _metric_aliases(metrics))
        + _iter_alias_spans(text, CALIBER_ALIASES)
        + _iter_alias_spans(text, TIME_PHRASES)
        + _iter_alias_spans(text, GRAMMAR_WORDS)
        + _iter_alias_spans(text, ENTITY_NOUNS)
        + _iter_alias_spans(text, _dimension_words(dimensions))
    )
    time_regex_spans = [
        (m.start(), m.end(), m.group(0)) for pattern in TIME_PATTERNS for m in re.finditer(pattern, text, re.IGNORECASE)
    ]
    dim_spans = _iter_alias_spans(text, _dimension_words(dimensions))
    covered = _covered(text, vocab_spans + time_regex_spans)

    matched_dims = [
        dim["name"]
        for dim in dimensions.values()
        if _in(text, [dim["name"], *dim.get("aliases", [])])
    ]
    verdict["matched_dimensions"] = matched_dims
    if metric:
        allowed_dims = set(supported_dimensions(metric["name"], dimensions))
        verdict["unsupported_dimensions"] = [d for d in matched_dims if d not in allowed_dims]

    enum_values = _all_enum_values(dimensions)
    for start, end, term in _residue_runs(text, covered):
        low = term.lower().strip()
        if not low:
            continue
        if len(low) < 2:
            # 单字残片多半是口语尾巴（"怎么样"剩个"样"），拦下来只会误杀能答的问题
            verdict["ignored_fragments"].append(term)
            continue
        if low in enum_values:
            verdict["dimension_values"].append(term)
            continue
        if _touches_dimension(text, (start, end, term), dim_spans):
            verdict["dimension_values"].append(term)
            continue
        if all(ch in TIME_HINT_CHARS for ch in low):
            verdict["time_terms"].append(term)
            continue
        kind = "dimension" if any(hint in term for hint in DIMENSION_HINT_WORDS) else "qualifier"
        verdict["residues"].append({"term": term, "kind": kind})

    residues = verdict["residues"]
    code = None
    if verdict["unsupported_dimensions"]:
        code = "dimension_not_supported"
    elif residues and metric is None:
        code = "missing_metric"
    elif any(item["kind"] == "dimension" for item in residues):
        code = "missing_dimension"
    elif residues:
        code = "unknown_qualifier"

    if code:
        verdict["allowed"] = False
        verdict["reason_code"] = code
        verdict["message"] = _build_message(code, verdict, metrics, dimensions)
    return verdict


def _in(text: str, words) -> bool:
    """词表里是否有任一词出现在文本里（大小写不敏感）。"""
    lowered = text.lower()
    return any(word and word.lower() in lowered for word in words)
