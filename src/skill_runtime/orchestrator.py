"""Orchestrator: drives the five-stage execution pipeline via the FSM."""

from __future__ import annotations

import time
from dataclasses import dataclass

from skill_runtime.ids import execution_id as gen_execution_id
from skill_runtime.ids import span_id as gen_span_id
from skill_runtime.ids import trace_id as gen_trace_id
from skill_runtime.models import (
    Artifact,
    ExecutionRecord,
    ExecutionRequest,
    ExecutionResult,
    SandboxPolicy,
    TraceEvent,
    Usage,
    intersect_capabilities,
)
from skill_runtime.policy import cap_to_policy
from skill_runtime.ports import (
    AgentExecutorPort,
    EnvironmentPort,
    SandboxPort,
    SkillRegistryPort,
    TraceSinkPort,
)
from skill_runtime.state_machine import (
    ExecutionEvent,
    ExecutionStateMachine,
    InvalidTransition,
)


class OrchestratorError(Exception):
    pass


@dataclass
class OrchestratorDeps:
    registry: SkillRegistryPort
    env: EnvironmentPort
    sandbox: SandboxPort
    agent: AgentExecutorPort
    trace: TraceSinkPort


class Orchestrator:
    """Control-plane pipeline driver.

    Stages (map 1:1 to FSM):
      1. RESOLVING_SKILLS   — registry.resolve + capability intersection
      2. PREPARING_ENV      — layered snapshot / image prep
      3. BUILDING_SANDBOX   — microVM create
      4. MOUNTING_SKILLS    — bind /skills read-only
      5. EXECUTING          — thin agent executor
      6. COLLECTING_TRACE   — finalize spans, build result
    """

    def __init__(self, deps: OrchestratorDeps) -> None:
        self.deps = deps
        self._records: dict[str, ExecutionRecord] = {}
        self._machines: dict[str, ExecutionStateMachine] = {}

    # ----- public API -----

    def submit(
        self, request: ExecutionRequest, *, execution_id: str | None = None
    ) -> ExecutionResult:
        eid = execution_id or gen_execution_id()
        tid = gen_trace_id()
        fsm = ExecutionStateMachine(eid)
        record = ExecutionRecord(
            execution_id=eid, request=request, trace_id=tid, state=fsm.state
        )
        self._records[eid] = record
        self._machines[eid] = fsm

        self._root_span(eid, tid, fsm)
        try:
            self._run(eid, fsm, record)
        except Exception as exc:  # noqa: BLE001 — boundary: convert to result
            self._abort(fsm, record, ExecutionEvent.FAIL, reason=str(exc))
            record.error = str(exc)

        return self._finalize(eid, fsm, record)

    def get(self, execution_id: str) -> ExecutionRecord:
        return self._records[execution_id]

    def cancel(self, execution_id: str) -> ExecutionRecord:
        record = self._records[execution_id]
        fsm = self._machines[execution_id]
        if fsm.is_terminal:
            return record
        self._abort(fsm, record, ExecutionEvent.CANCEL, reason="client cancel")
        record.sandbox_id and self.deps.sandbox.destroy(record.sandbox_id)
        return record

    # ----- pipeline -----

    def _run(self, eid: str, fsm: ExecutionStateMachine, record: ExecutionRecord) -> None:
        request = record.request
        tid = record.trace_id
        assert tid is not None

        # 1. resolve skills
        self._advance(fsm, record, ExecutionEvent.RESOLVE)
        skills = self.deps.registry.resolve(request.skill_refs)
        record.resolved_skills = skills
        # Registry already intersects; recompute once here as invariant check.
        effective = intersect_capabilities([s.manifest.capabilities for s in skills])
        self._span(eid, tid, fsm, "resolve_skills", attrs={"count": len(skills)})

        # 2. prepare env (layered snapshot)
        self._advance(fsm, record, ExecutionEvent.ENV_READY)
        env_ref = self.deps.env.prepare(
            base_image=request.env.base_image,
            packages=request.env.packages,
            skills=skills,
            snapshot_policy=request.env.snapshot_policy.value,
            pinned_snapshot_id=request.env.pinned_snapshot_id,
        )
        record.env_ref = env_ref
        self._span(eid, tid, fsm, "prepare_env", attrs={"env_ref": env_ref})

        # 3. build sandbox
        self._advance(fsm, record, ExecutionEvent.SANDBOX_READY)
        policy = self._effective_policy(request, effective)
        mounts = [self.deps.registry.bundle(s) for s in skills]
        sid = self.deps.sandbox.create(env_ref, policy, mounts)
        record.sandbox_id = sid
        self._span(eid, tid, fsm, "build_sandbox", attrs={"sandbox_id": sid})

        # 4. mount skills
        self._advance(fsm, record, ExecutionEvent.SKILLS_MOUNTED)
        self._span(
            eid,
            tid,
            fsm,
            "mount_skills",
            attrs={"skills": [s.ref.name for s in skills]},
        )

        # 5. execute
        self._advance(fsm, record, ExecutionEvent.EXEC_STARTED)
        t0 = time.time()
        try:
            run = self.deps.agent.run(
                sandbox_id=sid,
                request=request,
                skills=skills,
                effective_policy=policy,
            )
        except Exception as exc:
            self._span(eid, tid, fsm, "exec", status="error", attrs={"error": str(exc)})
            raise
        self._span(
            eid,
            tid,
            fsm,
            "exec",
            attrs={"status": run.status, "duration_ms": int((time.time() - t0) * 1000)},
        )
        if run.status not in ("ok", "succeeded"):
            raise OrchestratorError(f"agent run failed: {run.error or run.status}")

        # 6. collect trace
        self._advance(fsm, record, ExecutionEvent.EXEC_FINISHED)
        self._span(eid, tid, fsm, "collect_trace")
        self._advance(fsm, record, ExecutionEvent.TRACE_COLLECTED)

        record.result = ExecutionResult(
            execution_id=eid,
            status=fsm.state,
            trace_id=tid,
            artifacts=[
                Artifact(path=p) for p in (getattr(run, "artifacts", None) or request.task.outputs)
            ],
            usage=Usage(
                duration_ms=int((time.time() - t0) * 1000),
                llm_tokens=int((run.usage or {}).get("total_tokens") or 0),
                tool_calls=int((run.usage or {}).get("tool_calls") or 0),
            ),
        )
        if record.sandbox_id:
            # default: destroy after success; pause is opt-in for debug
            self.deps.sandbox.destroy(record.sandbox_id)

    # ----- helpers -----

    def _effective_policy(self, request: ExecutionRequest, caps) -> SandboxPolicy:
        return cap_to_policy(caps, base=request.sandbox_policy)

    def _advance(
        self,
        fsm: ExecutionStateMachine,
        record: ExecutionRecord,
        event: ExecutionEvent,
    ) -> None:
        state = fsm.fire(event)
        record.state = state

    def _abort(
        self,
        fsm: ExecutionStateMachine,
        record: ExecutionRecord,
        event: ExecutionEvent,
        *,
        reason: str,
    ) -> None:
        if fsm.is_terminal:
            return
        try:
            state = fsm.fire(event, reason=reason)
        except InvalidTransition:
            state = fsm.state
        record.state = state
        record.error = reason

    def _root_span(self, eid: str, tid: str, fsm: ExecutionStateMachine) -> None:
        self.deps.trace.emit(
            TraceEvent(
                trace_id=tid,
                span_id=gen_span_id(),
                parent_span_id=None,
                execution_id=eid,
                stage=fsm.state,
                name="execution",
                ts_start=_now(),
                ts_end=_now(),
                status="ok",
            )
        )

    def _span(
        self,
        eid: str,
        tid: str,
        fsm: ExecutionStateMachine,
        name: str,
        *,
        status: str = "ok",
        attrs: dict | None = None,
    ) -> None:
        self.deps.trace.emit(
            TraceEvent(
                trace_id=tid,
                span_id=gen_span_id(),
                parent_span_id=None,
                execution_id=eid,
                stage=fsm.state,
                name=name,
                ts_start=_now(),
                ts_end=_now(),
                status=status,
                attrs=attrs or {},
            )
        )

    def _finalize(
        self, eid: str, fsm: ExecutionStateMachine, record: ExecutionRecord
    ) -> ExecutionResult:
        if record.result is not None:
            return record.result
        return ExecutionResult(
            execution_id=eid,
            status=fsm.state,
            trace_id=record.trace_id or gen_trace_id(),
            artifacts=[],
            usage=Usage(),
            error=record.error,
        )


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
