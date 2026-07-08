from __future__ import annotations

from forwin.models import ArcPlanVersion, ChapterDraft, ChapterPlan, Project
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


def test_reembed_project_memories_indexes_accepted_latest_drafts_only() -> None:
    engine = get_engine(postgres_test_url("reembed_memory_index"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="检索重建", premise="前提", genre="玄幻")
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=3,
                arc_synopsis="测试 arc",
            )
            session.add(arc)
            session.flush()
            accepted = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                status="accepted",
            )
            planned = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=2,
                title="第二章",
                status="planned",
            )
            session.add_all([accepted, planned])
            session.flush()
            session.add_all(
                [
                    ChapterDraft(
                        chapter_plan_id=accepted.id,
                        version=1,
                        body_text="旧正文",
                        summary="旧摘要",
                    ),
                    ChapterDraft(
                        chapter_plan_id=accepted.id,
                        version=2,
                        body_text="新正文",
                        summary="新摘要",
                    ),
                    ChapterDraft(
                        chapter_plan_id=planned.id,
                        version=1,
                        body_text="未接受正文",
                        summary="未接受摘要",
                    ),
                ]
            )
            project_id = project.id

        memory_index = FakeMemoryIndex()
        result = reembed_project_memories(
            session_factory=Session,
            memory_index=memory_index,
            project_id=project_id,
        )

        assert result.scanned == 1
        assert result.upserted == 1
        assert memory_index.upserts == [
            {
                "project_id": project_id,
                "chapter_number": 1,
                "title": "第一章",
                "summary": "新摘要",
                "body": "新正文",
            }
        ]
    finally:
        engine.dispose()
