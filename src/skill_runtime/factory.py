"""Wiring helpers: build an Orchestrator with memory or E2B adapters."""

from __future__ import annotations

from skill_runtime.adapters.memory import (
    FakeSandboxManager,
    InMemorySkillRegistry,
    InMemoryTraceSink,
    LayeredEnvironment,
    StubAgentExecutor,
)
from skill_runtime.orchestrator import Orchestrator, OrchestratorDeps
from skill_runtime.ports import (
    AgentExecutorPort,
    EnvironmentPort,
    SandboxPort,
    SkillRegistryPort,
    TraceSinkPort,
)


def build_memory_orchestrator(
    registry: SkillRegistryPort | None = None,
    *,
    env: EnvironmentPort | None = None,
    sandbox: SandboxPort | None = None,
    agent: AgentExecutorPort | None = None,
    trace: TraceSinkPort | None = None,
) -> Orchestrator:
    return Orchestrator(
        OrchestratorDeps(
            registry=registry or InMemorySkillRegistry(),
            env=env or LayeredEnvironment(),
            sandbox=sandbox or FakeSandboxManager(),
            agent=agent or StubAgentExecutor(),
            trace=trace or InMemoryTraceSink(),
        )
    )


def build_e2b_orchestrator(
    registry: SkillRegistryPort,
    *,
    agent: AgentExecutorPort | None = None,
    trace: TraceSinkPort | None = None,
    sandbox_timeout_sec: int = 3600,
    llm_model: str = "mimo-v2.6-pro",
) -> Orchestrator:
    """Production wiring: E2B env snapshots + E2B sandbox + real agent loop.

    Requires the optional `e2b` dependency and E2B_API_KEY / E2B_DOMAIN env.
    LLM defaults to MiMoCode auth.json (OpenAI-compatible).
    """
    from skill_runtime.adapters.e2b import (
        E2BEnvironment,
        E2BSandbox,
        e2b_connect_factory,
        e2b_sandbox_factory,
        e2b_snapshot_create,
        e2b_snapshot_lookup,
    )
    from skill_runtime.agent.executor import ToolGatedAgentExecutor
    from skill_runtime.agent.llm import OpenAICompatClient

    create = e2b_sandbox_factory()
    sandbox = E2BSandbox(
        create_sandbox=create,
        connect_sandbox=e2b_connect_factory(),
        timeout_sec=sandbox_timeout_sec,
    )
    return Orchestrator(
        OrchestratorDeps(
            registry=registry,
            env=E2BEnvironment(
                create_sandbox=create,
                snapshot_lookup=e2b_snapshot_lookup(),
                snapshot_create=e2b_snapshot_create(),
            ),
            sandbox=sandbox,
            agent=agent
            or ToolGatedAgentExecutor(
                OpenAICompatClient.from_mimocode_auth(model=llm_model),
                sandbox,
            ),
            trace=trace or InMemoryTraceSink(),
        )
    )
