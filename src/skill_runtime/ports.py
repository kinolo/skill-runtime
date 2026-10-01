"""Port interfaces (dependency inversion) for external adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from skill_runtime.models import (
    ExecutionRequest,
    ResolvedSkill,
    SandboxPolicy,
    SkillRef,
    TraceEvent,
)
from skill_runtime.policy import SkillMount


@runtime_checkable
class SkillRegistryPort(Protocol):
    def resolve(self, refs: list[SkillRef]) -> list[ResolvedSkill]:
        """Resolve skill refs to concrete manifests + mount paths."""
        ...

    def bundle(self, resolved: ResolvedSkill) -> SkillMount:
        """Return skill file tree to mount at resolved.mount_path."""
        ...


@runtime_checkable
class EnvironmentPort(Protocol):
    def prepare(
        self,
        *,
        base_image: str,
        packages: list[str],
        skills: list[ResolvedSkill],
        snapshot_policy: str,
        pinned_snapshot_id: str | None = None,
    ) -> str:
        """Return env_ref (image_ref or snapshot_id). Should cache by content hash."""
        ...


@runtime_checkable
class SandboxPort(Protocol):
    def create(self, env_ref: str, policy: SandboxPolicy, mounts: list[SkillMount]) -> str:
        """Create microVM from env_ref (template/snapshot) and install skill mounts.

        Returns sandbox_id. Policy is already capability-intersected.
        """
        ...

    def exec(self, sandbox_id: str, command: list[str], timeout_sec: int) -> ExecResult: ...

    def write_file(self, sandbox_id: str, path: str, content: bytes | str) -> None: ...

    def read_file(self, sandbox_id: str, path: str) -> bytes: ...

    def destroy(self, sandbox_id: str) -> None: ...

    def pause(self, sandbox_id: str) -> None: ...

    def resume(self, sandbox_id: str) -> None: ...


class ExecResult(Protocol):
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class AgentExecutorPort(Protocol):
    def run(
        self,
        *,
        sandbox_id: str,
        request: ExecutionRequest,
        skills: list[ResolvedSkill],
        effective_policy: SandboxPolicy,
    ) -> AgentRunResult:
        """Run agent loop inside sandbox. Thin executor owns tool layer for capability enforcement."""
        ...


class AgentRunResult(Protocol):
    status: str  # ok | failed | timeout
    artifacts: list[str]
    usage: dict
    error: str | None


@runtime_checkable
class TraceSinkPort(Protocol):
    def emit(self, event: TraceEvent) -> None: ...

    def list_events(self, execution_id: str) -> list[TraceEvent]: ...
