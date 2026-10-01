"""FSM transition tests."""

import pytest

from skill_runtime.state_machine import (
    TERMINAL_STATES,
    ExecutionEvent,
    ExecutionState,
    ExecutionStateMachine,
    InvalidTransition,
)

HAPPY = [
    (ExecutionEvent.RESOLVE, ExecutionState.RESOLVING_SKILLS),
    (ExecutionEvent.ENV_READY, ExecutionState.PREPARING_ENV),
    (ExecutionEvent.SANDBOX_READY, ExecutionState.BUILDING_SANDBOX),
    (ExecutionEvent.SKILLS_MOUNTED, ExecutionState.MOUNTING_SKILLS),
    (ExecutionEvent.EXEC_STARTED, ExecutionState.EXECUTING),
    (ExecutionEvent.EXEC_FINISHED, ExecutionState.COLLECTING_TRACE),
    (ExecutionEvent.TRACE_COLLECTED, ExecutionState.SUCCEEDED),
]


def test_happy_path_walks_full_pipeline():
    fsm = ExecutionStateMachine("exec_1")
    assert fsm.state is ExecutionState.CREATED
    for event, expected in HAPPY:
        assert fsm.fire(event) is expected
    assert fsm.is_terminal
    assert len(fsm.history) == len(HAPPY)


def test_invalid_transition_raises():
    fsm = ExecutionStateMachine("exec_2")
    with pytest.raises(InvalidTransition):
        fsm.fire(ExecutionEvent.EXEC_STARTED)


def test_abort_from_any_non_terminal_state():
    for stop_after, abort_event, terminal in [
        (0, ExecutionEvent.CANCEL, ExecutionState.CANCELLED),
        (2, ExecutionEvent.FAIL, ExecutionState.FAILED),
        (4, ExecutionEvent.TIMEOUT, ExecutionState.TIMEOUT),
    ]:
        fsm = ExecutionStateMachine(f"exec_abort_{stop_after}")
        for event, _ in HAPPY[:stop_after]:
            fsm.fire(event)
        assert fsm.fire(abort_event) is terminal
        assert fsm.is_terminal
        assert terminal in TERMINAL_STATES


def test_no_events_after_terminal():
    fsm = ExecutionStateMachine("exec_term")
    fsm.fire(ExecutionEvent.RESOLVE)
    fsm.fire(ExecutionEvent.FAIL)
    assert fsm.available_events() == frozenset()
    with pytest.raises(InvalidTransition):
        fsm.fire(ExecutionEvent.RESOLVE)


def test_can_predicate():
    fsm = ExecutionStateMachine("exec_can")
    assert fsm.can(ExecutionEvent.RESOLVE)
    assert not fsm.can(ExecutionEvent.EXEC_STARTED)
    assert fsm.can(ExecutionEvent.CANCEL)
