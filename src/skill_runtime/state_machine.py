"""Execution lifecycle state machine.

Pipeline: request → env prep → sandbox build → execute → trace
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel


class ExecutionState(StrEnum):
    CREATED = "CREATED"
    RESOLVING_SKILLS = "RESOLVING_SKILLS"
    PREPARING_ENV = "PREPARING_ENV"
    BUILDING_SANDBOX = "BUILDING_SANDBOX"
    MOUNTING_SKILLS = "MOUNTING_SKILLS"
    EXECUTING = "EXECUTING"
    COLLECTING_TRACE = "COLLECTING_TRACE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ExecutionEvent(StrEnum):
    RESOLVE = "RESOLVE"
    ENV_READY = "ENV_READY"
    SANDBOX_READY = "SANDBOX_READY"
    SKILLS_MOUNTED = "SKILLS_MOUNTED"
    EXEC_STARTED = "EXEC_STARTED"
    EXEC_FINISHED = "EXEC_FINISHED"
    TRACE_COLLECTED = "TRACE_COLLECTED"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    CANCEL = "CANCEL"


TERMINAL_STATES: Final[frozenset[ExecutionState]] = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.TIMEOUT,
        ExecutionState.CANCELLED,
    }
)

# Happy path forward edges; FAIL/TIMEOUT/CANCEL are allowed from any non-terminal state.
_TRANSITIONS: Final[dict[ExecutionState, dict[ExecutionEvent, ExecutionState]]] = {
    ExecutionState.CREATED: {ExecutionEvent.RESOLVE: ExecutionState.RESOLVING_SKILLS},
    ExecutionState.RESOLVING_SKILLS: {ExecutionEvent.ENV_READY: ExecutionState.PREPARING_ENV},
    ExecutionState.PREPARING_ENV: {
        ExecutionEvent.SANDBOX_READY: ExecutionState.BUILDING_SANDBOX,
    },
    ExecutionState.BUILDING_SANDBOX: {
        ExecutionEvent.SKILLS_MOUNTED: ExecutionState.MOUNTING_SKILLS,
    },
    ExecutionState.MOUNTING_SKILLS: {ExecutionEvent.EXEC_STARTED: ExecutionState.EXECUTING},
    ExecutionState.EXECUTING: {ExecutionEvent.EXEC_FINISHED: ExecutionState.COLLECTING_TRACE},
    ExecutionState.COLLECTING_TRACE: {
        ExecutionEvent.TRACE_COLLECTED: ExecutionState.SUCCEEDED,
    },
}

_ABORT: Final[dict[ExecutionEvent, ExecutionState]] = {
    ExecutionEvent.FAIL: ExecutionState.FAILED,
    ExecutionEvent.TIMEOUT: ExecutionState.TIMEOUT,
    ExecutionEvent.CANCEL: ExecutionState.CANCELLED,
}


class TransitionRecord(BaseModel):
    from_state: ExecutionState
    event: ExecutionEvent
    to_state: ExecutionState
    at: float
    reason: str | None = None


class InvalidTransition(Exception):
    def __init__(self, state: ExecutionState, event: ExecutionEvent) -> None:
        super().__init__(f"invalid transition: {state} + {event}")
        self.state = state
        self.event = event


class ExecutionStateMachine:
    """Thread-confined FSM; caller serializes access if shared across tasks."""

    def __init__(self, execution_id: str, initial: ExecutionState = ExecutionState.CREATED) -> None:
        self.execution_id = execution_id
        self._state = initial
        self.history: list[TransitionRecord] = []

    @property
    def state(self) -> ExecutionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def available_events(self) -> frozenset[ExecutionEvent]:
        if self.is_terminal:
            return frozenset()
        return frozenset(_TRANSITIONS.get(self._state, {})) | frozenset(_ABORT)

    def can(self, event: ExecutionEvent) -> bool:
        return event in self.available_events()

    def fire(self, event: ExecutionEvent, *, reason: str | None = None, now: float | None = None) -> ExecutionState:
        if self.is_terminal:
            raise InvalidTransition(self._state, event)

        table = dict(_TRANSITIONS.get(self._state, {}))
        if self._state not in TERMINAL_STATES:
            table.update(_ABORT)

        if event not in table:
            raise InvalidTransition(self._state, event)

        import time

        prev = self._state
        self._state = table[event]
        self.history.append(
            TransitionRecord(
                from_state=prev,
                event=event,
                to_state=self._state,
                at=now if now is not None else time.time(),
                reason=reason,
            )
        )
        return self._state

    def snapshot(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "state": self._state.value,
            "terminal": self.is_terminal,
            "history": [r.model_dump(mode="json") for r in self.history],
        }
