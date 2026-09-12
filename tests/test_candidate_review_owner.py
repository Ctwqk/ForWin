from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.models.audit import DecisionEvent
from forwin.models.base import Base
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.naming.entity_registrar import EntityRegistrar
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.repair.verification import RepairVerifier
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore


@pytest.fixture
def database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="book", title="Book", premise="Fixture"))
        session.add(ArcPlanVersion(id="arc", project_id="book", arc_synopsis="Arc"))
        plan = ChapterPlan(
            id="chapter",
            project_id="book",
            arc_plan_id="arc",
            chapter_number=1,
            title="誓约",
            status="planned",
        )
        session.add(plan)
        session.commit()
        yield session, plan
    engine.dispose()


class BodyReviewer:
    """Frozen body labels expose stale review or stale registrar after autofix."""

    def __init__(self):
        self.bodies = []

    def review(
        self,
        *,
        project_id,
        repo,
        context,
        writer_output,
        continuity_checker,
        reviewer_skill_layers,
    ):
        EntityRegistrar(session=repo.session).verify_writer_output_admission(
            project_id=project_id,
            writer_output=writer_output,
        )
        self.bodies.append(writer_output.body)
        if "林明" in writer_output.body:
            return ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="canon_name_drift",
                        severity="error",
                        description="Name drift",
                        entity_names=["林明", "林川"],
                    )
                ],
            )
        if "姓名：工作人员" in writer_output.body:
            return ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="bare_role_placeholder_leakage",
                        severity="error",
                        description="Placeholder",
                    )
                ],
            )
        return ReviewVerdict(
            verdict="pass", review_summary=f"reviewed:{writer_output.body}"
        )


def _owner(session, tmp_path, reviewer=None):
    assert importlib.util.find_spec("forwin.review.candidate") is not None, (
        "A0 requires independently executable candidate review and persistence"
    )
    module = importlib.import_module("forwin.review.candidate")
    store = ArtifactStore(str(tmp_path))
    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(), artifact_store=store, observability=None
    )
    return module, module.CandidateReviewService(
        draft_review=reviewer or BodyReviewer(),
        repair_verifier=RepairVerifier(llm_enabled=False),
        model_client=SimpleNamespace(model="frozen-model"),
        skill_router=SimpleNamespace(select=lambda **_kwargs: []),
        skill_prompt_layer_builder=SimpleNamespace(build=lambda _skills: []),
        artifact_store=store,
        trace_recorder=recorder,
    )


def _request(module, session, output):
    repo = StateRepository(session)
    return module.CandidateReviewRequest(
        session=session,
        repo=repo,
        checker=ContinuityChecker(repo),
        project_id="book",
        chapter_number=1,
        context=SimpleNamespace(chapter_number=1),
        output=output,
    )


def _output(body="林明让工作人员留下。", title="誓约"):
    return WriterOutput(
        project_id="book",
        chapter_number=1,
        title=title,
        body=body,
        char_count=len(body),
        end_of_chapter_summary=body,
    )


def test_autofix_rechecks_each_final_body_before_persisting_its_review(
    database, tmp_path
):
    session, chapter = database
    reviewer = BodyReviewer()
    module, owner = _owner(session, tmp_path, reviewer)
    original = _output()
    evaluated = owner.evaluate(_request(module, session, original))
    assert reviewer.bodies == [
        "林明让工作人员留下。",
        "林川让工作人员留下。",
    ]
    assert original.body == "林明让工作人员留下。"
    assert evaluated.output.body == "林川让工作人员留下。"
    assert evaluated.review.review_summary == "reviewed:林川让工作人员留下。"
    assert list(session.scalars(select(ChapterDraft))) == []
    persisted = owner.persist(
        session=session,
        updater=StateUpdater(session),
        project_id="book",
        chapter_plan=chapter,
        evaluation=evaluated,
    )
    assert persisted.output.body == "林川让工作人员留下。"
    assert persisted.draft.body_text == "林川让工作人员留下。"
    assert persisted.review_row.draft_id == persisted.draft.id
    assert persisted.review_row.verdict == "pass"
    candidate = session.get(CandidateDraftRecord, persisted.candidate_id)
    assert candidate.candidate_draft_id == persisted.draft.id
    assert candidate.review_id == persisted.review_row.id
    assert persisted.draft.llm_model == "frozen-model"
    assert chapter.status == "drafted"
    assert list(session.scalars(select(CanonCommitRecord))) == []
    session.rollback()
    assert list(session.scalars(select(ChapterDraft))) == []
    assert list(session.scalars(select(ChapterReview))) == []
    assert list(session.scalars(select(CandidateDraftRecord))) == []
    assert list(session.scalars(select(DecisionEvent))) == []
    assert session.get(ChapterPlan, "chapter").status == "planned"


