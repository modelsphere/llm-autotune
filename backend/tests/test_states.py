import pytest

from app.control.orchestrator.states import ALLOWED_TRANSITIONS, can_transition
from app.db.models import TERMINAL_RUN_STATES, RunStatus


def test_happy_path_is_legal():
    path = [
        RunStatus.PENDING,
        RunStatus.LAUNCHING,
        RunStatus.WAITING_READY,
        RunStatus.HEALTH_CHECK,
        RunStatus.BENCHING,
        RunStatus.SUCCEEDED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        assert can_transition(current, target)


def test_terminal_states_are_terminal():
    for state in TERMINAL_RUN_STATES:
        assert ALLOWED_TRANSITIONS[state] == set()


def test_every_live_state_can_be_killed():
    for state, targets in ALLOWED_TRANSITIONS.items():
        if state not in TERMINAL_RUN_STATES and state != RunStatus.PENDING:
            assert RunStatus.KILLED in targets, f"{state} must be killable (window cutoff)"


def test_fast_ready_skips_waiting():
    # instantly-ready services jump launching -> health_check directly
    assert can_transition(RunStatus.LAUNCHING, RunStatus.HEALTH_CHECK)


def test_no_resurrection():
    assert not can_transition(RunStatus.SUCCEEDED, RunStatus.PENDING)
    assert not can_transition(RunStatus.FAILED, RunStatus.LAUNCHING)


def test_accepts_raw_strings():
    assert can_transition("pending", "launching")
    with pytest.raises(ValueError):
        can_transition("pending", "not-a-state")
