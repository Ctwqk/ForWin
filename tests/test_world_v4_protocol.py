from __future__ import annotations

import pytest

from pydantic import ValidationError

from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    Belief,
    BeliefStatus,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    GapObserverState,
    GapStatus,
    KnowledgeGap,
    ObserverType,
    ReaderExperienceDelta,
    RevealEvent,
    TruthRelation,
    VisibilityState,
    WorldCompileRequest,
    WorldCompileResult,
    WorldDelta,
    WorldLine,
)


def test_world_delta_accepts_core_delta_kinds() -> None:
    deltas = [
        WorldDelta(
            delta_id=f"delta_{kind.value}",
            project_id="project-1",
            world_line_id="line_homeworld",
            delta_kind=kind,
            summary=f"{kind.value} delta",
            objective_story_time="Day 30",
            narrative_chapter=23,
            source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
        )
        for kind in [
            DeltaKind.VISIBLE,
            DeltaKind.OFFSCREEN,
            DeltaKind.HINT,
            DeltaKind.KNOWLEDGE,
            DeltaKind.REVEAL,
        ]
    ]

    assert [delta.delta_kind for delta in deltas] == [
        DeltaKind.VISIBLE,
        DeltaKind.OFFSCREEN,
        DeltaKind.HINT,
        DeltaKind.KNOWLEDGE,
        DeltaKind.REVEAL,
    ]


def test_belief_separates_truth_relation_from_belief_status() -> None:
    truth_relations = [
        TruthRelation.TRUE,
        TruthRelation.FALSE,
        TruthRelation.PARTIAL,
        TruthRelation.UNKNOWN,
    ]
    statuses = [
        BeliefStatus.ACTIVE,
        BeliefStatus.OUTDATED,
        BeliefStatus.MANIPULATED,
        BeliefStatus.SUSPECTED,
        BeliefStatus.DISPUTED,
        BeliefStatus.CONFIRMED,
        BeliefStatus.REJECTED,
    ]

    beliefs = [
        Belief(
            belief_id=f"belief_{truth_relation.value}_{status.value}",
            holder_type=ObserverType.CHARACTER,
            holder_id="protagonist",
            proposition="母星仍然安全",
            truth_relation=truth_relation,
            confidence=0.45,
            belief_status=status,
            evidence_sources=["chapter:21:normal_comms"],
            created_at_chapter=21,
            last_updated_at_chapter=23,
        )
        for truth_relation in truth_relations
        for status in statuses
    ]

    assert len(beliefs) == len(truth_relations) * len(statuses)
    assert {belief.truth_relation for belief in beliefs} == set(truth_relations)
    assert {belief.belief_status for belief in beliefs} == set(statuses)


def test_knowledge_gap_tracks_homeworld_siege_observer_sequence() -> None:
    gap = KnowledgeGap(
        gap_id="gap_homeworld_siege",
        project_id="project-1",
        objective_truth="Day 30 父亲在母星被围",
        happened_at_story_time="Day 30",
        related_world_line_id="line_homeworld_siege",
        observer_states={
            "reader": GapObserverState(
                observer_type=ObserverType.READER,
                observer_id="reader",
                visibility=VisibilityState.HINTED,
                cognition_state="hinted",
                first_relevant_chapter=22,
                last_updated_chapter=22,
            ),
            "protagonist": GapObserverState(
                observer_type=ObserverType.CHARACTER,
                observer_id="protagonist",
                visibility=VisibilityState.SUSPECTED,
                cognition_state="suspected",
                first_relevant_chapter=23,
                last_updated_chapter=23,
            ),
        },
        narrative_function="延迟母星危机 reveal，制造殖民地与母星双线压力",
        planned_closure="chapter:28",
        maximum_safe_delay=7,
        fairness_requirements=["第22章必须有通讯异常", "第23章必须有旧部呼号"],
        status=GapStatus.OPEN,
    )

    assert gap.observer_states["reader"].visibility == VisibilityState.HINTED
    assert gap.observer_states["protagonist"].visibility == VisibilityState.SUSPECTED
    assert gap.status == GapStatus.OPEN

    closed_gap = gap.model_copy(update={"status": GapStatus.CLOSED})
    assert closed_gap.status == GapStatus.CLOSED
    assert gap.status == GapStatus.OPEN


def test_compile_dtos_group_extracted_and_approved_changes() -> None:
    world_line = WorldLine(
        world_line_id="line_homeworld_siege",
        project_id="project-1",
        line_type="hidden_parallel_line",
        title="母星围困线",
        objective_state_summary="敌方舰队正在切断母星通讯",
    )
    world_delta = WorldDelta(
        delta_id="delta_cut_array_3",
        project_id="project-1",
        world_line_id=world_line.world_line_id,
        delta_kind=DeltaKind.OFFSCREEN,
        summary="敌方切断第三通讯阵列",
        objective_story_time="Day 32",
        narrative_chapter=23,
        source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
    )
    reveal = RevealEvent(
        reveal_event_id="reveal_static_callsign",
        project_id="project-1",
        related_gap_id="gap_homeworld_siege",
        reveal_to_reader=True,
        reveal_to_characters=["protagonist"],
        reveal_method="hint",
        from_state=VisibilityState.HIDDEN,
        to_state=VisibilityState.HINTED,
        narrative_function="公平暗示母星异常",
    )
    reader_delta = ReaderExperienceDelta(
        reader_experience_delta_id="reader_exp_ch23_hint",
        project_id="project-1",
        chapter_number=23,
        reader_state_before="hidden",
        reader_state_after="hinted",
        cognition_transition="hidden -> hinted",
        payoff_type="short_term_hint",
        reward_tags=["mystery"],
        emotional_effect="不安与追问",
        next_desire="乱码通讯到底来自谁",
    )
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        world_deltas=[world_delta],
        reveal_events=[reveal],
        reader_experience_deltas=[reader_delta],
    )
    approved = ApprovedWorldChangeSet.from_extracted(
        extracted,
        approved_by=["WorldDeltaReviewer", "RevealReviewer", "ReaderCognitionReviewer"],
    )
    request = WorldCompileRequest(
        project_id="project-1",
        chapter_number=23,
        approved_changes=approved,
        review_verdict_id="review-23",
    )
    result = WorldCompileResult(
        project_id="project-1",
        chapter_number=23,
        compiler_run_id="compile-23",
        committed=True,
        world_delta_ids=["delta_cut_array_3"],
        reveal_event_ids=["reveal_static_callsign"],
        reader_experience_delta_ids=["reader_exp_ch23_hint"],
        snapshot_id="snapshot-23",
    )

    assert request.approved_changes.approved_by == [
        "WorldDeltaReviewer",
        "RevealReviewer",
        "ReaderCognitionReviewer",
    ]
    assert result.committed is True
    assert result.snapshot_id == "snapshot-23"


def test_world_delta_rejects_unknown_delta_kind() -> None:
    with pytest.raises(ValidationError):
        WorldDelta(
            delta_id="bad",
            project_id="project-1",
            world_line_id="line-1",
            delta_kind="teaser",
            summary="unsupported",
            source=DeltaSource(source_type=DeltaSourceType.CHARACTER_ACTION),
        )