@pytest.mark.parametrize(
    "must_preserve,title,want_verdict,want_preserved",
    [
        (["誓约"], "另一个标题", "fail", False),
        (["保留同伴相互支持的承诺"], "誓约", "pass", None),
    ],
)
def test_repair_verification_uses_evaluated_output_and_preserves_unknown(
    database, tmp_path, must_preserve, title, want_verdict, want_preserved
):
    session, _ = database
    module, owner = _owner(session, tmp_path)
    evaluated = owner.evaluate(
        _request(module, session, _output(title=title)),
        verification=module.CandidateRepairVerification(
            original_output=_output("林川让工作人员留下。"),
            before_review=ReviewVerdict(verdict="fail"),
            instruction=RepairInstruction(
                repair_scope="draft", failure_type="mixed", must_preserve=must_preserve
            ),
        ),
    )
    assert evaluated.output.body == "林川让工作人员留下。"
    assert evaluated.review.verdict == want_verdict
    assert (
        evaluated.review.repair_verification.preserved_all_must_preserve
        is want_preserved
    )
    if want_verdict == "fail":
        assert [issue.rule_name for issue in evaluated.review.issues] == [
            "repair_preserve_breach"
        ]


def test_review_error_propagates_without_persisting_draft(database, tmp_path):
    session, _ = database
    error = RuntimeError("review transport unavailable")

    def fail(**_kwargs):
        raise error

    module, owner = _owner(session, tmp_path, SimpleNamespace(review=fail))
    with pytest.raises(RuntimeError) as caught:
        owner.evaluate(_request(module, session, _output()))
    assert caught.value is error
    assert list(session.scalars(select(ChapterDraft))) == []
    assert list(session.scalars(select(CandidateDraftRecord))) == []


def test_placeholder_review_does_not_invent_an_alias_or_rewrite_evidence(
    database, tmp_path
):
    session, _ = database
    reviewer = BodyReviewer()
    module, owner = _owner(session, tmp_path, reviewer)
    original = _output("林川看见登记表写着：姓名：工作人员。")
    original.generation_meta = {"source_quote": "姓名：工作人员"}
    before = original.model_dump(mode="python")
    evaluated = owner.evaluate(_request(module, session, original))
    assert evaluated.output.body == before["body"]
    assert evaluated.output.end_of_chapter_summary == before["end_of_chapter_summary"]
    assert evaluated.output.generation_meta["source_quote"] == "姓名：工作人员"
    assert evaluated.review.verdict == "fail"
    assert [issue.rule_name for issue in evaluated.review.issues] == [
        "bare_role_placeholder_leakage"
    ]
    assert reviewer.bodies == [before["body"]]
    assert original.model_dump(mode="python") == before
    assert list(session.scalars(select(CandidateDraftRecord))) == []


def test_new_candidate_and_auto_retry_preserve_spent_repair_budget(database, tmp_path):
    from forwin.generation.review_auto_retry import reset_chapter_for_auto_review_retry

    session, chapter = database
    module, owner = _owner(session, tmp_path)
    chapter.repair_attempt_count = 3
    persisted = owner.persist(
        session=session,
        updater=StateUpdater(session),
        project_id="book",
        chapter_plan=chapter,
        evaluation=module.CandidateReviewEvaluation(
            _output("铜钥匙仍未兑现。"), ReviewVerdict(verdict="pass")
        ),
    )
    candidate = session.get(CandidateDraftRecord, persisted.candidate_id)
    assert candidate.repair_attempt_count == 3
    reset_chapter_for_auto_review_retry(
        session,
        project_id="book",
        chapter_number=1,
        plan=chapter,
        source="auto_continue_review_retry",
        reason="retry",
        summary="retry",
    )
    session.flush()
    assert chapter.repair_attempt_count == 3
    chapter.repair_attempt_count = (
        0  # A stale task path cannot erase the candidate ledger.
    )
    again = owner.persist(
        session=session,
        updater=StateUpdater(session),
        project_id="book",
        chapter_plan=chapter,
        evaluation=module.CandidateReviewEvaluation(
            _output("新的待审正文。"), ReviewVerdict(verdict="pass")
        ),
    )
    assert (
        session.get(CandidateDraftRecord, again.candidate_id).repair_attempt_count == 3
    )
    assert chapter.repair_attempt_count == 3
