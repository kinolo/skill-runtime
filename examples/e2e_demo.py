"""End-to-end demo: real LLM agent loop + E2B microVM (or in-process fallback).

Usage:
  export E2B_API_KEY=...                 # real microVM
  # optional overrides:
  export SKILL_RUNTIME_LLM_MODEL=mimo-v2.6-pro
  .venv/bin/python examples/e2e_demo.py

Without E2B_API_KEY the demo still exercises the real LLM tool loop against
FakeSandboxManager (control-plane correctness), and prints a clear notice.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from skill_runtime.adapters.memory import (
    FakeSandboxManager,
    InMemorySkillRegistry,
    InMemoryTraceSink,
    LayeredEnvironment,
)
from skill_runtime.agent.executor import ToolGatedAgentExecutor
from skill_runtime.agent.llm import OpenAICompatClient
from skill_runtime.factory import Orchestrator, OrchestratorDeps
from skill_runtime.models import (
    ExecutionRequest,
    ExecutionState,
    SkillCapabilities,
    SkillManifest,
    SkillRef,
    TaskSpec,
)


def build_registry() -> InMemorySkillRegistry:
    reg = InMemorySkillRegistry()
    reg.add(
        SkillManifest(
            name="hello-skill",
            version="1.0.0",
            description="Writes greeting artifacts under /workspace",
            capabilities=SkillCapabilities(
                network=[],  # deny-all egress
                filesystem_read=["/workspace", "/skills"],
                filesystem_write=["/workspace"],
                tools=["bash", "python", "write_file", "read_file", "list_dir"],
            ),
        ),
        files={
            "SKILL.md": (
                "# hello-skill\n\n"
                "1. Create /workspace/hello.txt with a short greeting.\n"
                "2. Create /workspace/greet.py that prints the greeting.\n"
                "3. Run python /workspace/greet.py once.\n"
                "4. Call finish() with those artifact paths.\n"
            ),
        },
    )
    return reg


def main() -> int:
    llm = OpenAICompatClient.from_mimocode_auth(
        model=os.environ.get("SKILL_RUNTIME_LLM_MODEL") or "mimo-v2.6-pro"
    )
    registry = build_registry()

    e2b_key = os.environ.get("E2B_API_KEY")
    mode = "e2b"
    if e2b_key:
        from skill_runtime.adapters.e2b import (
            E2BEnvironment,
            E2BSandbox,
            e2b_connect_factory,
            e2b_sandbox_factory,
            e2b_snapshot_create,
            e2b_snapshot_lookup,
        )

        create = e2b_sandbox_factory()
        env = E2BEnvironment(
            create_sandbox=create,
            snapshot_lookup=e2b_snapshot_lookup(),
            snapshot_create=e2b_snapshot_create(),
        )
        sandbox = E2BSandbox(create_sandbox=create, connect_sandbox=e2b_connect_factory())
    else:
        mode = "fake-sandbox (no E2B_API_KEY)"
        env = LayeredEnvironment()
        sandbox = FakeSandboxManager()

    agent = ToolGatedAgentExecutor(llm, sandbox)
    trace = InMemoryTraceSink()
    orch = Orchestrator(
        OrchestratorDeps(
            registry=registry,
            env=env,
            sandbox=sandbox,
            agent=agent,
            trace=trace,
        )
    )

    request = ExecutionRequest(
        skill_refs=[SkillRef(name="hello-skill")],
        task=TaskSpec(
            prompt=(
                "Follow the mounted hello-skill instructions exactly. "
                "Produce /workspace/hello.txt and /workspace/greet.py, run greet.py, "
                "then call finish() with both paths."
            ),
            outputs=["/workspace/hello.txt", "/workspace/greet.py"],
        ),
    )

    print(f"=== skill-runtime e2e demo · sandbox={mode} · model={llm.model} ===")
    try:
        result = orch.submit(request)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    record = orch.get(result.execution_id)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    print("--- FSM ---")
    print([r.to_state.value for r in orch._machines[result.execution_id].history])
    print("--- agent steps ---")
    run = getattr(agent, "last_run", None)
    if run and getattr(run, "steps", None):
        for i, step in enumerate(run.steps, 1):
            print(f"  [{i}] {step}")
    else:
        print("  (see usage)", result.usage)

    # verify artifacts on the sandbox
    if result.status is ExecutionState.SUCCEEDED and record.sandbox_id:
        print("--- artifact contents ---")
        for art in result.artifacts:
            try:
                data = sandbox.read_file(record.sandbox_id, art.path)
                print(f"{art.path}: {data[:200]!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"{art.path}: <read failed: {exc}>")

    print("--- trace events ---")
    for e in trace.list_events(result.execution_id):
        print(f"  {e.stage.value:18} {e.name:16} {e.status}")
    return 0 if result.status is ExecutionState.SUCCEEDED else 2


if __name__ == "__main__":
    raise SystemExit(main())
