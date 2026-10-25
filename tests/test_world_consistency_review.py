from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

from forwin.protocol.review import RepairVerification, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.decision.rules.final_residual import FinalResidualPolicy
from forwin.review.decision.rules.repair_v2 import decide_repair_v2
from forwin.review.decision.rules.review_outcome import decide_review_outcome
from forwin.review.decision.types import DecisionInput, PlanLayerHealth
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.world_consistency import WorldConsistencyReviewer, _named
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.prompt_core.world_repair import (
    DEFAULT_WORLD_REPAIR_FIX,
    PROTECTION_REPAIR_FIXES,
    READER_OBSERVER_LABEL,
    WORLD_REPAIR_FIXES,
    protection_repair_fix,
)
from tests.chapter_delta_helpers import chapter_delta
from tests.world_gate_helpers import _knowledge, _seed_world, _session_factory, _World
from tests.test_world_gate_v6 import (
    _day_of_moves,
    _move,
    _time,
)

from tests.chapter_delta_helpers import BODY, draft_review as _draft_review, world_review_output as _output


def _hero_walks(world: _World, amount: float, unit: str) -> dict[str, Any]:
    # loc 1 -> loc 2 is a 2 h road: 120 minutes; the 20% tolerance allows 96.
    return {**_time(amount, unit), "movements": [_move(world.hero, world.loc(1), world.loc(2))]}


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


def test_extraction_whose_every_fact_lost_its_evidence_is_error() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)
    dropped = {
        "severity": "warning", "code": "evidence_not_in_body", "path": "movements[0]",
        "message": "Evidence is not a passage from the chapter body.",
    }
    output = _output(world, meta={"chapter_delta_issues": [dropped]})

    verdict = _draft_review(Session, world, output)
    kept = _world_review(Session, world, _output(world, _hero_walks(world, 2, "时"), meta={
        "chapter_delta_issues": [{**dropped, "path": "movements[1]"}],
    }))

    assert verdict.verdict == "fail"
    assert _codes(verdict) == [("evidence_not_in_body", "warning"), ("chapter_delta_unverified", "error")]
    issue = verdict.issues[-1]
    assert (issue.target_scope, issue.blocking) == ("draft", True)
    assert issue.suggested_fix == WORLD_REPAIR_FIXES["chapter_delta_unverified"]
    decision = _repair_decision(verdict)
    assert (decision.outcome, decision.sub_action["scope"]) == ("local_repair", "draft")
    # A fact that kept its evidence leaves the extraction usable.
    assert "chapter_delta_unverified" not in [code for code, _severity in _codes(kept)]
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


def test_unreachable_movement_names_mover_and_destination() -> None:
    engine, Session = _session_factory()
    world = _seed_world(Session)

    # loc 5 has no edges; the gate's message ends the destination with a colon.
    verdict = _draft_review(Session, world, _output(world, _day_of_moves(_move(world.hero, world.loc(1), world.loc(5)))))

    assert _codes(verdict) == [("movement_unreachable", "error")]
    issue = verdict.issues[0]
    for node_id in (world.hero, world.loc(1), world.loc(5)):
        assert node_id not in issue.description
        assert not any(node_id in item for item in verdict.repair_instruction.must_fix)
    assert issue.entity_names == ["甲", "site-1", "site-5"]
    assert issue.suggested_fix == WORLD_REPAIR_FIXES["movement_unreachable"]
    engine.dispose()


def test_hidden_fact_contradiction_names_only_the_topic() -> None:
    # R44: repair identifies the topic without teaching the writer the hidden truth.
    engine, Session = _session_factory()
    world = _seed_world(Session)

    verdict = _draft_review(Session, world, _output(world, {"knowledge_events": [_knowledge(
        world.rival, world.fact("fact-public"), state="believes", contradicts_ref=world.fact("fact-secret"),
    )]}))

    assert _codes(verdict) == [("contradicts_hidden_fact", "error")]
    issue = verdict.issues[0]
    assert "涉及甲的未揭示真相" in issue.description
    assert "proposition-public" not in issue.description
    assert "proposition-secret" not in issue.description
    assert "fact:" not in issue.description
    assert any("涉及甲的未揭示真相" in item for item in verdict.repair_instruction.must_fix)
    assert all("proposition-secret" not in item for item in verdict.repair_instruction.must_fix)
    assert issue.original_result["fact_propositions"][world.fact("fact-secret")] == "proposition-secret"
    assert issue.entity_names == []
    assert issue.suggested_fix == WORLD_REPAIR_FIXES["contradicts_hidden_fact"]
    engine.dispose()


