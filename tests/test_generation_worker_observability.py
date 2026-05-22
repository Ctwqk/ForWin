from __future__ import annotations

from forwin.governance import DecisionEventType, ensure_decision_event_type


def test_generation_worker_decision_event_types_are_registered() -> None:
    assert ensure_decision_event_type("generation_worker_claimed") == (
        DecisionEventType.GENERATION_WORKER_CLAIMED
    )
    assert ensure_decision_event_type("generation_worker_reclaimed") == (
        DecisionEventType.GENERATION_WORKER_RECLAIMED
    )
    assert ensure_decision_event_type("generation_worker_heartbeat_failed") == (
        DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED
    )
    assert ensure_decision_event_type("generation_worker_execution_failed") == (
        DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED
    )
