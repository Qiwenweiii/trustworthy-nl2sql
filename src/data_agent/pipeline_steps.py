"""ask() 链路每一步的实现：每个函数对应链路一步，写 outcome 的对应字段并打 debug 痕迹。

pipeline.py 只管按顺序调用与提前收尾；本模块的语句是从瘦身前的 pipeline.py
原样搬来的——trace 行格式、outcome 写入、异常口径与先后顺序都不变。
outcome 字段是评测契约，见 README。
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from .graph.builder import run_semantic_gate
from .services.executor import QueryTimeout, is_result_empty, run_query
from .services.llm_client import SqlGenerator
from .services.metrics import enforce_metric_filters
from .services.schema import describe_schema
from .services.sql_guard import check_sql
from .services.time_boundary import fix_time_boundaries

LOGGER = logging.getLogger(__name__)


def new_outcome(question: str) -> dict:
    """outcome 骨架：每一步各自负责填的字段在这里一次摆齐。"""
    return {
        "question": question,
        "status": "ok",
        "sql": None,
        "guard": None,
        "enforcement": None,
        "time_fix": None,
        "result": None,
        "error": None,
        "llm_response": None,
        "caliber_note": None,
        "elapsed_ms": 0.0,
    }


def stamp(outcome: dict, started: float) -> dict:
    """补上端到端耗时并交出 outcome；链路上任何一步提前收尾都走这里。"""
    outcome["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return outcome


def make_tracer(debug: bool):
    """返回 trace(label, value)：debug 开启时把某一步的输入/输出压成一行写到 stderr。"""

    def trace(label: str, value) -> None:
        if not debug:
            return
        text = " ".join(str(value).split())
        print(f"[ask] {label}: {text}", file=sys.stderr, flush=True)

    return trace


def run_refusal_gate(question: str, outcome: dict, trace, *, injected_sql: str | None) -> bool:
    """语义层拒答检查：链路上第一个真正的岔路，只有这一段用 LangGraph。

    返回 True 表示已拒答——outcome 写进 no_sql/拒答文案/缺口类型，pipeline 直接收尾。
    拒答发生在模型调用之前——不调模型、不出 SQL，后面的注入/时间修正/guard/执行都不进。
    injected_sql 是"SQL 由外部给定、问题文本只是标签"的离线复放通道，问题层检查跳过，
    SQL 层的两道语义检查照常（注入不享有绕过校验的特权）。
    """
    if injected_sql is not None:
        trace("refuse", "跳过（injected_sql）")
        return False

    verdict = run_semantic_gate(question)["verdict"]
    if not verdict["allowed"]:
        message = verdict["message"]
        outcome["status"] = "no_sql"
        outcome["llm_response"] = message
        outcome["error"] = {"type": "semantic_refusal", "message": message}
        terms = "、".join(item["term"] for item in verdict["residues"]) or "（无）"
        trace("refuse", f"reason={verdict['reason_code']} terms={terms} metric={verdict['metric']} | {message}")
        trace("outcome", "status=no_sql")
        return True
    dims = "、".join(verdict["matched_dimensions"]) or "（无）"
    trace("refuse", f"通过 metric={verdict['metric']} dims={dims}")
    return False


def resolve_sql(
    question: str,
    outcome: dict,
    trace,
    *,
    generator: SqlGenerator | None,
    db_path: Path,
    today: date | None,
    injected_sql: str | None,
) -> str | None:
    """拿到本次要处理的 SQL：注入通道直接用外部 SQL，否则取 schema 调模型。

    返回 None 表示这一步已经收好了 outcome（llm_error 或 no_sql），pipeline 直接收尾。
    """
    if injected_sql is not None:
        if generator is not None:
            LOGGER.warning("generator ignored due to injected_sql")
        sql = injected_sql
        trace("schema", "跳过（injected_sql）")
    else:
        if generator is None:
            raise ValueError("generator 与 injected_sql 必须至少提供一个")
        schema_text = describe_schema(db_path)
        trace("schema", schema_text)
        run_date = today or date.today()
        try:
            generated = generator(question, schema_text, run_date)
        except Exception as exc:  # LLM 网络/鉴权错误统一落成 llm_error
            outcome["status"] = "llm_error"
            outcome["error"] = {"type": "llm_error", "message": str(exc)}
            trace("llm_error", f"{exc} | status={outcome['status']}")
            return None
        outcome["llm_response"] = generated.raw_response
        outcome["caliber_note"] = generated.caliber_note
        sql = generated.sql

    outcome["sql"] = sql
    trace("sql", "无" if sql is None else sql)
    if sql is None:
        outcome["status"] = "no_sql"
        trace("outcome", "status=no_sql")
        return None
    return sql


def enforce_metrics(question: str, sql: str, outcome: dict, trace) -> str:
    """语义层强制注入：所有 SQL（含 injected_sql）都过这道检查，没有分支；

    改写后的 SQL 再进 guard，注入不享有绕过校验的特权。痕迹记 outcome["enforcement"]。
    """
    sql, enforcement = enforce_metric_filters(question, sql)
    outcome["sql"] = sql
    outcome["enforcement"] = enforcement
    if enforcement is None:
        trace("metric", "未匹配指标，不注入")
    else:
        actions = " | ".join(item["detail"] for item in enforcement["actions"])
        trace(
            "metric",
            f"指标={enforcement['metric']} changed={enforcement['changed']} {actions}",
        )
    return sql


def fix_time(sql: str, db_path: Path, outcome: dict, trace) -> str:
    """时间边界校验：DATETIME 字段的闭区间上界会漏掉"月末当天"的数据，只改边界写法，

    不替换模型选的时间字段（用户点名 paid_at，改写后还是 paid_at）。痕迹记 outcome["time_fix"]。
    """
    sql, time_fix = fix_time_boundaries(sql, db_path=db_path)
    outcome["sql"] = sql
    outcome["time_fix"] = time_fix
    if time_fix is None:
        trace("time", "未识别到时间边界，不修正")
    else:
        actions = " | ".join(item["detail"] for item in time_fix["actions"])
        trace("time", f"changed={time_fix['changed']} {actions}")
    return sql


def run_guard(sql: str, outcome: dict, trace) -> str | None:
    """静态校验：结果记 outcome["guard"]；拦截时写 blocked 并返回 None，

    放行时返回 guard 规范化后的 SQL（剥尾部空白与分号）。
    """
    guard = check_sql(sql)
    guard_dict = guard.as_dict()
    outcome["guard"] = guard_dict
    violations = guard_dict["violations"]
    rules = " ".join(f"{item['rule']}({item['detail']})" for item in violations)
    trace("guard", f"passed={guard.passed} violations={len(violations)} {rules}")
    if not guard.passed:
        outcome["status"] = "blocked"
        trace("outcome", "status=blocked")
        return None
    outcome["sql"] = guard.sql
    return guard.sql


def execute(
    db_path: Path,
    sql: str,
    outcome: dict,
    trace,
    *,
    max_rows: int,
    timeout_s: float,
) -> None:
    """只读执行 + 空结果标记：status 按结果落成 ok / timeout / exec_error。"""
    try:
        result = run_query(
            db_path, sql, max_rows=max_rows, timeout_s=timeout_s
        )
    except QueryTimeout as exc:
        outcome["status"] = "timeout"
        outcome["error"] = {"type": "timeout", "message": str(exc)}
        trace("exec", f"timeout | {exc}")
    except sqlite3.Error as exc:
        outcome["status"] = "exec_error"
        outcome["error"] = {"type": "sql_error", "message": str(exc)}
        trace("exec", f"error | {exc}")
    else:
        outcome["status"] = "ok"
        result_dict = result.as_dict()
        outcome["result"] = result_dict
        trace(
            "exec",
            f"rows={result_dict['row_count']} elapsed_ms={result_dict['elapsed_ms']}",
        )
        if is_result_empty(result.rows):
            # 只按本次结果的形状判定，不重跑 SQL 验证；NULL/0 行必须说破，
            # 免得用户把空结果当成真实答案。文案含「没有数据」，与评测用例的断言正则一致
            notice = "当前条件下没有数据"
            raw = outcome["llm_response"]
            outcome["llm_response"] = f"{raw}\n{notice}" if raw else notice
            trace("exec", f"empty_result=True | {notice}")
