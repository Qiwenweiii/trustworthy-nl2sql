"""阶段 3 M1c：Web 壳契约测试（fastapi TestClient + 替身 ask，零模型调用、零 key）。

五组按 WEB_DEMO_PLAN §5 排列，判别性纪律=分层红：对宽松版（无校验、无闸门、
全透传）先红，对目标实现转绿。替身 ask 的签名与 pipeline.ask 的壳层调用面一致：
ask_fn(question=..., tracer=...) -> outcome dict。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from data_agent.pipeline_steps import new_outcome
from web.server.app import create_app

CONTRACT_KEYS = set(new_outcome("x").keys())


def _ok_outcome(question="上个月销售额是多少"):
    return {
        "question": question,
        "status": "ok",
        "sql": "SELECT SUM(amount) AS s FROM orders WHERE status = 'paid'",
        "guard": {"passed": True, "violations": []},
        "enforcement": {"metric": "销售额", "changed": True, "actions": []},
        "time_fix": None,
        "result": {
            "columns": ["s"],
            "rows": [{"s": 123.0}],
            "row_count": 1,
            "empty_result": False,
            "truncated": False,
            "max_rows": 200,
            "elapsed_ms": 1.2,
        },
        "error": None,
        "llm_response": "```sql\nSELECT ...```",
        "caliber_note": None,
        "retry": None,
        "elapsed_ms": 24000.0,
    }


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        fields = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                fields["event"] = line[len("event: ") :]
            elif line.startswith("data: "):
                fields["data"] = line[len("data: ") :]
        events.append(fields)
    return events


def _client(**kwargs):
    return TestClient(create_app(**kwargs))


class RecordingAsk:
    def __init__(self, outcome=None, exc=None):
        self.calls: list[dict] = []
        self.outcome = outcome if outcome is not None else _ok_outcome()
        self.exc = exc

    def __call__(self, *, question: str, tracer):
        self.calls.append({"question": question, "tracer": tracer})
        if self.exc is not None:
            raise self.exc
        tracer("sql", "SELECT SUM(amount) AS s FROM orders")
        tracer("guard", "passed=True violations=0")
        tracer("exec", "rows=1 elapsed_ms=1.2")
        return dict(self.outcome)


# ---------------------------------------------------------------- 组 1：契约透传


def test_result_frame_keys_equal_outcome_contract():
    """终局 result 帧的键集合与 outcome 字段字典逐键相等，多一少一都红。"""
    ask = RecordingAsk()
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    resp = client.post("/api/ask", json={"question": ask.outcome["question"]})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    result_frames = [e for e in events if e.get("event") == "result"]
    assert len(result_frames) == 1
    payload = json.loads(result_frames[0]["data"])
    assert set(payload.keys()) == CONTRACT_KEYS


def test_result_frame_is_verbatim_outcome():
    """outcome 原样透传：壳层不增删改任何值（替身返回什么，帧里就是什么）。"""
    outcome = _ok_outcome()
    ask = RecordingAsk(outcome=outcome)
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    resp = client.post("/api/ask", json={"question": outcome["question"]})
    frame = [e for e in _parse_sse(resp.text) if e.get("event") == "result"][0]
    assert json.loads(frame["data"]) == outcome


def test_stage_frames_carry_only_name_and_elapsed():
    """stage 帧只含阶段名与已耗时，不夹带任何 outcome 数据。"""
    outcome = _ok_outcome()
    ask = RecordingAsk(outcome=outcome)
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    resp = client.post("/api/ask", json={"question": outcome["question"]})
    events = _parse_sse(resp.text)
    stage_events = [e for e in events if e.get("event") == "stage"]
    assert stage_events, "替身打了三次 trace，就该有 stage 帧"
    for e in stage_events:
        payload = json.loads(e["data"])
        assert set(payload.keys()) == {"name", "elapsed_ms"}
        assert outcome["sql"] not in e["data"]
        assert payload["name"] in {"sql", "guard", "exec"}
    # 除 result 帧以外，流里任何一帧都不得含 outcome 数据
    for e in events:
        if e.get("event") != "result":
            assert outcome["sql"] not in e.get("data", "")


# ---------------------------------------------------------------- 组 2：参数白名单


def test_extra_forbidden_params_rejected():
    """请求体塞 injected_sql/max_rows → 422，替身根本不会被调到。"""
    ask = RecordingAsk()
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    resp = client.post(
        "/api/ask",
        json={"question": "总销售额", "injected_sql": "DROP TABLE orders", "max_rows": 9999},
    )
    assert resp.status_code == 422
    assert ask.calls == []


def test_ask_fn_receives_only_whitelisted_kwargs():
    """壳层调用管线时只传 question 与 tracer，别的入参一律不出现。"""
    ask = RecordingAsk()
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    client.post("/api/ask", json={"question": "总销售额"})
    assert len(ask.calls) == 1
    assert set(ask.calls[0].keys()) == {"question", "tracer"}


@pytest.mark.parametrize(
    "question",
    ["", "   ", "x" * 201, None],
    ids=["empty", "whitespace", "too_long", "missing"],
)
def test_question_validation(question):
    """1–200 字符、去空后非空，非法入参 422。"""
    ask = RecordingAsk()
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    body = {"question": question} if question is not None else {}
    resp = client.post("/api/ask", json=body)
    assert resp.status_code == 422
    assert ask.calls == []


# ---------------------------------------------------------------- 组 3：闸门与配额


def test_concurrency_gate_returns_429():
    """并发上限 2：第 3 个在执行中的请求拿 429，且不触达管线。"""
    import threading

    inside = threading.Semaphore(0)
    release = threading.Event()

    def slow_ask(*, question, tracer):
        inside.release()
        release.wait(timeout=5)
        return _ok_outcome(question)

    client = _client(ask_fn=slow_ask, concurrency_limit=2, daily_quota=300)
    responses = []
    threads = []
    for _ in range(2):
        t = threading.Thread(
            target=lambda: responses.append(
                client.post("/api/ask", json={"question": "总销售额"}).status_code
            )
        )
        t.start()
        threads.append(t)
    # 等两个先占住闸门
    inside.acquire(timeout=5)
    inside.acquire(timeout=5)
    third = client.post("/api/ask", json={"question": "总销售额"})
    assert third.status_code == 429
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert responses == [200, 200]


def test_daily_quota_returns_429():
    """日配额 1 打满后，第二个请求 429，管线只被调了一次。"""
    ask = RecordingAsk()
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=1)
    first = client.post("/api/ask", json={"question": "总销售额"})
    second = client.post("/api/ask", json={"question": "总销售额"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert len(ask.calls) == 1


# ---------------------------------------------------------------- 组 4：/api/meta 同源


def test_meta_metrics_dimensions_same_source(monkeypatch):
    """meta 的指标/维度就是 semantic_tables 登记表本身：现读，改一条立即多一条。"""
    from data_agent import semantic_tables

    client = _client(ask_fn=RecordingAsk(), concurrency_limit=2, daily_quota=300)
    before = {m["name"] for m in client.get("/api/meta").json()["metrics"]}
    monkeypatch.setitem(semantic_tables.METRICS, "毛利率", {"name": "毛利率", "synonyms": ["毛利率"]})
    monkeypatch.setitem(
        semantic_tables.DIMENSIONS, "产品线", {"name": "产品线", "aliases": ["产品线"], "column": "x.y"}
    )
    meta = client.get("/api/meta").json()
    assert {m["name"] for m in meta["metrics"]} == before | {"毛利率"}
    assert "产品线" in {d["name"] for d in meta["dimensions"]}


def test_meta_shape_and_enum_fields():
    meta = _client(ask_fn=RecordingAsk()).get("/api/meta").json()
    assert set(meta.keys()) == {"metrics", "dimensions", "sample_questions"}
    for m in meta["metrics"]:
        assert set(m.keys()) == {"name", "synonyms"}
    for d in meta["dimensions"]:
        assert {"name", "aliases", "column"} <= set(d.keys())
        assert set(d.keys()) <= {"name", "aliases", "column", "enum"}


def test_meta_sample_questions_from_golden_file(tmp_path):
    """sample_questions 只从已毕业 stage 的 golden 题里选，且含拒答样例的裁决口径：
    类目"拒答"恰好 2 条；题面必须与题集文件逐字一致（不手抄）。"""
    from data_agent.config import load_config  # noqa: F401  确保不触网也能构造

    cases_file = tmp_path / "eval_cases.json"
    cases_file.write_text(
        json.dumps(
            {
                "meta": {"stages": {"v1": {"status": "done"}, "v2": {"status": "done"}, "v9": {"status": "pending"}}},
                "cases": [
                    {"id": "A1", "stage": "v2", "category": "聚合形态", "question": "总销售额是多少", "deterministic": True},
                    {"id": "A2", "stage": "v2", "category": "聚合形态", "question": "各渠道订单数", "deterministic": True},
                    {"id": "B1", "stage": "v2", "category": "渠道维度", "question": "app渠道销售额", "deterministic": False},
                    {"id": "R2", "stage": "v1", "category": "拒答", "question": "各产品线毛利率", "deterministic": False},
                    {"id": "R3", "stage": "v1", "category": "拒答", "question": "新客销售额", "deterministic": False},
                    {"id": "P1", "stage": "v9", "category": "拒答", "question": "未毕业题不该出现", "deterministic": False},
                    {"id": "X1", "stage": "v2", "category": "安全", "question": "删表演示", "deterministic": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = _client(ask_fn=RecordingAsk(), cases_path=cases_file)
    questions = client.get("/api/meta").json()["sample_questions"]
    assert "未毕业题不该出现" not in questions
    assert "删表演示" not in questions  # 安全类不进推荐问法
    refusals = [q for q in questions if q in {"各产品线毛利率", "新客销售额"}]
    assert len(refusals) == 2
    assert "各渠道订单数" not in questions  # 同类目只取第一条，稳定规则


def test_meta_sample_questions_real_file():
    """真实题集文件也要满足规则：全部逐字来自 golden set，含 2 条拒答样例。"""
    questions = _client(ask_fn=RecordingAsk()).get("/api/meta").json()["sample_questions"]
    golden = json.load(open("problems/eval_cases.json", encoding="utf-8"))["cases"]
    texts = {c["question"] for c in golden}
    assert questions and set(questions) <= texts
    refusal_texts = {c["question"] for c in golden if c["category"] == "拒答"}
    assert len([q for q in questions if q in refusal_texts]) == 2
    assert 8 <= len(questions) <= 10


# ---------------------------------------------------------------- 组 5：兜底与自检


def test_unknown_exception_fixed_message():
    """管线抛"理论不该发生"的未知异常：SSE 出 error 帧固定文案，绝不带堆栈。"""
    ask = RecordingAsk(exc=RuntimeError("secret internal state at 0xdeadbeef"))
    client = _client(ask_fn=ask, concurrency_limit=2, daily_quota=300)
    resp = client.post("/api/ask", json={"question": "总销售额"})
    assert resp.status_code == 200  # 流已开，状态无法回收，兜底走帧
    events = _parse_sse(resp.text)
    error_frames = [e for e in events if e.get("event") == "error"]
    assert len(error_frames) == 1
    assert json.loads(error_frames[0]["data"]) == {"message": "服务内部错误，请联系演示负责人"}
    assert "secret" not in resp.text and "Traceback" not in resp.text


def test_health_reports_db_and_config(tmp_path):
    good_db = tmp_path / "ok.db"
    good_db.write_text("x")
    client = _client(ask_fn=RecordingAsk(), db_path=good_db)
    body = client.get("/api/health").json()
    assert {"ok", "db_exists", "config_ok"} <= set(body.keys())
    assert body["db_exists"] is True

    missing = tmp_path / "nope.db"
    client2 = _client(ask_fn=RecordingAsk(), db_path=missing)
    body2 = client2.get("/api/health").json()
    assert body2["db_exists"] is False
    assert body2["ok"] is False
