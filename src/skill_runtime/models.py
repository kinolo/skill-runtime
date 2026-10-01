"""Core domain models for skill execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator

from skill_runtime.state_machine import ExecutionState


class TraceLevel(StrEnum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"


class SnapshotPolicy(StrEnum):
    PREFER_CACHE = "prefer_cache"
    ALWAYS_BUILD = "always_build"
    PIN = "pin"


class NetworkMode(StrEnum):
    ALLOWLIST = "allowlist"
    DENY_ALL = "deny_all"
    OPEN = "open"  # dev only


# ---------- skill ----------


class SkillRef(BaseModel):
    name: str
    version: str = "*"  # semver range or exact or "*"


class SkillCapabilities(BaseModel):
    network: list[str] = Field(default_factory=list)  # host allowlist
    filesystem_read: list[str] = Field(default_factory=lambda: ["/workspace"])
    filesystem_write: list[str] = Field(default_factory=lambda: ["/workspace"])
    tools: list[str] = Field(default_factory=list)  # e.g. bash, python


class SkillManifest(BaseModel):
    name: str
    version: str
    description: str = ""
    entry: str = "SKILL.md"
    requires_packages: list[str] = Field(default_factory=list)
    requires_bins: list[str] = Field(default_factory=list)
    capabilities: SkillCapabilities = Field(default_factory=SkillCapabilities)


def intersect_capabilities(caps: list[SkillCapabilities]) -> SkillCapabilities:
    """Capability intersection across mounted skills.

    Empty input → deny-all default with writable workspace only.
    """
    if not caps:
        return SkillCapabilities(
            network=[],
            filesystem_read=["/workspace"],
            filesystem_write=["/workspace"],
            tools=[],
        )

    def _inter(key: str) -> list[str]:
        sets = [set(getattr(c, key)) for c in caps]
        return sorted(set.intersection(*sets)) if sets else []

    return SkillCapabilities(
        network=_inter("network"),
        filesystem_read=_inter("filesystem_read"),
        filesystem_write=_inter("filesystem_write"),
        tools=_inter("tools"),
    )


# ---------- request ----------


class TaskInput(BaseModel):
    path: str
    url: HttpUrl | None = None
    content_base64: str | None = None


class TaskSpec(BaseModel):
    prompt: str
    inputs: list[TaskInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

    @field_validator("outputs", mode="before")
    @classmethod
    def _coerce_outputs(cls, v: Any) -> list[str]:
        """Accept ["path"] or [{"path": "path"}] from the HTTP API draft."""
        if not v:
            return []
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("path") or ""))
            else:
                out.append(str(getattr(item, "path", item)))
        return [p for p in out if p]


class EnvSpec(BaseModel):
    base_image: str = "python:3.12-slim"
    packages: list[str] = Field(default_factory=list)
    snapshot_policy: SnapshotPolicy = SnapshotPolicy.PREFER_CACHE
    pinned_snapshot_id: str | None = None


class AgentSpec(BaseModel):
    model: str = "anthropic/claude-sonnet-4-5"
    max_steps: int = 40
    timeout_sec: int = 600


class FilesystemPolicy(BaseModel):
    readonly: list[str] = Field(default_factory=lambda: ["/skills"])
    writable: list[str] = Field(default_factory=lambda: ["/workspace"])


class NetworkPolicy(BaseModel):
    mode: NetworkMode = NetworkMode.ALLOWLIST
    hosts: list[str] = Field(default_factory=list)


class ResourcePolicy(BaseModel):
    vcpus: int = 2
    memory_mb: int = 2048
    disk_mb: int = 4096


class SandboxPolicy(BaseModel):
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    resources: ResourcePolicy = Field(default_factory=ResourcePolicy)


class TraceConfig(BaseModel):
    level: TraceLevel = TraceLevel.STANDARD
    record_fs: bool = True
    record_net: bool = True


class ExecutionRequest(BaseModel):
    skill_refs: list[SkillRef]
    task: TaskSpec
    env: EnvSpec = Field(default_factory=EnvSpec)
    agent: AgentSpec = Field(default_factory=AgentSpec)
    sandbox_policy: SandboxPolicy = Field(default_factory=SandboxPolicy)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    webhook: HttpUrl | None = None


# ---------- result / trace ----------


class Usage(BaseModel):
    duration_ms: int = 0
    llm_tokens: int = 0
    tool_calls: int = 0


class Artifact(BaseModel):
    path: str
    url: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


class ResolvedSkill(BaseModel):
    ref: SkillRef
    manifest: SkillManifest
    mount_path: str


class ExecutionResult(BaseModel):
    execution_id: str
    status: ExecutionState
    trace_id: str
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None


class TraceEvent(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    execution_id: str
    stage: ExecutionState
    name: str
    ts_start: datetime
    ts_end: datetime | None = None
    status: str = "ok"
    attrs: dict[str, Any] = Field(default_factory=dict)


class ExecutionRecord(BaseModel):
    """In-memory execution record (persistence adapter can wrap this)."""

    execution_id: str
    request: ExecutionRequest
    state: ExecutionState = ExecutionState.CREATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_skills: list[ResolvedSkill] = Field(default_factory=list)
    env_ref: str | None = None
    sandbox_id: str | None = None
    trace_id: str | None = None
    result: ExecutionResult | None = None
    error: str | None = None