def _commit_protection(Session, world: _World, **spec: Any) -> str:
    from forwin.book_state.projection import BookStateProjection
    from forwin.canon.admission import CanonAdmissionService
    from forwin.planning.beat_sheet.types import ProtectionSpec

    with Session.begin() as session:
        CanonAdmissionService().commit_plan_constraints(
            session=session, project_id=world.project_id, beat_sheet_id=f"sheet-{spec['kind']}",
            creates=[ProtectionSpec(until_chapter=0, **spec)], releases=[],
        )
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        return next(node.id for node in runtime.narrative.nodes_by_id.values()
                    if node.metadata.get("beat_sheet_id") == f"sheet-{spec['kind']}")


def test_repair_fix_text_has_no_fact_proposition() -> None:
    # R44: a must_not_know repair names the observer and the secret's topic, never its proposition.
    engine, Session = _session_factory()
    world = _seed_world(Session)
    protection_id = _commit_protection(Session, world, ref=world.rival, kind="must_not_know",
                                       observer_ref=world.rival, fact_ref=world.fact("fact-secret"))

    verdict = _draft_review(Session, world, _output(world, {"knowledge_events": [
        _knowledge(world.rival, world.fact("fact-secret"), evidence="乙 overhears it"),
    ]}))

    assert _codes(verdict) == [("protection_violated", "error")]
    issue = verdict.issues[0]
    assert issue.suggested_fix == protection_repair_fix(
        "must_not_know", {"observer": "乙", "topic": "涉及甲的未揭示真相"},
    )
    assert "乙" in issue.suggested_fix and "涉及甲的未揭示真相" in issue.suggested_fix
    writer_text = [issue.description, issue.suggested_fix, *verdict.repair_instruction.must_fix]
    assert all("proposition-secret" not in text and "fact:" not in text for text in writer_text)
    assert all(world.rival not in text and protection_id not in text for text in writer_text)
    assert any(issue.suggested_fix in item for item in verdict.repair_instruction.must_fix)
    assert issue.evidence_refs == [f"review_constraint:{protection_id}"]
    boundary = issue.original_result["boundary"]
    assert {key: boundary[key] for key in ("protection_node_id", "kind", "observer_ref", "fact_ref", "impact_level",
                                           "evidence")} == {
        "protection_node_id": protection_id, "kind": "must_not_know", "observer_ref": world.rival,
        "fact_ref": world.fact("fact-secret"), "impact_level": "major", "evidence": "乙 overhears it",
    }
    assert issue.original_result["fact_propositions"] == {world.fact("fact-secret"): "proposition-secret"}
    engine.dispose()


def test_milestone_ref_from_the_beat_sheet_names_the_entity_only() -> None:
    from sqlalchemy import select

    from forwin.models.project import ArcPlanVersion
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    engine, Session = _session_factory()
    world = _seed_world(Session)
    kill_rival = {"entity_changes": [
        {"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"},
    ]}
    assert _codes(_world_review(Session, world, _output(world, kill_rival))) == []
    with Session.begin() as session:
        arc = session.scalars(select(ArcPlanVersion).where(
            ArcPlanVersion.project_id == world.project_id, ArcPlanVersion.arc_number == 1)).one()
        ArcBeatSheetRepository(session).create(world.project_id, arc.id, ArcBeatSheetData(
            protagonist_ref=world.hero, phases=[], band_breakdown=[], milestones=[{
                "id": "duel", "description": "description-duel", "target_range": [3, 5],
                "required_refs": [world.rival], "hard": True,
            }],
        ), source="llm", reason="milestone")

    verdict = _world_review(Session, world, _output(world, kill_rival))

    assert _codes(verdict) == [("milestone_ref_ended", "error")]
    issue = verdict.issues[0]
    assert issue.suggested_fix == protection_repair_fix("milestone", {"name": "乙"})
    assert "乙" in issue.description and world.rival not in issue.description
    assert "description-duel" not in issue.description + issue.suggested_fix
    assert issue.evidence_refs == [f"node:{world.rival}"]
    assert (issue.original_result["boundary"]["milestone_ref"], issue.original_result["boundary"]["impact_level"]) == (
        world.rival, "critical",
    )
    engine.dispose()


