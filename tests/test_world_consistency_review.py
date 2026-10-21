from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import RepairVerification, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.decision.rules.final_residual import FinalResidualPolicy
from forwin.review.decision.rules.repair_v2 import decide_repair_v2
from forwin.review.decision.rules.review_outcome import decide_review_outcome
from forwin.review.decision.types import DecisionInput, PlanLayerHealth
from forwin.review.draft_service import DraftReviewService
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.world_consistency import WorldConsistencyReviewer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.writer.prompt_core.world_repair import DEFAULT_WORLD_REPAIR_FIX, WORLD_REPAIR_FIXES
from tests.chapter_delta_helpers import chapter_delta
from tests.test_world_gate_v6 import _move, _seed_world, _session_factory, _time, _World

BODY = "The hero walked the road."


def _output(world: _World, fields: dict[str, Any] | None = None, *, extracted: bool = True,
            meta: dict[str, Any] | None = None) -> WriterOutput:
    """A chapter-1 candidate with a current admission plan, as review sees it after admission."""
    output = WriterOutput(
        project_id=world.project_id, chapter_number=1, title="Chapter one", body=BODY,
        end_of_chapter_summary="The hero arrived.",
        chapter_delta=chapter_delta(BODY, **(fields or {})) if extracted else None,
    )
    plan = EntityAdmissionPlan(
        project_id=world.project_id, chapter_number=1,
        candidate_fingerprint=writer_output_admission_fingerprint(output),
    )
    return output.model_copy(update={"generation_meta": {
        **(meta or {}), "entity_admission_plan": plan.model_dump(mode="json"),
    }})


def _hero_walks(world: _World, amount: float, unit: str) -> dict[str, Any]:
    # loc 1 -> loc 2 is a 2 h road: 120 minutes; the 20% tolerance allows 96.
    return {**_time(amount, unit), "movements": [_move(world.hero, world.loc(1), world.loc(2))]}


def _draft_review(Session, world: _World, output: WriterOutput, *, goals: list[str] | None = None,
                  **service_kwargs: Any) -> ReviewVerdict:
    passing = ReviewVerdict(verdict="pass", issues=[])
    service = DraftReviewService(
        experience_review_enabled=False,
        lint_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
        **service_kwargs,
    )
    with Session() as session:
        return service.review(
            project_id=world.project_id,
            repo=StateRepository(session),
            context=ChapterContextPack(
                project_id=world.project_id, project_title="world", premise="p", genre="g",
                setting_summary="", chapter_number=1, chapter_plan_title="Chapter one",
                chapter_plan_one_line="The hero walks.", chapter_goals=list(goals or []),
            ),
            writer_output=output,
            continuity_checker=SimpleNamespace(check=lambda _project_id, _output: passing),
        )


def _world_review(Session, world: _World, output: WriterOutput) -> ReviewVerdict:
    with Session() as session:
        return WorldConsistencyReviewer().review(
            session=session, project_id=world.project_id, chapter_number=1,
            writer_output=output, travel_tolerance=0.2,
        )


def _codes(verdict: ReviewVerdict) -> list[tuple[str, str]]:
    return [(issue.rule_name, issue.severity) for issue in verdict.issues]


def _repair_decision(review: ReviewVerdict):
    return decide_repair_v2(DecisionInput(
        project_id="project-1", chapter_number=1, review=review, signals=[], open_obligations=[],
        attempts_completed=0, prior_scope_history=[], budget=None, target_total_chapters=0,
        plan_layer_health=PlanLayerHealth(),
    ))


def test_world_gate_error_fails_draft_with_must_fix() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    verdict = _draft_review(Session, world, _output(world, _hero_walks(world, 6, "刻")))

    assert verdict.verdict == "fail"
    assert _codes(verdict) == [("movement_too_fast", "error")]
    issue = verdict.issues[0]
    assert (issue.reviewer, issue.issue_type, issue.target_scope) == (
        "world_consistency", "world_consistency", "draft",
    )
    assert verdict.repair_instruction is not None
    assert verdict.repair_instruction.repair_scope == "draft"
    assert verdict.repair_instruction.failure_type == "continuity"
    assert any(issue.description in item for item in verdict.repair_instruction.must_fix)
    # The repair loop routes it to a local draft repair, not a chapter-plan patch.
    decision = _repair_decision(verdict)
    assert (decision.outcome, decision.sub_action["scope"]) == ("local_repair", "draft")
    engine.dispose()


def test_world_error_names_entities_and_gives_a_code_specific_fix() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    verdict = _draft_review(Session, world, _output(world, _hero_walks(world, 6, "刻")))

    issue = verdict.issues[0]
    # The writer sees the names its prompt uses, never BookState or map node ids.
    for name in ("甲", "site-1", "site-2"):
        assert name in issue.description
    for node_id in (world.hero, world.loc(1), world.loc(2)):
        assert node_id not in issue.description
        assert not any(node_id in item for item in verdict.repair_instruction.must_fix)
    assert issue.entity_names == ["甲", "site-1", "site-2"]
    assert issue.suggested_fix == WORLD_REPAIR_FIXES["movement_too_fast"]
    assert issue.suggested_fix != DEFAULT_WORLD_REPAIR_FIX
    # The raw gate message keeps the ids for audit.
    assert world.hero in str(issue.original_result["message"])
    assert issue.evidence_refs == [f"node:{world.hero}"]
    engine.dispose()


