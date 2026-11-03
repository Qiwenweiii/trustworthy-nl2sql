"""一次问数的编排：问题 -> 生成 SQL -> 静态校验 -> 只读执行。

每一步都是显式入参、返回结构化结果的普通函数，后续接编排框架时可直接包成节点。
outcome 的字段是评测契约，见 README。
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from .services.executor import QueryTimeout, run_query
from .services.llm_client import SqlGenerator
from .services.schema import describe_schema
from .services.sql_guard import check_sql

STATUSES = ("ok", "blocked", "no_sql", "exec_error", "timeout", "llm_error")

LOGGER = logging.getLogger(__name__)


def ask(
    question: str,
    *,
    generator: SqlGenerator | None = None,
    db_path: Path,
    max_rows: int = 200,
    query_timeout_s: float = 5.0,
    today: date | None = None,
    injected_sql: str | None = None,
    debug: bool = False,
) -> dict:
    """执行一次问数全链路，返回带 status/guard/result 的 outcome 字典。

    today 决定提示词里的基准日期；不传时用当前日期，评测时传入固定日期以可复现。
    injected_sql 非空时跳过取 schema 与模型调用，直接把这条 SQL 当作模型输出，
    后续静态校验与只读执行照常进行（注入不绕过 guard）。
    debug 为 True 时把每一步的输入/输出各打成一行 stderr，不影响返回值与业务逻辑。
    """
    started = time.monotonic()
    outcome = {
        "question": question,
        "status": "ok",
        "sql": None,
        "guard": None,
        "result": None,
        "error": None,
        "llm_response": None,
        "elapsed_ms": 0.0,
    }

    def finish() -> dict:
        """补上端到端耗时并交出 outcome；链路上任何一步提前收尾都走这里。"""
        outcome["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        return outcome

    def trace(label: str, value) -> None:
        """debug 开启时把某一步的输入/输出压成一行写到 stderr。"""
        if not debug:
            return
        text = " ".join(str(value).split())
        print(f"[ask] {label}: {text}", file=sys.stderr, flush=True)

    trace("question", question)

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
            return finish()
        outcome["llm_response"] = generated.raw_response
        sql = generated.sql

    outcome["sql"] = sql
    trace("sql", "无" if sql is None else sql)

    if sql is None:
        outcome["status"] = "no_sql"
        trace("outcome", "status=no_sql")
        return finish()

    guard = check_sql(sql)
    guard_dict = guard.as_dict()
    outcome["guard"] = guard_dict
    violations = guard_dict["violations"]
    rules = " ".join(f"{item['rule']}({item['detail']})" for item in violations)
    trace("guard", f"passed={guard.passed} violations={len(violations)} {rules}")
    if not guard.passed:
        outcome["status"] = "blocked"
        trace("outcome", "status=blocked")
        return finish()

    outcome["sql"] = guard.sql
    try:
        result = run_query(
            db_path, guard.sql, max_rows=max_rows, timeout_s=query_timeout_s
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

    return finish()
