from __future__ import annotations

import json

import pytest

from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction, ChapterTime, PlanFulfillment
from forwin.book_state.repository import BookStateRepository
from forwin.canon_quality import obligation_verifier as obligation_verifier_module
from forwin.canon_quality.obligation_verifier import (
    ObligationResolutionVerifier,
    verify_active_obligations_after_acceptance,
    verify_due_obligations_for_draft,
)
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.book_state import GraphDelta
from tests.postgres import postgres_test_url


EVIDENCE = "The receipt names the locksmith who supplied the key."


def _obligation(project_id: str = "project-1", **updates) -> NarrativeObligation:
    return NarrativeObligation(
        id="obl-due", project_id=project_id, origin_chapter_number=10,
        obligation_type="motivation_gap", priority="P1", status="active",
        summary="Explain the key's origin.", hardness="design_debt", deadline_chapter=12,
        payoff_test="Explain the key's origin by chapter 12.", blocking_policy="block_at_deadline",
    ).model_copy(update=updates)


def _fulfillment(status="done", *, target_id="obligation:obl-due", evidence=EVIDENCE):
    return PlanFulfillment(target_id=target_id, status=status, evidence=evidence)


def _delta(*items):
    return ChapterDeltaExtraction(
        body_sha256="test", time=ChapterTime(elapsed=None, evidence=""), plan_fulfillment=list(items),
    )


@pytest.mark.parametrize("status", ["done", "partial", "missing"])
def test_verifier_uses_plan_fulfillment_status(status) -> None:
    result = ObligationResolutionVerifier().verify(
        obligation=_obligation(), plan_fulfillment=[_fulfillment(status)],
    )

    assert (result.status == "pass") == (status == "done")
    assert result.verifier_result == result.status
    assert result.matched_markers == [EVIDENCE]


@pytest.mark.parametrize("items", [
    [], [_fulfillment(target_id="obligation:other")], [_fulfillment(target_id="goal:1")],
    [_fulfillment(evidence="")], [_fulfillment(evidence=" \n")],
])
def test_verifier_requires_matching_target_and_nonblank_evidence(items) -> None:
    result = ObligationResolutionVerifier().verify(obligation=_obligation(), plan_fulfillment=items)
    assert result.status != "pass"


@pytest.mark.parametrize("obligation_type", ["motivation_gap", "countdown_explanation", "custom_reader_promise"])
def test_draft_resolves_due_obligation_from_evidence_without_persisting(obligation_type) -> None:
    obligation = _obligation(obligation_type=obligation_type)
    resolved_ids = verify_due_obligations_for_draft(
        obligations=[obligation], chapter_number=12, chapter_delta=_delta(_fulfillment()), evidence_ref="draft:d1",
    )
    assert resolved_ids == [obligation.id]
    assert obligation.status == "active"


@pytest.mark.parametrize("chapter_delta", [None, _delta(), _delta(_fulfillment("partial")), _delta(_fulfillment("missing"))])
def test_draft_leaves_unfulfilled_obligations_unresolved(chapter_delta) -> None:
    assert verify_due_obligations_for_draft(
        obligations=[_obligation()], chapter_number=12, chapter_delta=chapter_delta,
    ) == []


@pytest.mark.parametrize("updates", [{"deadline_chapter": 13}, {"status": "resolved"}, {"status": "planned"}, {"id": ""}])
def test_draft_preserves_active_due_filter(updates) -> None:
    assert verify_due_obligations_for_draft(
        obligations=[_obligation(**updates)], chapter_number=12, chapter_delta=_delta(_fulfillment()),
    ) == []


