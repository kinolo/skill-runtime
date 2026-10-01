from skill_runtime.adapters.memory import (
    FakeSandboxManager,
    InMemorySkillRegistry,
    InMemoryTraceSink,
    LayeredEnvironment,
    StubAgentExecutor,
)

__all__ = [
    "FakeSandboxManager",
    "InMemorySkillRegistry",
    "InMemoryTraceSink",
    "LayeredEnvironment",
    "StubAgentExecutor",
]

try:
    from skill_runtime.adapters.e2b import (
        E2BEnvironment,
        E2BSandbox,
        e2b_connect_factory,
        e2b_sandbox_factory,
        e2b_snapshot_create,
        e2b_snapshot_lookup,
        layer_key,
        skill_fingerprint,
        snapshot_alias,
    )
except ImportError:  # e2b SDK not installed
    pass
else:
    __all__ += [
        "E2BEnvironment",
        "E2BSandbox",
        "e2b_connect_factory",
        "e2b_sandbox_factory",
        "e2b_snapshot_create",
        "e2b_snapshot_lookup",
        "layer_key",
        "skill_fingerprint",
        "snapshot_alias",
    ]
