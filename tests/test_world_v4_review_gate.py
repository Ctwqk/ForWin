from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import (
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    ReaderExperienceDelta,
    WorldDelta,
)
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.reviewer_v4.gate import V4ReviewGate


def _intent() -> ChapterWorldDeltaIntent:
    return ChapterWorldDeltaIntent(
        intent_id="chapter_23_intent",
        project_id="project-1",
        chapter_number=23,
        visible_delta_intents=["殖民地防线修复"],
        hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
        must_not_reveal=["father_sieged"],
        expected_observer_state_changes={
            "reader": "hidden -> hinted",
            "protagonist": "unknown -> suspected",
        },
    )


def test_gate_fails_when_protagonist_acts_on_unknown_hidden_truth() -> None:
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        world_deltas=[
            WorldDelta(
                delta_id="delta_magic_rescue",
                project_id="project-1",
                world_line_id="line_colony_defense",
                delta_kind=DeltaKind.VISIBLE,
                summary="主角立刻决定返航救父",
                source=DeltaSource(source_type=DeltaSourceType.CHARACTER_ACTION),
            )
        ],
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_intent(),
        chapter_body="主角忽然意识到父亲被围，立刻决定返航救父。",
    )

    assert verdict.passed is False
    assert any(issue.failure_type == "character_omniscience" for issue in verdict.issues)


def test_gate_fails_world_delta_without_source_type() -> None:
    source = DeltaSource.model_construct(source_type="")
    bad_delta = WorldDelta.model_construct(
        delta_id="delta_homeworld_falls",
        project_id="project-1",
        world_line_id="line_homeworld_siege",
        delta_kind=DeltaKind.OFFSCREEN,
        summary="母星突然沦陷",
        source=source,
    )
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        world_deltas=[bad_delta],
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_intent(),
        chapter_body="母星突然沦陷。",
    )

    assert verdict.passed is False
    assert any(issue.failure_type == "missing_delta_source" for issue in verdict.issues)


def test_gate_fails_early_reveal_against_must_not_reveal() -> None:
    extracted = ExtractedWorldChangeSet(project_id="project-1", chapter_number=23)

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_intent(),
        chapter_body="通讯终于接通，父亲明确说自己已经在母星被围。",
    )

    assert verdict.passed is False
    assert any(issue.failure_type == "early_reveal" for issue in verdict.issues)


def test_gate_warns_when_promise_debt_grows_without_payoff_plan() -> None:
    extracted = ExtractedWorldChangeSet(project_id="project-1", chapter_number=23)

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_intent().model_copy(update={"reader_experience_intents": []}),
        chapter_body="本章继续制造新问题，没有关闭旧问题。",
        promise_debt_count=4,
    )

    assert verdict.passed is True
    assert any(issue.failure_type == "unpaid_promise_debt" for issue in verdict.issues)


def test_gate_passes_chapter_23_hint_with_local_reader_experience() -> None:
    extracted = ExtractedWorldChangeSet(
        project_id="project-1",
        chapter_number=23,
        world_deltas=[
            WorldDelta(
                delta_id="delta_hint_callsign",
                project_id="project-1",
                world_line_id="line_homeworld_siege",
                delta_kind=DeltaKind.HINT,
                summary="乱码通讯与父亲旧部呼号",
                source=DeltaSource(source_type=DeltaSourceType.INFORMATION_SPREAD),
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
    )

    verdict = V4ReviewGate().review(
        extracted,
        chapter_intent=_intent(),
        chapter_body="防线修复后，通讯台传出乱码和父亲旧部呼号。",
    )

    assert verdict.passed is True
    assert verdict.approved_changes is not None
    assert verdict.approved_changes.world_deltas[0].delta_id == "delta_hint_callsign"


def test_review_protocol_accepts_v4_repair_and_gate_metadata() -> None:
    repair = RepairInstruction(
        repair_scope="world_model",
        failure_type="early_reveal",
        must_fix=["删除提前揭示父亲被围的句子"],
        must_preserve=["乱码通讯", "父亲旧部呼号"],
        must_not_reveal=["father_sieged"],
        required_hint_patch={"gap_homeworld_siege": ["乱码通讯", "父亲旧部呼号"]},
        required_payoff_patch={"reader": "hidden -> hinted"},
        evidence_refs=["chapter_body:父亲被围"],
    )
    verdict = ReviewVerdict(
        verdict="fail",
        issues=[],
        repair_instruction=repair,
        extracted_actuals={"world_deltas": ["delta_ch23_hint"]},
        approved_delta_refs=[],
        rejected_delta_refs=["delta_early_reveal"],
        compiler_gate_status="blocked",
    )

    assert verdict.repair_instruction is not None
    assert verdict.repair_instruction.repair_scope == "world_model"
    assert verdict.repair_instruction.failure_type == "early_reveal"
    assert verdict.repair_instruction.must_not_reveal == ["father_sieged"]
    assert verdict.compiler_gate_status == "blocked"
    assert verdict.rejected_delta_refs == ["delta_early_reveal"]
