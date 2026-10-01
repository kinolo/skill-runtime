"""FastAPI control-plane app."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from skill_runtime import __version__
from skill_runtime.adapters.memory import InMemorySkillRegistry
from skill_runtime.api.schemas import (
    ArtifactsResponse,
    CreateExecutionBody,
    ExecMode,
    ExecutionAccepted,
    ExecutionDetail,
    ExecutionSummary,
    HealthResponse,
    SkillListResponse,
    SkillPublishRequest,
    SkillPublishResponse,
    SkillVersionListResponse,
    TraceResponse,
)
from skill_runtime.api.store import ExecutionNotFound, ExecutionNotReady, ExecutionStore
from skill_runtime.models import ExecutionRequest, ExecutionState
from skill_runtime.state_machine import TransitionRecord

logger = logging.getLogger(__name__)


def create_app(
    store: ExecutionStore,
    *,
    registry: InMemorySkillRegistry | None = None,
    sandbox_mode: str = "memory",
) -> FastAPI:
    reg = registry or InMemorySkillRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        store.shutdown()

    app = FastAPI(title="skill-runtime", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.registry = reg
    app.state.sandbox_mode = sandbox_mode

    def _urls(eid: str) -> dict[str, str]:
        return {
            "poll_url": f"/v1/executions/{eid}",
            "trace_url": f"/v1/executions/{eid}/trace",
            "artifacts_url": f"/v1/executions/{eid}/artifacts",
        }

    def _summary(eid: str) -> ExecutionSummary:
        record = store.get_record(eid)
        result = record.result
        return ExecutionSummary(
            execution_id=record.execution_id,
            status=record.state,
            created_at=record.created_at,
            trace_id=record.trace_id,
            env_ref=record.env_ref,
            sandbox_id=record.sandbox_id,
            error=record.error or (result.error if result else None),
            usage=result.usage if result else None,
            artifacts=result.artifacts if result else [],
            **_urls(eid),
        )

    # ----- health -----

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(version=__version__, sandbox_mode=app.state.sandbox_mode)

    # ----- executions -----

    @app.post("/v1/executions")
    def create_execution(
        body: CreateExecutionBody,
        mode: Annotated[ExecMode | None, Query()] = None,
    ) -> Any:
        run_mode = mode or body.mode or ExecMode.SYNC
        request = ExecutionRequest.model_validate(body.model_dump(exclude={"mode"}))

        if run_mode is ExecMode.ASYNC:
            eid = store.submit_async(request)
            if request.webhook:
                store.set_webhook(eid, str(request.webhook))
            return JSONResponse(
                status_code=202,
                content=ExecutionAccepted(
                    execution_id=eid,
                    status=ExecutionState.CREATED,
                    poll_url=f"/v1/executions/{eid}",
                ).model_dump(mode="json"),
            )

        result = store.submit_sync(request)
        return result

    @app.get("/v1/executions/{execution_id}", response_model=ExecutionDetail)
    def get_execution(execution_id: str) -> ExecutionDetail:
        try:
            record = store.get_record(execution_id)
        except ExecutionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        summary = _summary(execution_id)
        history: list[TransitionRecord] = []
        machine = store.orchestrator._machines.get(execution_id)
        if machine is not None:
            history = list(machine.history)

        return ExecutionDetail(
            **summary.model_dump(),
            request=record.request,
            resolved_skills=[s.model_dump(mode="json") for s in record.resolved_skills],
            history=history,
        )

    @app.get("/v1/executions/{execution_id}/trace", response_model=TraceResponse)
    def get_trace(execution_id: str) -> TraceResponse:
        try:
            record = store.get_record(execution_id)
        except ExecutionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        events = store.orchestrator.deps.trace.list_events(execution_id)
        return TraceResponse(
            execution_id=execution_id,
            trace_id=record.trace_id,
            events=[e.model_dump(mode="json") for e in events],
        )

    @app.get("/v1/executions/{execution_id}/artifacts", response_model=ArtifactsResponse)
    def get_artifacts(execution_id: str) -> ArtifactsResponse:
        try:
            result = store.get_result(execution_id)
        except ExecutionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExecutionNotReady as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ArtifactsResponse(execution_id=execution_id, artifacts=result.artifacts)

    @app.post("/v1/executions/{execution_id}/cancel", response_model=ExecutionSummary)
    def cancel_execution(execution_id: str) -> ExecutionSummary:
        try:
            store.cancel(execution_id)
        except ExecutionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _summary(execution_id)

    @app.post("/v1/executions/{execution_id}/replay")
    def replay_execution(
        execution_id: str,
        mode: Annotated[ExecMode, Query()] = ExecMode.SYNC,
    ) -> Any:
        try:
            record = store.get_record(execution_id)
        except ExecutionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if mode is ExecMode.ASYNC:
            eid = store.submit_async(record.request)
            return JSONResponse(
                status_code=202,
                content=ExecutionAccepted(
                    execution_id=eid,
                    status=ExecutionState.CREATED,
                    poll_url=f"/v1/executions/{eid}",
                ).model_dump(mode="json"),
            )
        return store.submit_sync(record.request)

    # ----- skills -----

    @app.get("/v1/skills", response_model=SkillListResponse)
    def list_skills() -> SkillListResponse:
        items: list[dict[str, Any]] = []
        for versions in reg._by_name.values():
            for manifest in versions.values():
                items.append(manifest.model_dump(mode="json"))
        return SkillListResponse(skills=items)

    @app.get("/v1/skills/{name}/versions", response_model=SkillVersionListResponse)
    def list_skill_versions(name: str) -> SkillVersionListResponse:
        versions = reg._by_name.get(name)
        if versions is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        return SkillVersionListResponse(name=name, versions=list(versions))

    @app.post(
        "/v1/skills/{name}/versions",
        response_model=SkillPublishResponse,
        status_code=201,
    )
    def publish_skill_version(name: str, body: SkillPublishRequest) -> SkillPublishResponse:
        if body.manifest.name != name:
            raise HTTPException(
                status_code=422,
                detail=f"manifest.name {body.manifest.name!r} != path name {name!r}",
            )
        reg.add(body.manifest, files=body.files)
        return SkillPublishResponse(
            name=name,
            version=body.manifest.version,
            file_count=len(body.files) + 1,
        )

    return app
