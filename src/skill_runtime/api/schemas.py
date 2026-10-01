"""HTTP API schemas layered on domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from skill_runtime.models import (
    Artifact,
    ExecutionRequest,
    ExecutionState,
    SkillManifest,
    Usage,
)
from skill_runtime.state_machine import TransitionRecord


class ExecMode(StrEnum):
    SYNC = "sync"
    ASYNC = "async"


class CreateExecutionQuery(BaseModel):
    mode: ExecMode = ExecMode.SYNC


class CreateExecutionBody(ExecutionRequest):
    mode: ExecMode | None = None  # optional override in body


class ExecutionAccepted(BaseModel):
    execution_id: str
    status: ExecutionState
    poll_url: str


class ExecutionSummary(BaseModel):
    execution_id: str
    status: ExecutionState
    created_at: datetime
    trace_id: str | None = None
    env_ref: str | None = None
    sandbox_id: str | None = None
    error: str | None = None
    usage: Usage | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    poll_url: str | None = None
    trace_url: str | None = None
    artifacts_url: str | None = None


class ExecutionDetail(ExecutionSummary):
    request: ExecutionRequest
    resolved_skills: list[dict[str, Any]] = Field(default_factory=list)
    history: list[TransitionRecord] = Field(default_factory=list)


class TraceResponse(BaseModel):
    execution_id: str
    trace_id: str | None
    events: list[dict[str, Any]]


class ArtifactsResponse(BaseModel):
    execution_id: str
    artifacts: list[Artifact]


class SkillListResponse(BaseModel):
    skills: list[dict[str, Any]]


class SkillVersionListResponse(BaseModel):
    name: str
    versions: list[str]


class SkillPublishRequest(BaseModel):
    manifest: SkillManifest
    files: dict[str, str] = Field(default_factory=dict)


class SkillPublishResponse(BaseModel):
    name: str
    version: str
    file_count: int


class ErrorResponse(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    sandbox_mode: str
