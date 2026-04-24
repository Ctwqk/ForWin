from __future__ import annotations

from forwin.protocol.world_v4 import (
    Belief,
    BeliefStatus,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    KnowledgeGap,
    ReaderExperienceDelta,
    RevealEvent,
    TruthRelation,
    VisibilityState,
    WorldDelta,
)
from forwin.protocol.writer import WriterOutput


def test_writer_output_keeps_legacy_payload_defaults() -> None:
    output = WriterOutput(
        chapter_number=1,
        title="第一章",
        body="正文",
        end_of_chapter_summary="摘要",
    )

    assert output.world_deltas == []
    assert output.belief_updates == []
    assert output.knowledge_gap_updates == []
    assert output.reveal_events == []
    assert output.reader_experience_deltas == []
    assert output.observer_visibility_updates == {}
    assert output.must_preserve_facts == []
    assert output.must_not_reveal_violations == []


def test_writer_output_accepts_optional_v4_self_report_fields() -> None:
    output = WriterOutput(
        project_id="project-1",
        chapter_number=23,
        title="乱码呼号",
        body="正文里只有乱码通讯和父亲旧部呼号。",
        end_of_chapter_summary="主角修复防线时收到异常通讯。",
        world_deltas=[
            WorldDelta(
                delta_id="delta_hint_callsign",
                project_id="project-1",
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="乱码通讯中出现父亲旧部呼号",
                narrative_chapter=23,
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            )
        ],
        belief_updates=[
            Belief(
                belief_id="belief_protagonist_homeworld_abnormal",
                holder_type="character",
                holder_id="protagonist",
                proposition="母星通讯异常可能不是距离导致",
                truth_relation=TruthRelation.PARTIAL,
                confidence=0.45,
                belief_status=BeliefStatus.SUSPECTED,
            )
        ],
        knowledge_gap_updates=[
            KnowledgeGap(
                gap_id="gap_homeworld_siege",
                project_id="project-1",
                objective_truth="父亲在母星被围",
                status="hinted",
            )
        ],
        reveal_events=[
            RevealEvent(
                reveal_event_id="reveal_static_callsign",
                project_id="project-1",
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_method="hint",
                from_state=VisibilityState.HIDDEN,
                to_state=VisibilityState.HINTED,
            )
        ],
        reader_experience_deltas=[
            ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch23",
                project_id="project-1",
                chapter_number=23,
                cognition_transition="hidden -> hinted",
                payoff_type="short_term_hint",
                reward_tags=["mystery"],
            )
        ],
        observer_visibility_updates={"reader": "hidden -> hinted"},
        must_preserve_facts=["殖民地防线已修复"],
        must_not_reveal_violations=[],
    )

    assert output.world_deltas[0].delta_kind == DeltaKind.HINT
    assert output.belief_updates[0].belief_status == BeliefStatus.SUSPECTED
    assert output.knowledge_gap_updates[0].status == "hinted"
    assert output.reveal_events[0].to_state == VisibilityState.HINTED
    assert output.reader_experience_deltas[0].reward_tags == ["mystery"]
    assert output.observer_visibility_updates["reader"] == "hidden -> hinted"
