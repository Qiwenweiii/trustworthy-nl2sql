"""一次问数的编排：问题 -> 语义层拒答检查 -> 生成 SQL -> 指标必需条件强制注入 -> 时间边界校验 -> 静态校验 -> 只读执行 ->（执行报错时）报错回炉一次。

本文件只负责"按顺序调用、该收尾就收尾"；每一步怎么写 outcome、怎么打 debug 痕迹，
在 pipeline_steps.py 里一函数一步。除拒答检查那一段（检查通过/拒答的岔路在 graph/
里用 LangGraph 装配）以外，各步都是显式入参、返回结构化结果的普通函数。
outcome 的字段是评测契约，见 README。
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from .pipeline_steps import (
    enforce_metrics,
    execute,
    fix_time,
    make_tracer,
    new_outcome,
    resolve_sql,
    retry_after_error,
    run_guard,
    run_refusal_gate,
    stamp,
)
from .services.llm_client import SqlGenerator

STATUSES = ("ok", "blocked", "no_sql", "exec_error", "timeout", "llm_error")


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
    后续指标条件注入、静态校验与只读执行照常进行（注入不绕过 guard）。
    问题在调模型之前先过语义层拒答检查：指标或维度不在定义表里（撑不住的问题）
    直接返回 status=no_sql、sql=None，拒答文案进 llm_response、缺口类型进
    error["type"]="semantic_refusal"，模型一次都不调；injected_sql 走复放通道，
    问题层检查跳过（那条通道的问题文本只是标签），SQL 层检查不受影响。
    模型产出的 SQL 在进入校验前先过语义层：指标定义要求的必需条件（订单范围）
    缺失会被代码补上、冲突会被改写，处理痕迹记在 outcome["enforcement"]；
    时间边界按库里字段的真实类型校验，DATETIME 字段的闭区间上界改成半开区间，
    痕迹记在 outcome["time_fix"]（只改边界写法，不改模型选的时间字段）。
    执行报数据库错误（exec_error）时回炉一次：失败 SQL 与报错原文拼进修复提示词
    再问模型，修好的 SQL 重走注入/时间修正/guard/执行全套安检；最多一次，
    timeout 与 injected_sql 复放通道不回炉。现场记在 outcome["retry"]。
    debug 为 True 时把每一步的输入/输出各打成一行 stderr，不影响返回值与业务逻辑。
    """
    started = time.monotonic()
    outcome = new_outcome(question)
    trace = make_tracer(debug)
    trace("question", question)

    # 语义层拒答检查：拒答发生在模型调用之前，拒了就到此为止（详见 pipeline_steps 各步函数）
    if run_refusal_gate(question, outcome, trace, injected_sql=injected_sql):
        return stamp(outcome, started)

    # 取 SQL：注入通道或 schema+模型；llm_error / no_sql 在这一步内收好状态
    sql = resolve_sql(
        question,
        outcome,
        trace,
        generator=generator,
        db_path=db_path,
        today=today,
        injected_sql=injected_sql,
    )
    if sql is None:
        return stamp(outcome, started)

    # 语义层两步：订单范围强制注入、时间边界校验（注入不享有绕过校验的特权）
    sql = enforce_metrics(question, sql, outcome, trace)
    sql = fix_time(sql, db_path, outcome, trace)

    # 静态校验：拦截即 blocked
    guarded = run_guard(sql, outcome, trace)
    if guarded is None:
        return stamp(outcome, started)

    # 只读执行 + 空结果标记
    execute(db_path, guarded, outcome, trace, max_rows=max_rows, timeout_s=query_timeout_s)

    # 报错回炉（阶段 1）：执行报错时把报错原文带回给模型修一次。
    # timeout 不回炉（重试大概率再慢一次）、injected_sql 复放通道没有模型可问，也不回炉。
    if outcome["status"] == "exec_error" and injected_sql is None and generator is not None:
        retry_after_error(
            question,
            outcome,
            trace,
            generator=generator,
            db_path=db_path,
            today=today,
            failed_sql=guarded,
            db_error=outcome["error"]["message"],
            max_rows=max_rows,
            timeout_s=query_timeout_s,
        )
    return stamp(outcome, started)
