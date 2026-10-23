from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.models.project import ChapterPlan
from forwin.review.plan_reviewer import PlanContractReviewer
from tests.chapter_delta_helpers import plan_review_context as _context, plan_review_output as _output


@pytest.mark.parametrize("reported", [False, True])
def test_missing_binding_blocks_review(reported):
    plan = ChapterPlan(world_bindings_json=json.dumps([
        {"kind": "intent", "ref": "intent-1", "how": "Advance the delivery"},
    ]))
    output = _output(*[("bind:0", "missing")] if reported else [])
    review = PlanContractReviewer().review(_context([]), output, chapter_plan=plan)
    assert review.verdict == "fail"
    assert [(issue.rule_name, issue.severity, issue.blocking) for issue in review.issues] == [
        ("plan_binding_missing", "error", True),
    ]
    assert "target=bind:0" in review.issues[0].evidence_refs


def test_reviewer_reads_bindings_from_persisted_plan():
    plan = ChapterPlan(world_bindings_json='[{"kind":"event","ref":"event-1","how":"Witness"}]')
    session = SimpleNamespace(scalar=lambda query: plan)
    review = PlanContractReviewer().review(_context([]), _output(), session=session, project_id="p")
    assert review.verdict == "fail"
    assert review.issues[0].rule_name == "plan_binding_missing"


def test_completed_binding_passes_without_optional_directives():
    plan = ChapterPlan(world_bindings_json='[{"kind":"event","ref":"event-1","how":"Witness"}]',
                       reveal_directives_json='[{"id":"r","fact_ref":"fact:s","observer":"reader"}]')
    review = PlanContractReviewer().review(_context([]), _output(("bind:0", "done")), chapter_plan=plan)
    assert review.verdict == "pass" and review.issues == []


def test_draft_review_reads_bindings_and_reveals_from_plan_postgres():
    from sqlalchemy import select
    from forwin.models.project import ArcPlanVersion
    from forwin.planning.beat_sheet.types import READER_OBSERVER
    from tests.world_gate_helpers import _knowledge, _seed_world, _session_factory
    from tests.chapter_delta_helpers import BODY, draft_review as _draft_review, world_review_output as world_output

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2}])
        secret = world.fact("fact-secret")
        with Session.begin() as session:
            arc = session.scalars(select(ArcPlanVersion).where(
                ArcPlanVersion.project_id == world.project_id, ArcPlanVersion.arc_number == 1)).one()
            session.add(ChapterPlan(project_id=world.project_id, arc_plan_id=arc.id, chapter_number=1,
                world_bindings_json=json.dumps([{"kind": "secret", "ref": secret, "how": "Reveal"}]),
                reveal_directives_json=json.dumps([{"id": "r", "fact_ref": secret,
                                                   "observer": READER_OBSERVER, "target_state": "knows"}])))
        fields = {"knowledge_events": [_knowledge(READER_OBSERVER, secret, channel="inferred")]}
        missing = _draft_review(Session, world, world_output(world, fields))
        assert [(issue.rule_name, issue.blocking) for issue in missing.issues] == [("plan_binding_missing", True)]
        fields["plan_fulfillment"] = [{"target_id": "bind:0", "status": "done", "evidence": BODY}]
        fulfilled = _draft_review(Session, world, world_output(world, fields))
        assert fulfilled.verdict == "pass" and fulfilled.issues == []
    finally:
        engine.dispose()


def test_draft_review_loads_binding_plan_sqlite():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from forwin.models.base import Base
    from forwin.models.project import ArcPlanVersion, Project
    from tests.chapter_delta_helpers import draft_review as _draft_review, world_review_output as world_output

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session.begin() as session:
            project = Project(title="Bindings", premise="test", genre="test")
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
            session.add(arc)
            session.flush()
            session.add(ChapterPlan(project_id=project.id, arc_plan_id=arc.id, chapter_number=1,
                                   world_bindings_json='[{"kind":"event","ref":"event-1","how":"Witness"}]'))
            world = SimpleNamespace(project_id=project.id)
        review = _draft_review(Session, world, world_output(world), world_consistency_review_enabled=False)
        assert [(issue.rule_name, issue.blocking) for issue in review.issues] == [("plan_binding_missing", True)]
    finally:
        engine.dispose()


@pytest.mark.parametrize("status", ["missing", "partial"])
def test_binding_repair_never_exposes_author_details(status):
    from forwin.generation.pipeline_core.repair_patches import _chapter_experience_patch_payload
    from forwin.protocol.experience import ChapterExperiencePlan
    from forwin.review.draft_service import DraftReviewService

    marker = "AUTHOR_ONLY_SECRET_927"
    bindings = [{"kind": "secret", "ref": f"fact:{marker}-{n}", "how": f"Reveal {marker}-{n}"}
                for n in range(2)]
    plan = ChapterPlan(world_bindings_json=json.dumps(bindings))
    context = _context(["Follow the visible clue"])
    output = _output(("goal:1", "done"), ("bind:0", status), ("bind:1", status))
    review = PlanContractReviewer().review(context, output, chapter_plan=plan)
    assert len({issue.description for issue in review.issues}) == 2
    for index, issue in enumerate(review.issues):
        assert marker not in issue.description + issue.suggested_fix
        assert str(index + 1) in issue.description
        assert "secret" in issue.description
        assert issue.original_result == {**bindings[index], "index": index}
        assert marker not in " ".join(issue.evidence_refs)
    instruction = DraftReviewService._plan_repair_instruction(plan_issues=review.issues, context=context)
    assert marker not in " ".join(instruction.must_fix)
    instruction = DraftReviewService._restrict_must_fix_to_writer_errors(instruction, issues=review.issues)
    assert marker not in " ".join(instruction.must_fix)
    patch = _chapter_experience_patch_payload(ChapterExperiencePlan(chapter_number=1), instruction)
    assert marker not in " ".join(patch["rule_anchors"])
    if status == "missing":
        assert len(instruction.must_fix) == len(patch["rule_anchors"]) == 2


def test_partial_binding_warns_without_blocking():
    plan = ChapterPlan(world_bindings_json='[{"kind":"event","ref":"event-1","how":"Witness"}]')
    review = PlanContractReviewer().review(_context([]), _output(("bind:0", "partial")), chapter_plan=plan)
    assert review.verdict == "warn"
    assert [(issue.rule_name, issue.severity, issue.blocking) for issue in review.issues] == [
        ("plan_binding_partial", "warning", False),
    ]
    assert "target=bind:0" in review.issues[0].evidence_refs


def test_binding_lookup_without_project_logs_warning(caplog):
    session = SimpleNamespace(scalar=lambda query: None)
    PlanContractReviewer().review(_context([]), _output(), session=session)
    assert "project_id" in caplog.text and "chapter=1" in caplog.text
