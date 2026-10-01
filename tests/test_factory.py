"""Factory wiring smoke tests."""

from skill_runtime.adapters.memory import InMemorySkillRegistry
from skill_runtime.factory import build_memory_orchestrator
from skill_runtime.models import (
    ExecutionRequest,
    SkillCapabilities,
    SkillManifest,
    SkillRef,
    TaskSpec,
)
from skill_runtime.state_machine import ExecutionState


def test_build_memory_orchestrator_end_to_end():
    reg = InMemorySkillRegistry(
        [
            SkillManifest(
                name="pdf",
                version="1.0.0",
                capabilities=SkillCapabilities(
                    network=["api.x.com"],
                    filesystem_read=["/workspace"],
                    filesystem_write=["/workspace"],
                    tools=["python"],
                ),
                description="pdf skill",
            )
        ]
    )
    orch = build_memory_orchestrator(reg)
    result = orch.submit(
        ExecutionRequest(
            skill_refs=[SkillRef(name="pdf")],
            task=TaskSpec(prompt="do pdf things", outputs=["out/a.pdf"]),
        )
    )
    assert result.status is ExecutionState.SUCCEEDED
    record = orch.get(result.execution_id)
    assert record.resolved_skills[0].manifest.capabilities.tools == ["python"]
