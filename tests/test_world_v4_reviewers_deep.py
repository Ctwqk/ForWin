from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import (
    Belief,
    BeliefStatus,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    ObserverType,
    TruthRelation,
    WorldDelta,
)
from forwin.reviewer_v4.gate import V4ReviewGate


def _chapter_23_intent() -> ChapterWorldDeltaIntent:
    return ChapterWorldDeltaIntent(
        intent_id="chapter_23_intent",
        project_id="project-1",
        chapter_number=23,
        hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
        must_not_reveal=["father_sieged"],
        expected_observer_state_changes={"protagonist": "unknown -> suspected"},
    )


def test_reviewers_warn_false_belief_without_evidence() -> None:
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        belief_updates=[
            Belief(
                belief_id="belief_distance_delay",
                holder_type=ObserverType.CHARACTER,
                holder_id="protagonist",
                proposition="通讯问题只是距离导致",
                truth_relation=TruthRelation.FALSE,
                belief_status=BeliefStatus.SUSPECTED,
                evidence_sources=[],
            )
        ],
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_chapter_23_intent(),
        chapter_body="主角仍以为通讯问题只是距离导致。",
    )

    issue = next(issue for issue in verdict.issues if issue.failure_type == "unsupported_false_belief")
    assert issue.severity == "warn"
    assert "required_belief_patch" in issue.repair_patch


def test_reveal_reviewer_blocks_reveal_before_ladder_step() -> None:
    intent = _chapter_23_intent().model_copy(
        update={"metadata": {"planned_reveal_chapter": 25}}
    )
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        world_deltas=[
            WorldDelta(
                delta_id="delta_direct_reveal",
                project_id="project-1",
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.REVEAL,
                summary="父亲明确说自己已经被围",
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
            )
        ],
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=intent,
        chapter_body="父亲明确说自己已经被围。",
    )

    issue = next(issue for issue in verdict.issues if issue.failure_type == "reveal_before_planned_chapter")
    assert verdict.passed is False
    assert issue.severity == "fail"


def test_world_delta_reviewer_warns_hidden_line_without_hint_plan() -> None:
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=24,
        world_deltas=[
            WorldDelta(
                delta_id="delta_hidden_line_advances",
                project_id="project-1",
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.OFFSCREEN,
                summary="幕后敌方舰队推进",
                source=DeltaSource(source_type=DeltaSourceType.FACTION_ACTION),
            )
        ],
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=ChapterWorldDeltaIntent(
            intent_id="ch24",
            project_id="project-1",
            chapter_number=24,
        ),
        chapter_body="幕后敌方舰队推进。",
    )

    issue = next(issue for issue in verdict.issues if issue.failure_type == "offscreen_without_reveal_plan")
    assert issue.severity == "warn"


def test_reader_cognition_reviewer_fails_band_without_increment() -> None:
    verdict = V4ReviewGate().review(
        ExtractedWorldChangeSet(project_id="project-1", chapter_number=24),
        chapter_intent=ChapterWorldDeltaIntent(
            intent_id="ch24",
            project_id="project-1",
            chapter_number=24,
        ),
        chapter_body="没有事件，也没有认知变化。",
        promise_debt_count=5,
    )

    issue = next(issue for issue in verdict.issues if issue.failure_type == "missing_chapter_increment")
    assert verdict.passed is False
    assert issue.severity == "fail"
