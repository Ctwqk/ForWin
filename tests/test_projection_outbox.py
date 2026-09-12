from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from forwin.http.adapters.api_projection_routes import build_handlers as build_projection_handlers
from forwin.book_state import BookStateRepository
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.outbox import OutboxEvent
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.models.project import ChapterPlan
from forwin.outbox.store import enqueue_outbox_event
from forwin.outbox.worker import run_one_outbox_event
from forwin.protocol.book_state import WorldNode
from tests.postgres import postgres_test_url
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _create_project_with_node(session) -> str:
    project = Project(
        title="Projection Outbox", premise="测试 projection outbox。", genre="玄幻"
    )
    session.add(project)
    session.flush()
    BookStateRepository(session).create_world_node(
        WorldNode(
            id="char_projection_outbox",
            project_id=project.id,
            node_type="character",
            name="林烬",
            summary="旧城线主角。",
            source_refs=["chapter:1"],
        )
    )
    return project.id


def _projection_page_count(session, project_id: str) -> int:
    return session.execute(
        select(func.count(KnowledgeProjectionPageRow.id)).where(
            KnowledgeProjectionPageRow.project_id == project_id,
            KnowledgeProjectionPageRow.projection_kind == "obsidian",
        )
    ).scalar_one()


def test_projection_refresh_can_defer_to_outbox_worker(tmp_path: Path) -> None:
    from forwin.outbox.handlers import build_default_outbox_handlers

    engine, Session = _session_factory("projection-outbox")
    qdrant_client = FakeQdrantClient()
    try:
        with Session.begin() as session:
            project_id = _create_project_with_node(session)

        projection_handlers = build_projection_handlers(
            get_session=Session,
            obsidian_root=tmp_path / "vaults",
            llm_kb_root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )
        response = projection_handlers["refresh_projection"](
            project_id,
            projection_kind="all",
            as_of_chapter=1,
            defer=True,
        )

        assert response["ok"] is True
        assert response["deferred"] is True
        assert response["event_type"] == "knowledge.projection.refresh_requested"
        assert response["project_id"] == project_id
        assert response["projection_kind"] == "all"
        assert response["as_of_chapter"] == 1
        assert response["outbox_event_id"]

        with Session() as session:
            row = session.get(OutboxEvent, response["outbox_row_id"])
            assert row is not None
            assert row.status == "pending"
            payload = json.loads(row.payload_json)
            assert payload["project_id"] == project_id
            assert payload["projection_kind"] == "all"
            assert payload["requested_as_of_chapter"] == 1
            assert _projection_page_count(session, project_id) == 0

        result = run_one_outbox_event(
            session_factory=Session,
            worker_id="projection-worker-1",
            handlers=build_default_outbox_handlers(
                session_factory=Session,
                obsidian_root=tmp_path / "vaults",
                llm_kb_root=tmp_path / "kb",
                qdrant_client=qdrant_client,
                qdrant_models=FakeQdrantModels,
            ),
        )

        assert result.claimed is True
        assert result.processed is True
        with Session() as session:
            row = session.get(OutboxEvent, response["outbox_row_id"])
            assert row is not None
            assert row.status == "processed"
            assert _projection_page_count(session, project_id) >= 1
    finally:
        engine.dispose()


def test_outbox_worker_cli_registers_default_handlers() -> None:
    source = Path("forwin/cli.py").read_text(encoding="utf-8")
    assert "build_outbox_worker_runtime" in source
    assert "handlers=runtime.handlers" in source


