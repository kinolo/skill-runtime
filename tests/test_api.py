"""HTTP API tests (in-memory orchestrator, no E2B / no real LLM)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from skill_runtime.adapters.memory import (
    InMemorySkillRegistry,
)
from skill_runtime.api.app import create_app
from skill_runtime.api.store import ExecutionStore
from skill_runtime.factory import build_memory_orchestrator
from skill_runtime.models import (
    SkillCapabilities,
    SkillManifest,
)


def _registry() -> InMemorySkillRegistry:
    reg = InMemorySkillRegistry()
    reg.add(
        SkillManifest(
            name="demo",
            version="1.0.0",
            description="demo",
            capabilities=SkillCapabilities(
                network=["api.example.com"],
                filesystem_read=["/workspace"],
                filesystem_write=["/workspace"],
                tools=["python"],
            ),
        )
    )
    return reg


def _client() -> TestClient:
    orch = build_memory_orchestrator(_registry())
    store = ExecutionStore(orch)
    app = create_app(store, registry=orch.deps.registry, sandbox_mode="memory")
    return TestClient(app)


def _payload() -> dict:
    return {
        "skill_refs": [{"name": "demo", "version": "*"}],
        "task": {"prompt": "do something", "outputs": ["/workspace/a.txt"]},
    }


def test_healthz():
    c = _client()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_sync_execution():
    c = _client()
    r = c.post("/v1/executions", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "SUCCEEDED"
    assert body["execution_id"].startswith("exec_")
    assert body["trace_id"].startswith("tr_")
    assert [a["path"] for a in body["artifacts"]] == ["/workspace/a.txt"]
    assert body["usage"]["tool_calls"] >= 0


def test_create_accepts_outputs_as_objects():
    c = _client()
    payload = _payload()
    payload["task"]["outputs"] = [{"path": "/workspace/a.txt"}]
    r = c.post("/v1/executions", json=payload)
    assert r.status_code == 200
    assert r.json()["artifacts"][0]["path"] == "/workspace/a.txt"


def test_get_execution_detail_and_trace():
    c = _client()
    eid = c.post("/v1/executions", json=_payload()).json()["execution_id"]

    r = c.get(f"/v1/executions/{eid}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "SUCCEEDED"
    assert detail["resolved_skills"][0]["manifest"]["name"] == "demo"
    assert detail["history"][-1]["to_state"] == "SUCCEEDED"

    r = c.get(f"/v1/executions/{eid}/trace")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["events"]]
    assert "resolve_skills" in names
    assert "exec" in names

    r = c.get(f"/v1/executions/{eid}/artifacts")
    assert r.status_code == 200
    assert r.json()["artifacts"][0]["path"] == "/workspace/a.txt"


def test_missing_execution_404():
    c = _client()
    assert c.get("/v1/executions/exec_nope").status_code == 404
    assert c.get("/v1/executions/exec_nope/trace").status_code == 404


def test_async_create_and_poll():
    c = _client()
    r = c.post("/v1/executions", json=_payload(), params={"mode": "async"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "CREATED"
    assert body["poll_url"].endswith(body["execution_id"])

    # wait for worker
    import time

    for _ in range(50):
        d = c.get(body["poll_url"]).json()
        if d["status"] != "CREATED" and d.get("usage") is not None or d["status"] in (
            "SUCCEEDED",
            "FAILED",
            "TIMEOUT",
            "CANCELLED",
        ):
            break
        time.sleep(0.05)
    assert d["status"] == "SUCCEEDED"


def test_cancel_and_replay():
    c = _client()
    eid = c.post("/v1/executions", json=_payload()).json()["execution_id"]
    r = c.post(f"/v1/executions/{eid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "SUCCEEDED"  # already terminal

    r = c.post(f"/v1/executions/{eid}/replay")
    assert r.status_code == 200
    assert r.json()["status"] == "SUCCEEDED"
    assert r.json()["execution_id"] != eid


def test_skills_publish_and_list():
    c = _client()
    r = c.get("/v1/skills")
    assert r.status_code == 200
    assert any(s["name"] == "demo" for s in r.json()["skills"])

    r = c.get("/v1/skills/demo/versions")
    assert r.json()["versions"] == ["1.0.0"]

    r = c.post(
        "/v1/skills/demo/versions",
        json={
            "manifest": {
                "name": "demo",
                "version": "2.0.0",
                "description": "v2",
                "capabilities": {"tools": ["bash"]},
            },
            "files": {"scripts/x.sh": "echo hi"},
        },
    )
    assert r.status_code == 201
    assert r.json()["version"] == "2.0.0"

    r = c.get("/v1/skills/demo/versions")
    assert set(r.json()["versions"]) == {"1.0.0", "2.0.0"}

    # name mismatch
    r = c.post(
        "/v1/skills/other/versions",
        json={"manifest": {"name": "demo", "version": "9.0.0"}},
    )
    assert r.status_code == 422
