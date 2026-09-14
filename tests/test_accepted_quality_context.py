"""Formal Writer conflicts come from immutable accepted quality evidence."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from forwin.book_state.repository import BookStateRepository
from forwin.canon_quality import acceptance
from forwin.canon_quality.cache import persist_quality_projection
from forwin.canon_quality.chapter_review_form.service import ChapterReviewFormResult
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.canon_quality.types import (
    CanonQualityAnalysisResult,
    QualityAnalysisCachePayload,
)
from forwin.knowledge_system.context import KnowledgeContextQuery
from forwin.models import (
    ArcPlanVersion,
    CandidateDraftRecord,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    Project,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon import CanonCommitRecord
from forwin.protocol.book_state import WorldNode
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.retrieval.source_identity import CanonBaselineChanged, CanonReadBaseline
from forwin.writer.prompt_core import render_writer_context
from forwin.writer.world_context import render_world_context
from tests.postgres import postgres_test_url


def _signal(
    *,
    signal_id: str,
    project_id: str = "book",
    chapter_number: int,
    draft_id: str,
    description: str,
    status: str = "open",
) -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=signal_id,
        project_id=project_id,
        chapter_number=chapter_number,
        signal_type="world_model_conflict",
        severity="warning",
        target_scope="book",
        subject_key="world:fixture",
        description=description,
        status=status,
        payload={"draft_id": draft_id},
    )


def _accept(
    session,
    *,
    chapter_number: int,
    revision: int,
    signals: list[CanonQualitySignal],
) -> CanonCommitRecord:
    plan_id = f"chapter-{chapter_number}"
    draft_id = f"draft-{chapter_number}-{revision}"
    review_id = f"review-{chapter_number}-{revision}"
    candidate_id = f"candidate-{chapter_number}-{revision}"
    body = f"Body {chapter_number} revision {revision}"
    session.add(
        ChapterDraft(
            id=draft_id,
            chapter_plan_id=plan_id,
            version=revision,
            body_text=body,
            summary=body,
        )
    )
    session.flush()
    session.add(ChapterReview(id=review_id, draft_id=draft_id, verdict="pass"))
    session.flush()
    session.add(
        CandidateDraftRecord(
            id=candidate_id,
            project_id="book",
            chapter_plan_id=plan_id,
            chapter_number=chapter_number,
            candidate_draft_id=draft_id,
            review_id=review_id,
            version=revision,
            body_hash=hashlib.sha256(body.encode()).hexdigest(),
            status="accepted",
            canon_status="canon",
        )
    )
    session.flush()

    payload = QualityAnalysisCachePayload(
        analysis=CanonQualityAnalysisResult(
            project_id="book",
            chapter_number=chapter_number,
            draft_id=draft_id,
            signals=signals,
        )
    )
    repo = CanonQualityRepository(session)
    persist_quality_projection(
        repo,
        project_id="book",
        chapter_number=chapter_number,
        payload=payload,
    )
    gate = evaluate_canon_admission(
        project_id="book",
        chapter_number=chapter_number,
        draft_id=draft_id,
        review_id=review_id,
        review_verdict="pass",
        signals=signals,
    )
    run = repo.save_admission_run(gate, signals=signals)
    commit = CanonCommitRecord(
        id=f"accept-{chapter_number}-{revision}",
        idempotency_key=f"accepted-quality-{chapter_number}-{revision}",
        project_id="book",
        chapter_plan_id=plan_id,
        chapter_number=chapter_number,
        candidate_id=candidate_id,
        acceptance_revision=revision,
    )
    session.add(commit)
    session.flush()
    repo.bind_acceptance(
        project_id="book",
        chapter_number=chapter_number,
        draft_id=draft_id,
        acceptance_id=commit.id,
        quality_admission_run_id=run.id,
    )
    plan = session.get(ChapterPlan, plan_id)
    plan.active_commit_id = commit.id
    plan.status = "accepted"
    session.flush()
    return commit


@pytest.fixture
def accepted_quality_case():
    engine = get_engine(postgres_test_url("accepted-quality-context"))
    init_db(engine)
    factory = get_session_factory(engine)
    with factory.begin() as session:
        session.add_all(
            [
                Project(id="book", title="Book", premise="Fixture"),
                Project(id="other-book", title="Other", premise="Fixture"),
            ]
        )
        session.flush()
        session.add(
            ArcPlanVersion(
                id="arc",
                project_id="book",
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
                arc_synopsis="Fixture",
            )
        )
        session.flush()
        for number in (1, 2, 3):
            session.add(
                ChapterPlan(
                    id=f"chapter-{number}",
                    project_id="book",
                    arc_plan_id="arc",
                    chapter_number=number,
                    title=f"Chapter {number}",
                )
            )
        session.flush()
        BookStateRepository(session).create_world_node(
            WorldNode(
                id="world-rule",
                project_id="book",
                node_type="rule",
                name="World rule",
            )
        )

        old = _accept(
            session,
            chapter_number=1,
            revision=1,
            signals=[
                _signal(
                    signal_id="old-version",
                    chapter_number=1,
                    draft_id="draft-1-1",
                    description="旧版本冲突",
                )
            ],
        )
        current = _accept(
            session,
            chapter_number=1,
            revision=2,
            signals=[
                _signal(
                    signal_id="formal-conflict",
                    chapter_number=1,
                    draft_id="draft-1-2",
                    description="正式冲突",
                ),
                _signal(
                    signal_id="resolved-conflict",
                    chapter_number=1,
                    draft_id="draft-1-2",
                    description="已解决冲突",
                    status="resolved",
                ),
            ],
        )
        session.get(CandidateDraftRecord, old.candidate_id).status = "superseded"
        assert session.get(ChapterPlan, "chapter-1").active_commit_id == current.id

        _accept(
            session,
            chapter_number=3,
            revision=1,
            signals=[
                _signal(
                    signal_id="future-conflict",
                    chapter_number=3,
                    draft_id="draft-3-1",
                    description="未来信号",
                )
            ],
        )

        candidate_body = "Rejected candidate"
        session.add(
            ChapterDraft(
                id="rejected-draft",
                chapter_plan_id="chapter-2",
                body_text=candidate_body,
            )
        )
        session.flush()
        issue = ContinuityIssue(
            rule_name="candidate-world-conflict",
            severity="error",
            description="候选诊断",
            issue_type="world_model_conflict",
        )
        repair = RepairInstruction(
            repair_scope="world_model",
            failure_type="world_model_conflict",
            must_fix=[issue.description],
        )
        verdict = ReviewVerdict(
            verdict="fail",
            issues=[issue],
            repair_instruction=repair,
        )
        session.add(
            ChapterReview(
                id="rejected-review",
                draft_id="rejected-draft",
                verdict="fail",
                issues_json=json.dumps(
                    [issue.model_dump(mode="json")], ensure_ascii=False
                ),
                review_meta_json=verdict.model_dump_json(),
            )
        )
        session.flush()
        session.add(
            CandidateDraftRecord(
                id="rejected-candidate",
                project_id="book",
                chapter_plan_id="chapter-2",
                chapter_number=2,
                candidate_draft_id="rejected-draft",
                review_id="rejected-review",
                body_hash=hashlib.sha256(candidate_body.encode()).hexdigest(),
                status="failed",
                canon_status="candidate",
            )
        )
        session.flush()
        candidate_signal = _signal(
            signal_id="candidate-diagnostic",
            chapter_number=2,
            draft_id="rejected-draft",
            description="候选诊断",
        )
        repo = CanonQualityRepository(session)
        # Mutable analysis rows remain candidate diagnostics. They cannot rewrite
        # the immutable snapshot that was bound to the active acceptance.
        repo.save_signals(
            [
                _signal(
                    signal_id="formal-conflict",
                    chapter_number=1,
                    draft_id="rejected-draft",
                    description="候选诊断",
                ),
                candidate_signal,
                _signal(
                    signal_id="unknown-source",
                    chapter_number=2,
                    draft_id="missing-draft",
                    description="未知来源",
                ),
                _signal(
                    signal_id="other-project",
                    project_id="other-book",
                    chapter_number=1,
                    draft_id="other-draft",
                    description="其他项目",
                ),
                *[
                    _signal(
                        signal_id=f"newer-invalid-{index}",
                        chapter_number=2,
                        draft_id="rejected-draft",
                        description=f"较新无效信号 {index}",
                    )
                    for index in range(10)
                ],
            ]
        )
    try:
        yield SimpleNamespace(
            Session=factory,
            repair=repair,
            candidate_signal=candidate_signal,
            candidate_body_hash=hashlib.sha256(candidate_body.encode()).hexdigest(),
        )
    finally:
        engine.dispose()


def _writer_pack(world_context, repair_contract=None):
    return ChapterContextPack(
        project_id="book",
        project_title="Book",
        premise="Fixture",
        genre="mystery",
        setting_summary="World",
        chapter_number=3,
        chapter_plan_title="Chapter 3",
        chapter_plan_one_line="Continue",
        chapter_goals=[],
        world_context=world_context,
        repair_contract=repair_contract,
    )


def _activate_candidate_overlay(session, case):
    CanonQualityRepository(session).activate_candidate_projection(
        project_id="book",
        chapter_number=2,
        draft_id="rejected-draft",
        body_sha256=case.candidate_body_hash,
        historical_form=ChapterReviewFormResult(
            project_id="book",
            chapter_number=2,
            draft_id="rejected-draft",
            signals=[case.candidate_signal],
        ),
        projection_id="rejected-validation",
    )


def test_writer_world_conflicts_use_only_current_accepted_quality_snapshot(
    accepted_quality_case,
):
    with accepted_quality_case.Session() as session:
        _activate_candidate_overlay(session, accepted_quality_case)
        baseline = CanonReadBaseline.capture(session, "book", as_of_chapter=2)
        world_context = KnowledgeContextQuery(session).build(
            project_id="book",
            chapter_number=3,
            baseline=baseline,
        )
        prompt = render_world_context(world_context)
        writer_prompt = render_writer_context(
            _writer_pack(world_context), layout="single"
        )
        repair_prompt = render_writer_context(
            _writer_pack(world_context, accepted_quality_case.repair),
            layout="single",
        )

    assert prompt is not None
    assert "正式冲突" in prompt
    assert "正式冲突" in writer_prompt
    for excluded in (
        "候选诊断",
        "未来信号",
        "旧版本冲突",
        "未知来源",
        "其他项目",
        "已解决冲突",
        "较新无效信号",
    ):
        assert excluded not in writer_prompt
    # Candidate evidence remains available through the existing repair contract.
    assert "候选诊断" in repair_prompt


def test_world_conflict_read_rejects_baseline_that_changes_after_projection_read(
    accepted_quality_case, monkeypatch
):
    original = acceptance.active_projections
    changed = False

    def change_after_read(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            with accepted_quality_case.Session.begin() as concurrent:
                concurrent.get(Project, "book").book_revision += 1
        return result

    monkeypatch.setattr(acceptance, "active_projections", change_after_read)
    with accepted_quality_case.Session() as session:
        baseline = CanonReadBaseline.capture(session, "book", as_of_chapter=2)
        with pytest.raises(CanonBaselineChanged):
            KnowledgeContextQuery(session).build(
                project_id="book",
                chapter_number=3,
                baseline=baseline,
            )
