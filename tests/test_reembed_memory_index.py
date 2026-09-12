from __future__ import annotations

from forwin.models import ChapterDraft
from forwin.models.base import get_engine, get_session_factory, init_db
from scripts.reembed_memory_index import _memory_index_vector_dims, reembed_project_memories
from tests.postgres import postgres_test_url


class FakeMemoryIndex:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []

    def upsert_chapter(self, **kwargs) -> None:  # noqa: ANN003
        self.upserts.append(dict(kwargs))


class FakeDimensionedMemoryIndex(FakeMemoryIndex):
    def collection_vector_size(self) -> int:
        return 128

    def embedding_status(self) -> dict[str, object]:
        return {"kind": "gateway", "dims": 384}


def test_reembed_dimension_check_prefers_collection_vector_size() -> None:
    assert _memory_index_vector_dims(FakeDimensionedMemoryIndex()) == 128


def test_reembed_project_memories_uses_active_source_despite_newer_unaccepted_draft():
    from tests.test_projection_checkpoints import _add_project, _add_accepted_chapter
    engine = get_engine(postgres_test_url("reembed_memory_index"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            _add_project(session)
            commit = _add_accepted_chapter(session, "project-1", 1)
            session.add(ChapterDraft(chapter_plan_id=commit.chapter_plan_id, version=2, body_text="未接纳新稿", summary="未接纳摘要"))
        index = FakeMemoryIndex()
        result = reembed_project_memories(session_factory=Session, memory_index=index, project_id="project-1")
        assert result.upserted == 1
        assert index.upserts[0]["body"] == "Body 1"
        assert index.upserts[0]["canon_commit_id"] == commit.id
        assert index.upserts[0]["draft_id"] == "draft-project-1-1"
    finally:
        engine.dispose()


def test_reembed_populates_new_model_space_at_same_canon_revision_and_retains_old():
    from tests.test_projection_checkpoints import _add_project, _add_accepted_chapter
    from forwin.models import Project, ChapterPlan
    from forwin.retrieval.memory_index import QdrantChapterMemoryIndex, HashTextEmbedder
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels
    engine = get_engine(postgres_test_url("reembed-new-model"))
    init_db(engine)
    Session = get_session_factory(engine)
    client = FakeQdrantClient()
    calls = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            calls.extend(texts)
            return super().embed(texts)
    try:
        with Session.begin() as session:
            _add_project(session)
            for chapter in (1, 2):
                _add_accepted_chapter(session, "project-1", chapter)
            revision = session.get(Project, "project-1").book_revision
        def new_index(model):
            embedder = CountingEmbedder(dims=8)
            embedder.model = model
            return QdrantChapterMemoryIndex(url=":memory:", collection_name="reembed", embedder=embedder, client=client, qdrant_models=FakeQdrantModels)
        first = new_index("old-model")
        assert reembed_project_memories(session_factory=Session, memory_index=first).upserted == 2
        old_points = dict(client.collections[first.collection_name]["points"])
        second = new_index("new-model-same-dimensions")
        second._ensure_ready()
        assert client.collections[second.collection_name]["points"] == {}
        assert reembed_project_memories(session_factory=Session, memory_index=second).upserted == 2
        assert len(calls) == 4
        assert first.collection_name != second.collection_name
        assert client.collections[first.collection_name]["points"] == old_points
        assert {p.payload["chapter_number"] for p in client.collections[second.collection_name]["points"].values()} == {1, 2}
        restarted = new_index("new-model-same-dimensions")
        assert reembed_project_memories(session_factory=Session, memory_index=restarted).upserted == 2
        assert len(calls) == 4
        with Session() as session:
            assert session.get(Project, "project-1").book_revision == revision
            assert all(plan.active_commit_id for plan in session.query(ChapterPlan).all())
    finally:
        engine.dispose()


def test_reembed_dry_run_filters_and_unknown_active_source_are_not_indexed():
    from tests.test_projection_checkpoints import _add_project, _add_accepted_chapter
    from forwin.models import ChapterPlan
    engine = get_engine(postgres_test_url("reembed-filter"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            _add_project(session)
            for chapter in (1, 2, 3):
                _add_accepted_chapter(session, "project-1", chapter)
            session.get(ChapterPlan, "chapter-project-1-2").active_commit_id = None
            session.get(ChapterPlan, "chapter-project-1-3").status = "planned"
        index = FakeMemoryIndex()
        result = reembed_project_memories(session_factory=Session, memory_index=index, dry_run=True)
        assert (result.scanned, result.upserted, result.skipped_without_draft) == (2, 1, 1)
        assert index.upserts == []
        result = reembed_project_memories(session_factory=Session, memory_index=index, from_chapter=2, to_chapter=3, limit=1)
        assert (result.scanned, result.upserted, result.skipped_without_draft) == (1, 0, 1)
        assert index.upserts == []
    finally:
        engine.dispose()