def test_after_acceptance_reads_committed_delta_fulfillment() -> None:
    engine = get_engine(postgres_test_url("obligation_verifier_acceptance"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory.begin() as session:
            project = Project(title="Obligation verification", premise="test", genre="test", target_total_chapters=20)
            session.add(project)
            session.flush()
            project_id = project.id
            repo = NarrativeObligationRepository(session)
            created = repo.create_obligation(_obligation(project_id))
            for obligation_id in ("partial", "missing", "absent", "other-source", "other-chapter"):
                repo.create_obligation(_obligation(project_id, id=obligation_id))
            deltas = BookStateRepository(session)
            deltas.append_graph_delta(GraphDelta(
                id="committed-extraction", project_id=project_id, chapter_number=12, source_type="chapter_extraction",
                metadata={"plan_fulfillment": [item.model_dump(mode="json") for item in (
                    _fulfillment(), _fulfillment("partial", target_id="obligation:partial"),
                    _fulfillment("missing", target_id="obligation:missing", evidence=""),
                )]},
            ))
            for delta_id, source_type, chapter, obligation_id in (
                ("other-source", "writer_output", 12, "other-source"),
                ("prior-chapter", "chapter_extraction", 11, "other-chapter"),
            ):
                deltas.append_graph_delta(GraphDelta(
                    id=delta_id, project_id=project_id, chapter_number=chapter, source_type=source_type,
                    metadata={"plan_fulfillment": [_fulfillment(target_id=f"obligation:{obligation_id}").model_dump(mode="json")]},
                ))

        with session_factory.begin() as session:
            result = verify_active_obligations_after_acceptance(session=session, project_id=project_id, chapter_number=12)
            assert result["resolved_obligation_ids"] == [created.id]
            assert set(result["warned_obligation_ids"] + result["failed_obligation_ids"]) == {
                "partial", "missing", "absent", "other-source", "other-chapter",
            }

        with session_factory() as session:
            stored = session.get(NarrativeObligationRow, created.id)
            assert stored.status == "resolved"
            assert stored.resolution_chapter == 12
            assert json.loads(stored.resolution_evidence_refs_json) == ["chapter:12"]
            verification = json.loads(stored.metadata_json)["verifier_result"]
            assert verification["chapter_number"] == 12
            assert verification["matched_markers"] == [EVIDENCE]
            assert len(NarrativeObligationRepository(session).list_active_for_context(project_id, chapter_number=13)) == 5
    finally:
        engine.dispose()


@pytest.mark.parametrize("source_type", ["chapter_extraction", "writer_output"])
def test_after_acceptance_uses_only_committed_extraction(monkeypatch, source_type) -> None:
    saved = []
    class ObligationRepo:
        def __init__(self, _session):
            pass

        def list_active_for_context(self, project_id, *, chapter_number):
            assert (project_id, chapter_number) == ("project-1", 12)
            return [_obligation()]

        def mark_obligation_resolved(self, obligation_id, **kwargs):
            saved.append((obligation_id, kwargs))

    class DeltaRepo:
        def __init__(self, _session):
            pass

        def list_graph_deltas(self, project_id, *, after_chapter, through_chapter):
            assert (project_id, after_chapter, through_chapter) == ("project-1", 11, 12)
            return [GraphDelta(
                id="committed", project_id=project_id, chapter_number=12, source_type=source_type,
                metadata={"plan_fulfillment": [_fulfillment().model_dump(mode="json")]},
            )]

    monkeypatch.setattr(obligation_verifier_module, "NarrativeObligationRepository", ObligationRepo)
    monkeypatch.setattr(obligation_verifier_module, "BookStateRepository", DeltaRepo, raising=False)
    result = verify_active_obligations_after_acceptance(session=object(), project_id="project-1", chapter_number=12)
    if source_type == "chapter_extraction":
        assert result["resolved_obligation_ids"] == ["obl-due"]
        assert len(saved) == 1
        obligation_id, resolution = saved[0]
        assert obligation_id == "obl-due"
        assert resolution["resolution_chapter"] == 12
        assert resolution["evidence_refs"] == ["chapter:12"]
        assert resolution["verifier_result"]["matched_markers"] == [EVIDENCE]
    else:
        assert saved == []
        assert result["resolved_obligation_ids"] == []
        assert result["warned_obligation_ids"] + result["failed_obligation_ids"] == ["obl-due"]
