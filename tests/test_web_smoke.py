"""起服务自检：app factory 不连库不连模型也能装配，health 给出可诊断文案。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from web.server.app import build_app, create_app


def test_build_app_factory_starts_without_db_or_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_AGENT_DB_PATH", str(tmp_path / "missing.db"))
    client = TestClient(build_app())
    body = client.get("/api/health").json()
    assert body["ok"] is False
    assert body["db_exists"] is False
    assert any("演示库不存在" in p for p in body["problems"])


def test_create_app_defaults_are_isolated_from_real_pipeline():
    """注入替身即可全量走接口，不需要 key/库——评测与 CI 的零依赖面。"""
    client = TestClient(create_app(ask_fn=lambda **_: {"status": "ok"}, daily_quota=5))
    assert client.get("/api/meta").status_code == 200
