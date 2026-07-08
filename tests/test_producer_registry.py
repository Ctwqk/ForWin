from __future__ import annotations

from forwin.canon_quality.continuity_adapter import signals_from_continuity_issues
from forwin.canon_quality.gate import _EXPANDED_FATAL_SIGNAL_TYPES, _FATAL_ONLY_SIGNAL_TYPES, evaluate_canon_admission
from forwin.canon_quality.producer_registry import SIGNAL_PRODUCERS
from forwin.protocol.review import ContinuityIssue


def test_every_fatal_signal_type_has_registered_producer() -> None:
    fatal_types = _FATAL_ONLY_SIGNAL_TYPES | _EXPANDED_FATAL_SIGNAL_TYPES

    assert fatal_types <= set(SIGNAL_PRODUCERS)
    assert "already_dead_character_resurrected" not in fatal_types
    assert "character_resurrection" not in fatal_types
    assert "character_dead_alive" not in fatal_types
    assert "character_teleport" not in fatal_types


def test_continuity_adapter_emits_dead_character_resurrection_and_gate_blocks() -> None:
    signals = signals_from_continuity_issues(
        project_id="p1",
        chapter_number=9,
        issues=[
            ContinuityIssue(
                rule_name="dead_character_active",
                severity="error",
                description="已死亡角色仍作为行动者。",
                entity_names=["林若"],
                evidence_refs=["event=林若开门", "entity=林若"],
            )
        ],
    )
    gate = evaluate_canon_admission(
        project_id="p1",
        chapter_number=9,
        signals=signals,
        mode="fatal_only",
    )

    assert [signal.signal_type for signal in signals] == ["dead_character_resurrection"]
    assert gate.commit_allowed is False
    assert gate.deterministic_issue_refs == [signals[0].signal_id]


def test_continuity_adapter_emits_closed_thread_reopened_and_gate_blocks() -> None:
    signals = signals_from_continuity_issues(
        project_id="p1",
        chapter_number=12,
        issues=[
            ContinuityIssue(
                rule_name="thread_already_closed",
                severity="warning",
                description="已关闭情节线被重新推进。",
                evidence_refs=["thread=旧港逃亡"],
            )
        ],
    )
    gate = evaluate_canon_admission(
        project_id="p1",
        chapter_number=12,
        signals=signals,
        mode="fatal_only",
    )

    assert [signal.signal_type for signal in signals] == ["closed_thread_reopened"]
    assert signals[0].severity == "error"
    assert gate.commit_allowed is False
