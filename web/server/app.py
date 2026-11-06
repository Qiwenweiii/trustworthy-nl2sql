"""阶段 3 Web 壳：FastAPI 薄壳，把 pipeline.ask 直播成 SSE（WEB_DEMO_PLAN §3）。

壳层只做三件事：入参白名单、并发闸门+日配额、调用与转发。一切数据正确性
在管线内部（guard/enforcement/time_fix/retry 照常运转）；Web 只是第四个调用方。
ask_fn 可注入（契约测试用替身，零模型调用）；不注入时走真实管线，
generator 在每次请求时从 Config 懒构，起服务不强制要求 key。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from data_agent import semantic_tables
from data_agent.config import PROJECT_ROOT, ConfigError, load_config
from data_agent.pipeline import ask as pipeline_ask
from data_agent.services.llm_client import build_generator

from .schemas import AskRequest

DEFAULT_CASES_PATH = PROJECT_ROOT / "problems" / "eval_cases.json"
DEFAULT_WEB_DB_PATH = PROJECT_ROOT / "data" / "web_demo.db"
ERROR_FRAME_MESSAGE = "服务内部错误，请联系演示负责人"
GATE_DETAIL = "演示服务并发已满，请稍后再试"
QUOTA_DETAIL = "今日演示配额已用完，请联系演示负责人"

_UNSET = object()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def select_sample_questions(doc: dict, total: int = 10, refusal_slots: int = 2) -> list[str]:
    """从 golden 题集选推荐问法：只取已毕业 stage、非注入复放的题；
    按题集顺序每类取第一条，类目"拒答"固定占 refusal_slots 个槽位
    （演示"系统会诚实说不知道"），总数不超 total。"""
    done_stages = {
        name
        for name, info in doc.get("meta", {}).get("stages", {}).items()
        if isinstance(info, dict) and info.get("status") == "done"
    }
    graduated = [
        case
        for case in doc.get("cases", [])
        if case.get("stage") in done_stages and not case.get("deterministic")
    ]
    picked: list[str] = []
    refusals_taken = 0
    seen_categories: set[str] = set()
    for case in graduated:
        if len(picked) >= total:
            break
        category = case.get("category")
        if category == "拒答":
            if refusals_taken < refusal_slots:
                refusals_taken += 1
                picked.append(case["question"])
            continue
        if category in seen_categories:
            continue
        seen_categories.add(category)
        picked.append(case["question"])
    return picked


def create_app(
    *,
    ask_fn: Callable[..., dict] | object = _UNSET,
    cases_path: Path | object = _UNSET,
    db_path: Path | object = _UNSET,
    concurrency_limit: int | object = _UNSET,
    daily_quota: int | object = _UNSET,
) -> FastAPI:
    app = FastAPI(title="data-agent demo", docs_url=None, redoc_url=None)

    real_ask = ask_fn is _UNSET
    if real_ask:
        def ask_fn(*, question: str, tracer) -> dict:
            cfg = load_config()
            return pipeline_ask(
                question,
                generator=build_generator(cfg),
                db_path=cfg.db_path,
                tracer=tracer,
            )
    if cases_path is _UNSET:
        cases_path = Path(os.environ.get("DATA_AGENT_CASES_PATH", DEFAULT_CASES_PATH))
    if db_path is _UNSET:
        db_path = Path(os.environ.get("DATA_AGENT_DB_PATH", DEFAULT_WEB_DB_PATH))
    if concurrency_limit is _UNSET:
        concurrency_limit = _int_env("DATA_AGENT_WEB_CONCURRENCY", 2)
    if daily_quota is _UNSET:
        daily_quota = _int_env("DATA_AGENT_WEB_QUOTA", 300)

    gate = threading.Semaphore(int(concurrency_limit))
    quota = {"used": 0}
    quota_lock = threading.Lock()

    def _sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _run_and_emit(question: str, sink: queue.Queue) -> None:
        started = time.monotonic()

        def tracer(label: str, value) -> None:
            sink.put(("stage", {"name": label, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}))

        try:
            outcome = ask_fn(question=question, tracer=tracer)
        except Exception:  # noqa: BLE001 —— 壳层只兜"理论不该发生"，不泄漏堆栈
            sink.put(("error", {"message": ERROR_FRAME_MESSAGE}))
        else:
            sink.put(("result", outcome))

    @app.post("/api/ask")
    async def ask(request: Request):
        try:
            payload = AskRequest.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": json.loads(exc.json())})
        question = payload.question

        with quota_lock:
            if quota["used"] >= int(daily_quota):
                return JSONResponse(status_code=429, content={"detail": QUOTA_DETAIL})
            if not gate.acquire(blocking=False):
                return JSONResponse(status_code=429, content={"detail": GATE_DETAIL})
            quota["used"] += 1

        sink: queue.Queue = queue.Queue()
        worker = threading.Thread(target=_run_and_emit, args=(question, sink), daemon=True)
        worker.start()

        async def stream():
            try:
                while True:
                    item = await asyncio.to_thread(sink.get)
                    event, data = item
                    yield _sse(event, data)
                    if event in ("result", "error"):
                        break
            finally:
                gate.release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/meta")
    def meta():
        metrics = [
            {"name": spec["name"], "synonyms": list(spec.get("synonyms", []))}
            for spec in semantic_tables.METRICS.values()
        ]
        dimensions = []
        for spec in semantic_tables.DIMENSIONS.values():
            entry = {
                "name": spec["name"],
                "aliases": list(spec.get("aliases", [])),
                "column": spec.get("column"),
            }
            if spec.get("enum") is not None:
                entry["enum"] = list(spec["enum"])
            dimensions.append(entry)
        doc = json.loads(Path(cases_path).read_text(encoding="utf-8"))
        return {
            "metrics": metrics,
            "dimensions": dimensions,
            "sample_questions": select_sample_questions(doc),
        }

    @app.get("/api/health")
    def health():
        db_exists = Path(db_path).exists()
        problems: list[str] = []
        if real_ask:
            try:
                load_config()
            except ConfigError as exc:
                problems.append(str(exc))
        if not db_exists:
            problems.append(f"演示库不存在: {db_path}（先运行: python scripts/init_db.py 后复制为 web_demo.db）")
        return {
            "ok": db_exists and not problems,
            "db_exists": db_exists,
            "config_ok": not problems,
            "problems": problems,
        }

    return app


def build_app() -> FastAPI:
    """uvicorn --factory 入口。"""
    return create_app()
