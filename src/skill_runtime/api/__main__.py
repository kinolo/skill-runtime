"""Run the HTTP API: uvicorn skill_runtime.api.app:create_app --factory"""

from __future__ import annotations

import os

import uvicorn

from skill_runtime.adapters.memory import InMemorySkillRegistry
from skill_runtime.api.app import create_app
from skill_runtime.api.store import ExecutionStore
from skill_runtime.factory import build_e2b_orchestrator, build_memory_orchestrator


def build():
    registry = InMemorySkillRegistry()
    if os.environ.get("E2B_API_KEY"):
        orch = build_e2b_orchestrator(registry)
        mode = "e2b"
    else:
        orch = build_memory_orchestrator(registry)
        mode = "memory"
    store = ExecutionStore(orch)
    return create_app(store, registry=registry, sandbox_mode=mode)


if __name__ == "__main__":
    uvicorn.run(build, factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
