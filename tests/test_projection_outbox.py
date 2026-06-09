from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from forwin.api_projection_routes import build_handlers as build_projection_handlers
from forwin.book_state import BookStateRepository
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.outbox import OutboxEvent
from forwin.models.world_model import WorldModelPageRow
from forwin.outbox.worker import run_one_outbox_event
from forwin.protocol.book_state import WorldNode
from tests.postgres import postgres_test_url
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _create_project_with_node(session) -> str:
    project = Project(title="Projection Outbox", premise="测试 projection outbox。", genre="玄幻")
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
        select(func.count(WorldModelPageRow.id)).where(
            WorldModelPageRow.project_id == project_id,
            WorldModelPageRow.projection_kind == "obsidian",
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
            assert payload["as_of_chapter"] == 1
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
    assert "build_default_outbox_handlers" in source
    assert "handlers=build_default_outbox_handlers" in source
