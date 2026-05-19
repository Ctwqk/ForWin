from __future__ import annotations

from sqlalchemy import inspect

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import (
    NarrativeObligation,
    NarrativePlanPatch,
)


def test_init_db_creates_narrative_obligation_tables() -> None:
    engine = get_engine(postgres_test_url("narrative_obligation_tables"))
    init_db(engine)
    try:
        names = set(inspect(engine).get_table_names())
        assert "narrative_obligations" in names
        assert "narrative_plan_patches" in names
    finally:
        engine.dispose()


def test_repository_persists_obligation_and_plan_patch_lifecycle() -> None:
    engine = get_engine(postgres_test_url("narrative_obligation_ledger"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="义务账本", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            obligation = repo.create_obligation(
                NarrativeObligation(
                    project_id=project.id,
                    origin_chapter_number=10,
                    origin_draft_id="draft-10",
                    origin_review_id="review-10",
                    origin_signal_ids=["sig-motivation"],
                    obligation_type="motivation_gap",
                    priority="P1",
                    status="proposed",
                    summary="韩砚协助陆明的动机尚未解释。",
                    deferral_reason="下一章可以用行动和对白偿还。",
                    hardness="design_debt",
                    subject_refs=["character:韩砚"],
                    evidence_refs=["review:sig-motivation"],
                    deadline_chapter=11,
                    payoff_test="第11章必须给出韩砚协助陆明的明确动机证据。",
                    blocking_policy="block_at_deadline",
                )
            )
            patch = repo.create_plan_patch(
                NarrativePlanPatch(
                    project_id=project.id,
                    patch_type="defer_acceptance",
                    target_scope="chapter",
                    target_plan_id="chapter-plan-11",
                    affected_chapters=[11],
                    source_obligation_ids=[obligation.id],
                    source_signal_ids=["sig-motivation"],
                    new_contract={"obligations_to_resolve": [obligation.id]},
                    writer_context_injections=[
                        {
                            "obligation_id": obligation.id,
                            "instruction": "用韩砚的行动或对白解释他为何协助陆明。",
                        }
                    ],
                    reviewer_context_injections=[
                        {
                            "obligation_id": obligation.id,
                            "payoff_test": "必须看到明确动机证据。",
                        }
                    ],
                    expected_resolution_tests=["第11章必须给出韩砚动机证据。"],
                    validation_status="passed",
                    applied=True,
                )
            )
            repo.mark_obligation_planned(obligation.id, linked_plan_patch_ids=[patch.id])
            repo.activate_planned_for_chapter(project.id, origin_chapter_number=10)
            session.commit()

        with session_factory() as session:
            repo = NarrativeObligationRepository(session)
            active = repo.list_active_for_context(project.id, chapter_number=11)
            assert len(active) == 1
            assert active[0].id == obligation.id
            assert active[0].status == "active"
            assert active[0].linked_plan_patch_ids == [patch.id]
            assert active[0].must_resolve_now is True
    finally:
        engine.dispose()


def _active_obligation(project_id: str, obligation_id: str = "") -> NarrativeObligation:
    return NarrativeObligation(
        id=obligation_id,
        project_id=project_id,
        origin_chapter_number=10,
        origin_draft_id="draft-10",
        origin_review_id="review-10",
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="韩砚协助陆明的动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="第12章必须给出韩砚协助陆明的明确动机证据。",
        blocking_policy="block_at_deadline",
    )


def test_mark_obligation_resolved_records_evidence() -> None:
    engine = get_engine(postgres_test_url("narrative_obligation_resolved"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="义务状态", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            created = repo.create_obligation(_active_obligation(project.id))

            resolved = repo.mark_obligation_resolved(
                created.id,
                verifier_result={"status": "pass", "matched_markers": ["marker-1"]},
                evidence_refs=["chapter:12"],
                resolution_chapter=12,
            )
            session.commit()

        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution_chapter == 12
        assert resolved.resolution_evidence_refs == ["chapter:12"]
        assert resolved.metadata["verifier_result"]["status"] == "pass"
    finally:
        engine.dispose()


def test_obligation_expire_block_and_waive_transitions() -> None:
    engine = get_engine(postgres_test_url("narrative_obligation_transitions"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="义务状态", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            expired_source = repo.create_obligation(_active_obligation(project.id))
            waived_source = repo.create_obligation(_active_obligation(project.id))

            expired = repo.expire_obligation(expired_source.id, reason="deadline passed")
            blocked = repo.block_expired_obligation(expired_source.id)
            waived = repo.waive_obligation(waived_source.id, reason="human exception", actor="operator")
            session.commit()

        assert expired is not None
        assert expired.status == "expired"
        assert expired.metadata["expire_reason"] == "deadline passed"
        assert blocked is not None
        assert blocked.status == "blocked"
        assert waived is not None
        assert waived.status == "waived"
        assert waived.waive_reason == "human exception"
        assert waived.metadata["waived_by"] == "operator"
    finally:
        engine.dispose()


def test_waive_obligation_rejects_missing_or_system_actor() -> None:
    engine = get_engine(postgres_test_url("narrative_obligation_waive_guard"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="义务状态", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            created = repo.create_obligation(_active_obligation(project.id))

            for actor in ("", "system"):
                try:
                    repo.waive_obligation(created.id, reason="unsafe", actor=actor)
                except ValueError as exc:
                    assert "human actor" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError(f"actor={actor!r} should be rejected")
    finally:
        engine.dispose()
