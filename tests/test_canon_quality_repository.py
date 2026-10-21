from __future__ import annotations

from sqlalchemy import inspect

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from tests.postgres import postgres_test_url


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
        assert "countdown_ledgers" not in names
        assert "character_state_transitions" not in names
    finally:
        engine.dispose()
