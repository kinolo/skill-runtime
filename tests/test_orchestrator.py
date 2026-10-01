"""Orchestrator pipeline integration tests (in-memory adapters)."""


from skill_runtime.adapters.memory import (
    FakeSandboxManager,
    InMemorySkillRegistry,
    InMemoryTraceSink,
    LayeredEnvironment,
    StubAgentExecutor,
)
from skill_runtime.models import (
    ExecutionRequest,
    SkillCapabilities,
    SkillManifest,
    SkillRef,
    TaskSpec,
)
from skill_runtime.orchestrator import Orchestrator, OrchestratorDeps
from skill_runtime.state_machine import ExecutionState


def _registry() -> InMemorySkillRegistry:
    cap = SkillCapabilities(
        network=["api.example.com"],
        filesystem_read=["/workspace"],
        filesystem_write=["/workspace"],
        tools=["python", "bash"],
    )
    return InMemorySkillRegistry(
        [
            SkillManifest(name="pdf", version="1.0.0", capabilities=cap),
            SkillManifest(name="docx", version="1.0.0", capabilities=cap),
        ]
    )


def _orch() -> Orchestrator:
    return Orchestrator(
        OrchestratorDeps(
            registry=_registry(),
            env=LayeredEnvironment(),
            sandbox=FakeSandboxManager(),
            agent=StubAgentExecutor(),
            trace=InMemoryTraceSink(),
        )
    )


def _req() -> ExecutionRequest:
    return ExecutionRequest(
        skill_refs=[SkillRef(name="pdf"), SkillRef(name="docx")],
        task=TaskSpec(prompt="convert pdf to docx", outputs=["output/report.docx"]),
    )


def test_happy_path_reaches_succeeded_with_trace():
    orch = _orch()
    result = orch.submit(_req())

    assert result.status is ExecutionState.SUCCEEDED
    assert result.error is None
    assert result.trace_id.startswith("tr_")

    record = orch.get(result.execution_id)
    assert record.state is ExecutionState.SUCCEEDED
    assert record.env_ref is not None
    assert record.sandbox_id is not None
    assert len(record.resolved_skills) == 2

    events = orch.deps.trace.list_events(result.execution_id)
    names = [e.name for e in events]
    assert "execution" in names
    assert "resolve_skills" in names
    assert "prepare_env" in names
    assert "build_sandbox" in names
    assert "mount_skills" in names
    assert "exec" in names
    assert "collect_trace" in names

    # FSM history covers full pipeline
    fsm_states = [r.to_state.value for r in orch._machines[result.execution_id].history]
    assert fsm_states[0] == "RESOLVING_SKILLS"
    assert fsm_states[-1] == "SUCCEEDED"


def test_env_cache_hits_on_second_identical_request():
    orch = _orch()
    orch.submit(_req())
    orch.submit(_req())
    assert isinstance(orch.deps.env, LayeredEnvironment)
    assert orch.deps.env.build_count == 1
    assert orch.deps.env.hit_count == 1


def test_failure_path_records_failed_terminal():
    class BoomAgent(StubAgentExecutor):
        def run(self, **kwargs):
            raise RuntimeError("llm unavailable")

    orch = Orchestrator(
        OrchestratorDeps(
            registry=_registry(),
            env=LayeredEnvironment(),
            sandbox=FakeSandboxManager(),
            agent=BoomAgent(),
            trace=InMemoryTraceSink(),
        )
    )
    result = orch.submit(_req())
    assert result.status is ExecutionState.FAILED
    assert "llm unavailable" in (result.error or "")


def test_cancel_terminal():
    orch = _orch()
    # cancel before run is not possible via submit (sync); simulate cancel of done exec
    result = orch.submit(_req())
    record = orch.cancel(result.execution_id)
    assert record.state is ExecutionState.SUCCEEDED  # already terminal, no-op


def test_capability_intersection_narrows_sandbox_policy():

    reg = InMemorySkillRegistry(
        [
            SkillManifest(
                name="a",
                version="1.0.0",
                capabilities=SkillCapabilities(
                    network=["x.com", "y.com"],
                    filesystem_read=["/workspace", "/data"],
                    filesystem_write=["/workspace", "/tmp"],
                    tools=["python"],
                ),
            ),
            SkillManifest(
                name="b",
                version="1.0.0",
                capabilities=SkillCapabilities(
                    network=["y.com", "z.com"],
                    filesystem_read=["/workspace"],
                    filesystem_write=["/workspace"],
                    tools=["python"],
                ),
            ),
        ]
    )
    sandbox = FakeSandboxManager()
    orch = Orchestrator(
        OrchestratorDeps(
            registry=reg,
            env=LayeredEnvironment(),
            sandbox=sandbox,
            agent=StubAgentExecutor(),
            trace=InMemoryTraceSink(),
        )
    )
    req = ExecutionRequest(
        skill_refs=[SkillRef(name="a"), SkillRef(name="b")],
        task=TaskSpec(prompt="t"),
    )
    result = orch.submit(req)
    assert result.status is ExecutionState.SUCCEEDED
    box = sandbox.get(orch.get(result.execution_id).sandbox_id)
    assert box.policy.network.hosts == ["y.com"]
    assert "/workspace" in box.policy.filesystem.writable
    assert "/tmp" not in box.policy.filesystem.writable
