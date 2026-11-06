"""阶段 3 M1a：pipeline.ask() 加可选 tracer 入参的判别性测试。

约定：Web 壳要把管线"步到步"的现场直播成 SSE 阶段帧，复用链路里已有的
trace(label, value) 记录点——ask(..., tracer=fn) 时每个既有 trace 调用点
同步回调 fn；不传 tracer 时行为与现状逐字节相同（debug 仍走 stderr）。
这些测试在实现前必须跑红——旧 ask() 没有 tracer 这个关键字参数，直接 TypeError。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from data_agent.pipeline import ask
from data_agent.services.llm_client import GeneratedSql


@pytest.fixture()
def demo_db(tmp_path):
    """与真实库同形的临时演示库。"""
    path = tmp_path / "demo.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                order_date TEXT NOT NULL
            );
            INSERT INTO orders VALUES
                (1, 1, 10.0, 'paid', '2026-09-05'),
                (2, 1, 20.0, 'paid', '2026-09-15'),
                (3, 2, 30.0, 'paid', '2026-09-30');
            """
        )
        conn.commit()
    return path


def _recorder():
    calls: list[tuple[str, str]] = []

    def tracer(label: str, value) -> None:
        # 记录原文：壳层拿到的就是管线各调用点传出的原值
        calls.append((label, str(value)))

    return tracer, calls


def _ask(generator, db_path, question="一共有多少订单", **kwargs):
    return ask(
        question,
        generator=generator,
        db_path=db_path,
        today=date(2026, 10, 30),
        **kwargs,
    )


def test_tracer_kwarg_is_accepted(demo_db):
    """旧实现没有这个参数：仅签名扩展就足以让本文件先红。"""
    tracer, calls = _recorder()
    outcome = _ask(
        lambda q, s, t: GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x"),
        demo_db,
        tracer=tracer,
    )
    assert outcome["status"] == "ok"
    assert calls, "tracer 至少要收到回调"


def test_tracer_sees_every_stage_in_order(demo_db):
    """ok 全链路：question→sql→metric→guard→exec 的回调顺序与链路顺序一致。"""
    tracer, calls = _recorder()
    _ask(
        lambda q, s, t: GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x"),
        demo_db,
        tracer=tracer,
    )
    labels = [label for label, _ in calls]
    assert "question" in labels
    ordered = [k for k in ("question", "sql", "metric", "guard", "exec") if k in labels]
    assert ordered == ["question", "sql", "metric", "guard", "exec"]


def _strip_timing(obj: dict) -> dict:
    """剥掉天然每次不同的耗时字段（顶层与 result 内各一处）。"""
    copy = {k: v for k, v in obj.items() if k != "elapsed_ms"}
    if isinstance(copy.get("result"), dict):
        copy["result"] = {k: v for k, v in copy["result"].items() if k != "elapsed_ms"}
    return copy


def test_tracer_value_format_matches_debug(demo_db, capsys):
    """直播与成绩单同源：tracer 的调用点序列与 debug stderr 逐条对得上。
    （值口径允许差一个"折叠空白"——debug 打一行必然折叠，tracer 拿原文。）"""
    tracer, calls = _recorder()
    gen = lambda q, s, t: GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x")
    _ask(gen, demo_db, tracer=tracer)
    _ask(gen, demo_db, debug=True)
    stderr_lines = [
        line.removeprefix("[ask] ").split(": ", 1)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("[ask] ")
    ]
    assert len(stderr_lines) == len(calls) >= 5

    def collapse(text: str) -> str:
        # elapsed_ms 是实测数字，每次必不同，键值一起剥掉后不参与同源比较
        return " ".join(re.sub(r"elapsed_ms=\S+", "", text).split())

    for (d_label, d_value), (t_label, t_value) in zip(stderr_lines, calls):
        assert d_label == t_label
        assert collapse(d_value) == collapse(t_value)


def test_refusal_path_traces_without_model_call(demo_db):
    """拒答秒回档：不传 generator 也照样跑，tracer 看到 refuse 且模型一次没调。"""
    tracer, calls = _recorder()
    outcome = ask(
        "库存SKU有多少",
        db_path=demo_db,
        today=date(2026, 10, 30),
        tracer=tracer,
    )
    assert outcome["status"] == "no_sql"
    assert any(label == "refuse" for label, _ in calls)


def test_retry_path_traces_repair(demo_db):
    """回炉档：tracer 收到 retry 现场，failed_sql 与 db_error 都在值里。"""

    def gen(question, schema_text, today):
        if "nope" in question or "报错" in question:
            return GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x")
        return GeneratedSql(sql="SELECT nope FROM orders", raw_response="x")

    tracer, calls = _recorder()
    outcome = _ask(gen, demo_db, tracer=tracer)
    assert outcome["status"] == "ok"
    retry_events = [value for label, value in calls if label == "retry"]
    assert retry_events, "回炉必须直播出来"
    assert any("no such column" in value for value in retry_events)


def test_outcome_unchanged_by_tracer(demo_db):
    """tracer 只直播不改账：带 tracer 与不带的 outcome 除 elapsed_ms 外逐字段相等。"""
    gen = lambda q, s, t: GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x")
    tracer, _ = _recorder()
    with_tracer = _ask(gen, demo_db, tracer=tracer)
    plain = _ask(gen, demo_db)
    assert _strip_timing(with_tracer) == _strip_timing(plain)


def test_without_tracer_debug_behavior_unchanged(demo_db, capsys):
    """不传 tracer 的路径不受影响：debug=True 仍旧打 stderr，默认仍旧一声不吭。"""
    gen = lambda q, s, t: GeneratedSql(sql="SELECT COUNT(*) AS n FROM orders", raw_response="x")
    _ask(gen, demo_db)
    assert capsys.readouterr().err == ""
    _ask(gen, demo_db, debug=True)
    assert "[ask] sql:" in capsys.readouterr().err