def test_world_warning_only_is_warn_not_fail() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    verdict = _draft_review(Session, world, _output(world, _hero_walks(world, 7, "刻")))

    assert verdict.verdict == "warn"
    assert _codes(verdict) == [("movement_fast_within_tolerance", "warning")]
    assert verdict.repair_instruction is None
    engine.dispose()


def test_extraction_issues_surface_as_warnings() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    extraction_issue = {
        "severity": "error", "code": "evidence_not_found", "path": "events[0]",
        "message": "evidence quote is not in the body",
    }

    verdict = _world_review(Session, world, _output(world, meta={"chapter_delta_issues": [extraction_issue]}))

    assert verdict.verdict == "warn"
    assert _codes(verdict) == [("evidence_not_found", "warning")]
    assert verdict.issues[0].description == "evidence quote is not in the body"
    assert verdict.issues[0].reviewer == "world_consistency"
    engine.dispose()


def test_missing_chapter_delta_is_error() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    outage = {
        "severity": "error", "code": "extraction_unavailable", "path": "",
        "message": "No LLM client is available to extract the chapter delta.",
    }
    output = _output(world, extracted=False, meta={"chapter_delta_issues": [outage]})

    # With goals to verify, the plan reviewer leaves the missing delta to world consistency.
    verdict = _draft_review(Session, world, output, goals=["Find the ledger"])

    assert verdict.verdict == "fail"
    assert _codes(verdict) == [("extraction_unavailable", "warning"), ("chapter_delta_missing", "error")]
    assert verdict.repair_instruction is not None
    assert verdict.repair_instruction.repair_scope == "draft"
    decision = _repair_decision(verdict)
    assert (decision.outcome, decision.sub_action["scope"]) == ("local_repair", "draft")
    engine.dispose()


def test_translation_errors_fail_and_translation_warnings_warn() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    unresolved = _world_review(Session, world, _output(world, {
        **_time(1, "日"), "movements": [_move("unknown-ref", world.loc(1), world.loc(2))],
    }))
    untargeted = _world_review(Session, world, _output(world, {
        "thread_beats": [{"beat_type": "advance", "evidence": "e"}],
    }))

    assert unresolved.verdict == "fail"
    assert ("unresolved_ref", "error") in _codes(unresolved)
    assert untargeted.verdict == "warn"
    assert _codes(untargeted) == [("untargeted_thread_beat", "warning")]
    engine.dispose()


def test_draft_review_passes_policy_tolerance_to_world_reviewer() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    calls: list[dict[str, Any]] = []

    class Recorder:
        def review(self, **kwargs: Any) -> ReviewVerdict:
            calls.append(kwargs)
            return ReviewVerdict(verdict="pass", issues=[])

    output = _output(world)
    _draft_review(Session, world, output, world_consistency_reviewer=Recorder(), travel_tolerance=0.35)
    _draft_review(Session, world, output, world_consistency_reviewer=Recorder(),
                  world_consistency_review_enabled=False)

    assert len(calls) == 1
    assert {key: value for key, value in calls[0].items() if key != "session"} == {
        "project_id": world.project_id, "chapter_number": 1, "writer_output": output,
        "travel_tolerance": 0.35,
    }
    assert calls[0]["session"] is not None
    engine.dispose()


def test_world_issue_type_is_registered_for_routing() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    missing = _world_review(Session, world, _output(world, extracted=False))
    engine.dispose()
    review = ReviewVerdict(verdict="fail", issues=missing.issues)
    decision = _repair_decision(review)

    assert _codes(missing) == [("chapter_delta_missing", "error")]
    assert issue_group_for_issue(issue_type="world_consistency") == "fact_conflict"
    assert (decision.outcome, decision.sub_action["scope"]) == ("local_repair", "draft")
    assert decide_review_outcome(DecisionInput(
        project_id="project-1", chapter_number=1, review=review, signals=[], open_obligations=[],
        attempts_completed=0, prior_scope_history=[], budget=None, target_total_chapters=0,
        plan_layer_health=PlanLayerHealth(),
    )).sub_action["review_action"] == "local_rewrite"
    residual = FinalResidualPolicy().evaluate(
        review=review,
        verification=RepairVerification(fixed_all_must_fix=True, preserved_all_must_preserve=True),
    )
    assert residual.reason == "hard-residual-issue:world_consistency"


def test_map_movement_module_is_gone() -> None:
    assert importlib.util.find_spec("forwin.review.map_movement") is None


def test_policy_defaults_include_world_consistency() -> None:
    assert RuntimePolicy.for_profile("standard").review.signals == (
        "experience", "lint", "world_consistency", "personality", "canon_quality", "publisher",
    )
    assert RuntimePolicy.for_profile("pulp").review.signals == ("lint", "world_consistency", "publisher")