def test_canon_projection_failure_preserves_acceptance_and_retries(
    tmp_path: Path,
) -> None:
    from forwin.canon.outbox_events import (
        CANON_PROJECTION_REQUESTED,
        canon_commit_id,
        canon_event_id,
    )
    from forwin.outbox.handlers import build_default_outbox_handlers

    engine, Session = _session_factory("canon-projection-retry")

    class MemoryIndex:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.fail = True

        def upsert_chapter(self, **kwargs) -> None:
            self.calls.append(dict(kwargs))
            if self.fail:
                raise RuntimeError("projection backend unavailable")

    memory_index = MemoryIndex()
    try:
        with Session.begin() as session:
            project = Project(
                title="Accepted projection",
                premise="Projection failure cannot undo Canon.",
                genre="thriller",
            )
            session.add(project)
            session.flush()
            from forwin.models.project import ArcPlanVersion

            arc = ArcPlanVersion(
                project_id=project.id,
                arc_synopsis="Arc one",
                chapter_start=1,
                chapter_end=1,
            )
            session.add(arc)
            session.flush()
            chapter = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="Accepted chapter",
                status="accepted",
            )
            session.add(chapter)
            session.flush()
            draft = ChapterDraft(
                chapter_plan_id=chapter.id,
                version=1,
                body_text="Accepted body",
                summary="Accepted summary",
                char_count=13,
            )
            session.add(draft)
            session.flush()
            review = ChapterReview(
                draft_id=draft.id,
                verdict="pass",
                issues_json="[]",
                review_meta_json='{"verdict":"pass"}',
            )
            session.add(review)
            session.flush()
            candidate = CandidateDraftRecord(
                project_id=project.id,
                chapter_plan_id=chapter.id,
                chapter_number=1,
                candidate_draft_id=draft.id,
                review_id=review.id,
                version=1,
                status="accepted",
                canon_status="canon",
                body_hash=__import__("hashlib").sha256(draft.body_text.encode()).hexdigest(),
                plan_revision="plan-revision",
                policy_version=1,
                idempotency_key="canon-projection-key",
            )
            session.add(candidate)
            session.flush()
            commit = CanonCommitRecord(
                id=canon_commit_id(candidate.idempotency_key),
                idempotency_key=candidate.idempotency_key,
                candidate_id=candidate.id,
                project_id=project.id,
                chapter_plan_id=chapter.id,
                chapter_title=chapter.title,
                chapter_number=1,
                status="committed",
            )
            session.add(commit)
            session.flush()
            chapter.active_commit_id = commit.id
            project.book_revision = 1
            candidate.canon_commit_id = commit.id
            session.add(candidate)
            event = enqueue_outbox_event(
                session,
                aggregate_type="project",
                aggregate_id=project.id,
                event_type=CANON_PROJECTION_REQUESTED,
                event_id=canon_event_id(
                    commit.idempotency_key,
                    CANON_PROJECTION_REQUESTED,
                ),
                payload={
                    "schema_version": 1,
                    "canon_commit_id": commit.id,
                    "canon_idempotency_key": commit.idempotency_key,
                    "project_id": project.id,
                    "chapter_number": 1,
                    "candidate_id": candidate.id,
                },
            )
            event_id = event.id
            project_id = project.id

        first = run_one_outbox_event(
            session_factory=Session,
            worker_id="canon-projection-worker",
            handlers=build_default_outbox_handlers(
                session_factory=Session,
                obsidian_root=tmp_path / "vaults",
                llm_kb_root=tmp_path / "kb",
                qdrant_client=FakeQdrantClient(),
                qdrant_models=FakeQdrantModels,
                memory_index=memory_index,
            ),
            base_delay_seconds=0,
            max_delay_seconds=0,
        )

        assert first.claimed is True
        assert first.processed is False
        with Session() as session:
            row = session.get(OutboxEvent, event_id)
            accepted = session.execute(
                select(ChapterPlan).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == 1,
                )
            ).scalar_one()
            candidate = session.execute(
                select(CandidateDraftRecord).where(
                    CandidateDraftRecord.project_id == project_id
                )
            ).scalar_one()
            assert row is not None
            assert row.status == "pending"
            assert row.attempts == 1
            assert accepted.status == "accepted"
            assert candidate.status == "accepted"
            assert len(memory_index.calls) == 1

        memory_index.fail = False
        second = run_one_outbox_event(
            session_factory=Session,
            worker_id="canon-projection-worker",
            handlers=build_default_outbox_handlers(
                session_factory=Session,
                obsidian_root=tmp_path / "vaults",
                llm_kb_root=tmp_path / "kb",
                qdrant_client=FakeQdrantClient(),
                qdrant_models=FakeQdrantModels,
                memory_index=memory_index,
            ),
            base_delay_seconds=0,
            max_delay_seconds=0,
        )

        assert second.processed is True
        assert len(memory_index.calls) == 2
        assert memory_index.calls[-1]["project_id"] == project_id
        with Session() as session:
            row = session.get(OutboxEvent, event_id)
            chapter = session.execute(
                select(ChapterPlan).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == 1,
                )
            ).scalar_one()
            assert row is not None
            assert row.status == "processed"
            assert row.attempts == 2
            assert chapter.status == "accepted"
    finally:
        engine.dispose()