def test_only_endings_that_contradict_the_plan_are_milestone_violations() -> None:
    from sqlalchemy import select

    from forwin.models.project import ArcPlanVersion
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    engine, Session = _session_factory()
    world = _seed_world(Session)
    kill_rival = _output(world, {"entity_changes": [
        {"ref": world.rival, "field": "alive_status", "new_value": "dead", "evidence": "e"},
    ]})

    def review_with_sheet(*milestone_ranges: tuple[int, int]) -> list[tuple[str, str]]:
        with Session.begin() as session:
            arc = session.scalars(select(ArcPlanVersion).where(
                ArcPlanVersion.project_id == world.project_id, ArcPlanVersion.arc_number == 1)).one()
            ArcBeatSheetRepository(session).create(world.project_id, arc.id, ArcBeatSheetData(
                protagonist_ref=world.hero, phases=[], band_breakdown=[],
                milestones=[{"id": f"m-{start}", "description": "d", "target_range": [start, end],
                             "required_refs": [world.rival], "hard": True} for start, end in milestone_ranges],
                # The arc ends with the rival dead.
                exit_state=[{"ref": world.rival, "field": "state.alive_status", "value": "dead"},
                            {"ref": world.hero, "field": "location_id", "value": world.loc(2)}],
            ), source="llm", reason="test")
        return _codes(_world_review(Session, world, kill_rival))

    # The exit state's own ending is the arc's goal; a milestone whose range includes the chapter may be under way.
    assert review_with_sheet() == []
    assert review_with_sheet((1, 3)) == []
    # A strictly future hard milestone still needs the rival.
    assert review_with_sheet((1, 3), (2, 4)) == [("milestone_ref_ended", "error")]
    verdict = _world_review(Session, world, kill_rival)
    assert verdict.issues[0].original_result["boundary"]["impact_level"] == "critical"
    # The hero's exit state is not an ending, so ending the hero still counts.
    kill_hero = _output(world, {"entity_changes": [
        {"ref": world.hero, "field": "alive_status", "new_value": "dead", "evidence": "e"},
    ]})
    assert _codes(_world_review(Session, world, kill_hero)) == [("milestone_ref_ended", "error")]
    # Inside a milestone's range a protection still guards the ref.
    review_with_sheet((1, 3))
    _commit_protection(Session, world, ref=world.rival, kind="must_be_alive")
    assert _codes(_world_review(Session, world, kill_rival)) == [("protection_violated", "error")]
    engine.dispose()


def test_protection_fix_covers_every_kind_and_masks_missing_names() -> None:
    from forwin.book_state.narrative import PROTECTION_KINDS
    from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL, UNNAMED_FACT_TOPIC

    assert set(PROTECTION_REPAIR_FIXES) == {*PROTECTION_KINDS, "milestone"}
    for kind in PROTECTION_REPAIR_FIXES:
        fix = protection_repair_fix(kind, {"name": "Ann", "observer": "Bo", "topic": "topic-1"})
        assert "{" not in fix and ("Ann" in fix or ("Bo" in fix and "topic-1" in fix))
        assert UNNAMED_ACTOR_LABEL in protection_repair_fix(kind, {}) or UNNAMED_FACT_TOPIC in protection_repair_fix(
            kind, {"observer": "Bo"})
    assert READER_OBSERVER_LABEL in protection_repair_fix("must_not_know", {"observer": READER_OBSERVER_LABEL})
    assert protection_repair_fix("unknown", {"name": "Ann"}) == DEFAULT_WORLD_REPAIR_FIX


def test_refs_before_trailing_punctuation_get_their_names() -> None:
    names = {"node-1": "Ann", "site-1": "Harbor", "fact:fact-1": "the vault is empty"}

    assert _named("node-1 reaches site-1: blocked. Then site-1.", names) == (
        "Ann reaches Harbor: blocked. Then Harbor.", ["Ann", "Harbor"],
    )
    assert _named("fact:fact-1: known", names) == ("「未揭示真相」: known", [])
    assert _named("fact:fact-1: known", names, {"fact:fact-1": ["Ann"]}) == (
        "「涉及Ann的未揭示真相」: known", [],
    )
    assert _named("fact:unknown: kept", names) == ("「未揭示真相」: kept", [])
    assert _named("unknown-1: kept", names) == ("unknown-1: kept", [])


# Gate errors the writer cannot fix by rewriting prose keep the generic fix; the reader check
# applies only to world tick deltas, which never reach the writer.
_DEFAULT_FIX_GATE_CODES = {"delta_not_allowed_for_canon", "world_engine_reader_knowledge", "invalid_protection"}
# Broken protections and milestone refs take their fix from the violation's kind (PROTECTION_REPAIR_FIXES).
_BOUNDARY_GATE_CODES = {"protection_violated", "milestone_ref_ended"}


def _gate_error_codes() -> set[str]:
    """Every error code forwin/book_state/reviewer.py can emit, read from its source."""
    source = Path(__file__).resolve().parents[1] / "forwin" / "book_state" / "reviewer.py"
    codes: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if callee == "_issue" and len(node.args) >= 2:
            severity, code = node.args[0], node.args[1]
        elif callee == "BookStateReviewIssue":
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            severity, code = keywords.get("severity"), keywords.get("code")
        else:
            continue
        if isinstance(severity, ast.Constant) and severity.value == "error" and isinstance(code, ast.Constant):
            codes.add(code.value)
    return codes


def test_every_gate_error_code_has_a_repair_fix() -> None:
    codes = _gate_error_codes()

    assert "movement_unreachable" in codes and "movement_total_too_fast" in codes
    assert codes - set(WORLD_REPAIR_FIXES) == _DEFAULT_FIX_GATE_CODES | _BOUNDARY_GATE_CODES
    # The table names no code the gate has stopped emitting.
    assert set(WORLD_REPAIR_FIXES) - codes == {"chapter_delta_missing", "chapter_delta_unverified"}


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
