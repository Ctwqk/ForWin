from __future__ import annotations

from sqlalchemy import inspect

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import CanonQualitySignal, CountdownLedgerEntry
from forwin.models import ArcPlanVersion, CandidateDraftRecord, ChapterDraft, ChapterPlan, ChapterReview, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch


def test_repository_persists_signals_and_admission_runs() -> None:
    engine = get_engine(postgres_test_url("canon_quality_repository"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Canon Quality", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            repo = CanonQualityRepository(session)
            signal = CanonQualitySignal(
                signal_id="sig-1",
                project_id=project.id,
                chapter_number=1,
                signal_type="placeholder_leakage",
                severity="error",
                target_scope="body",
                subject_key="placeholder:相关人员",
                description="正文包含占位符。",
                evidence_refs=["body:1-5"],
            )
            repo.save_signals([signal])
            gate = evaluate_canon_admission(
                project_id=project.id,
                chapter_number=1,
                draft_id="draft-1",
                review_id="review-1",
                review_verdict="warn",
                signals=[signal],
                mode="strict",
            )
            run = repo.save_admission_run(gate, signals=[signal])
            session.commit()

        with session_factory() as session:
            repo = CanonQualityRepository(session)
            open_signals = repo.list_open_signals(project.id, before_chapter=2)
            assert len(open_signals) == 1
            assert open_signals[0].signal_id == "sig-1"
            assert session.get(type(run), run.id).blocking_issue_count == 1
    finally:
        engine.dispose()


def test_repository_persists_obligation_admission_run_fields() -> None:
    engine = get_engine(postgres_test_url("canon_quality_obligation_admission_run"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Canon Obligation", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            repo = CanonQualityRepository(session)
            obligation = NarrativeObligation(
                id="obl-run",
                project_id=project.id,
                origin_chapter_number=10,
                obligation_type="motivation_gap",
                priority="P1",
                status="planned",
                summary="韩砚动机尚未解释。",
                hardness="design_debt",
                deadline_chapter=11,
                payoff_test="第11章必须给出韩砚动机证据。",
                linked_plan_patch_ids=["patch-run"],
            )
            patch = NarrativePlanPatch(
                id="patch-run",
                project_id=project.id,
                target_scope="chapter",
                affected_chapters=[11],
                source_obligation_ids=["obl-run"],
                validation_status="passed",
                applied=True,
            )
            gate = evaluate_canon_admission(
                project_id=project.id,
                chapter_number=10,
                draft_id="draft-10",
                review_id="review-10",
                review_verdict="warn",
                obligations=[obligation],
                plan_patches=[patch],
                mode="strict",
            )
            run = repo.save_admission_run(gate, signals=[])
            session.commit()

        with session_factory() as session:
            stored = session.get(type(run), run.id)
            assert stored.admission_mode == "with_obligation"
            assert stored.obligation_ids_json == '["obl-run"]'
            assert stored.required_plan_patch_ids_json == '["patch-run"]'
            assert stored.over_budget == "false"
    finally:
        engine.dispose()


def test_init_db_creates_canon_quality_tables() -> None:
    engine = get_engine(postgres_test_url("canon_quality_tables"))
    init_db(engine)
    try:
        names = set(inspect(engine).get_table_names())
        assert "canon_quality_signals" in names
        assert "canon_admission_runs" in names
        assert "countdown_ledgers" in names
    finally:
        engine.dispose()


def test_countdown_history_uses_only_committed_draft_ledgers() -> None:
    engine = get_engine(postgres_test_url("canon_quality_countdown_committed_drafts"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Canon Quality", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=1,
                arc_synopsis="测试 arc",
            )
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
            )
            session.add(plan)
            session.flush()
            stale_draft = ChapterDraft(
                id="draft-stale",
                chapter_plan_id=plan.id,
                body_text="stale",
                summary="stale",
            )
            accepted_draft = ChapterDraft(
                id="draft-accepted",
                chapter_plan_id=plan.id,
                body_text="accepted",
                summary="accepted",
            )
            session.add_all([stale_draft, accepted_draft])
            session.flush()
            accepted_review = ChapterReview(
                id="review-accepted",
                draft_id=accepted_draft.id,
                verdict="pass",
            )
            session.add(accepted_review)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan.id,
                    chapter_number=1,
                    candidate_draft_id=accepted_draft.id,
                    review_id=accepted_review.id,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.flush()
            repo = CanonQualityRepository(session)
            repo.save_countdown_entries(
                [
                    CountdownLedgerEntry(
                        project_id=project.id,
                        countdown_key="main",
                        chapter_number=1,
                        normalized_remaining_minutes=240,
                        raw_mention="4小时",
                        payload={"draft_id": "draft-stale"},
                    ),
                    CountdownLedgerEntry(
                        project_id=project.id,
                        countdown_key="memory_reset",
                        chapter_number=1,
                        normalized_remaining_minutes=10080,
                        raw_mention="七天",
                        payload={"draft_id": "draft-accepted"},
                    ),
                ]
            )
            gate = evaluate_canon_admission(
                project_id=project.id,
                chapter_number=1,
                draft_id="draft-accepted",
                review_id="review-accepted",
                review_verdict="pass",
                signals=[],
                mode="strict",
            )
            repo.save_admission_run(gate, signals=[])
            session.commit()

        with session_factory() as session:
            entries = CanonQualityRepository(session).list_countdown_entries(project.id, before_chapter=2)

        assert entries == [
            {
                "countdown_key": "memory_reset",
                "chapter_number": 1,
                "normalized_remaining_minutes": 10080,
                "status": "consistent",
            }
        ]
    finally:
        engine.dispose()


def test_countdown_history_excludes_ledgers_when_no_committed_draft_exists() -> None:
    engine = get_engine(postgres_test_url("canon_quality_countdown_requires_committed_draft"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Canon Quality", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            repo = CanonQualityRepository(session)
            repo.save_countdown_entries(
                [
                    CountdownLedgerEntry(
                        project_id=project.id,
                        countdown_key="memory_reset",
                        chapter_number=1,
                        normalized_remaining_minutes=20,
                        raw_mention="二十分钟",
                        payload={"draft_id": "uncommitted-draft"},
                    )
                ]
            )
            session.commit()

        with session_factory() as session:
            entries = CanonQualityRepository(session).list_countdown_entries(project.id, before_chapter=2)

        assert entries == []
    finally:
        engine.dispose()
