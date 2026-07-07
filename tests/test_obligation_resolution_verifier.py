from __future__ import annotations

from forwin.canon_quality import obligation_verifier as obligation_verifier_module
from forwin.canon_quality.obligation_verifier import (
    ObligationResolutionVerifier,
    verify_active_obligations_after_acceptance,
)
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation


def _obligation(project_id: str = "project-1", *, payoff_test: str = "第12章必须解释钥匙来源") -> NarrativeObligation:
    return NarrativeObligation(
        project_id=project_id,
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="必须解释钥匙来源。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test=payoff_test,
        blocking_policy="block_at_deadline",
    )


def test_verifier_passes_when_accepted_text_contains_payoff_marker() -> None:
    verifier = ObligationResolutionVerifier()
    result = verifier.verify(
        obligation=_obligation(),
        accepted_chapter_text="第12章解释了钥匙来源，并给出证据。",
    )

    assert result.status == "pass"
    assert result.matched_markers == ["钥匙来源"]


def test_verifier_warns_when_payoff_marker_is_missing() -> None:
    result = ObligationResolutionVerifier().verify(
        obligation=_obligation(),
        accepted_chapter_text="第12章继续追逐，没有解释关键物件。",
    )

    assert result.status == "warn"


def test_verify_active_obligations_after_acceptance_marks_passed_items_resolved() -> None:
    engine = get_engine(postgres_test_url("obligation_verifier_acceptance"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="义务验证", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            created = repo.create_obligation(_obligation(project.id))

            result = verify_active_obligations_after_acceptance(
                session=session,
                project_id=project.id,
                chapter_number=12,
                accepted_text="第12章解释了钥匙来源，并给出证据。",
            )
            session.commit()

        assert result["resolved_obligation_ids"] == [created.id]

        with session_factory() as session:
            stored = session.get(NarrativeObligationRow, created.id)
            assert stored is not None
            assert stored.status == "resolved"
            resolved = NarrativeObligationRepository(session).list_active_for_context(
                project.id,
                chapter_number=13,
            )
            assert resolved == []
    finally:
        engine.dispose()


def test_verify_due_obligations_for_draft_returns_passed_due_ids_without_persisting() -> None:
    verify_due_obligations_for_draft = getattr(
        obligation_verifier_module,
        "verify_due_obligations_for_draft",
        None,
    )
    assert callable(verify_due_obligations_for_draft)
    obligation = _obligation(
        project_id="project-1",
        payoff_test="第12章必须解释钥匙来源",
    ).model_copy(update={"id": "obl-due"})
    resolved_ids = verify_due_obligations_for_draft(
        obligations=[obligation],
        chapter_number=12,
        draft_text="第12章解释了钥匙来源，并给出证据。",
        evidence_ref="draft:d1",
    )

    assert resolved_ids == ["obl-due"]
    assert obligation.status == "active"


def test_verify_due_obligations_for_draft_ignores_unknown_type_without_payoff_marker() -> None:
    verify_due_obligations_for_draft = getattr(
        obligation_verifier_module,
        "verify_due_obligations_for_draft",
        None,
    )
    assert callable(verify_due_obligations_for_draft)
    obligation = _obligation(
        project_id="project-1",
        payoff_test="第12章必须揭示钥匙来源",
    ).model_copy(
        update={
            "id": "obl-unknown",
            "obligation_type": "custom_reader_promise",
        }
    )

    resolved_ids = verify_due_obligations_for_draft(
        obligations=[obligation],
        chapter_number=12,
        draft_text="第12章安排新的追逐场景，但没有兑现读者承诺。",
        evidence_ref="draft:d1",
    )

    assert resolved_ids == []
