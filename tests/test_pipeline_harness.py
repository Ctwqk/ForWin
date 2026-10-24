from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.book_state import BookStateRepository
from forwin.canon import CanonQualityGateOutcome
from forwin.checker.hard_floor import HardFloorResult
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline_core import project_chapters
from forwin.llm_eval.runner import _load_mini_genesis_pack, _seed_mini_real_project
from forwin.models.base import Base
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from tests.chapter_delta_helpers import stub_chapter_delta_extraction
from tests.pipeline_harness import seeded_book, write_one_chapter
from tests.postgres import postgres_test_url


@pytest.fixture
def memory_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()


def test_harness_seed_materializes_genesis_handoff(memory_session):
    project_id = seeded_book(memory_session, chapters=4, arc_ranges=[(1, 2), (3, 4)])
    with memory_session() as session:
        project = session.get(Project, project_id)
        revision = session.get(BookGenesisRevision, project.active_genesis_revision_id)
        assert project.creation_status == "writing"
        assert revision.status == "locked"
        assert BookStateRepository(session).latest_canon_chapter(project_id) == 0
        assert BookStateRepository(session).list_world_nodes(project_id)
        arcs = session.scalars(select(ArcPlanVersion).order_by(ArcPlanVersion.arc_number)).all()
        assert [(arc.chapter_start, arc.chapter_end, arc.status) for arc in arcs] == [
            (1, 2, "active"), (3, 4, "planned"),
        ]
        plans = session.scalars(select(ChapterPlan).order_by(ChapterPlan.chapter_number)).all()
        assert [plan.chapter_number for plan in plans] == [1, 2]
        assert all(plan.band_plan_id and plan.status == "planned" for plan in plans)


def test_eval_genesis_fixture_locks_and_seeds_without_chapter_plans(memory_session):
    pack = _load_mini_genesis_pack(str(Path(__file__).parent / "fixtures/llm_eval/genesis.json"))
    project_id = _seed_mini_real_project(
        SimpleNamespace(_SessionFactory=memory_session, llm_client=None), pack=pack,
        premise="fixture premise", genre="fixture genre", policy=RuntimePolicy.for_profile("standard"),
    )
    with memory_session() as session:
        project = session.get(Project, project_id)
        revision = session.get(BookGenesisRevision, project.active_genesis_revision_id)
        assert (project.premise, project.genre, project.target_total_chapters) == ("fixture premise", "fixture genre", 2)
        assert project.creation_status == "writing"
        assert revision.status == "locked"
        assert all(stage["locked"] for stage in json.loads(revision.pack_json)["stage_states"].values())
        assert BookStateRepository(session).latest_canon_chapter(project_id) == 0
        assert list(session.scalars(select(ChapterPlan))) == []


def test_harness_runs_one_chapter_from_genesis_seed(monkeypatch, tmp_path):
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        manual_checkpoints=False, band_checkpoint_action="continue",
    )
    container = RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=postgres_test_url("pipeline-harness"),
            artifact_root=str(tmp_path), qdrant_url=":memory:", embedding_backend="hash",
            minimax_api_key="", minimax_model="fake-model",
        ),
        policy=policy, role="generation_worker",
    )
    pipeline = container.build_chapter_pipeline()
    try:
        stub_chapter_delta_extraction(monkeypatch)
        monkeypatch.setattr(project_chapters, "run_hard_floor", lambda **_: HardFloorResult(passed=True))
        monkeypatch.setattr(pipeline.writer, "write_chapter", lambda context: WriterOutput(
            chapter_number=context.chapter_number, title="第一章", body="正文" * 900,
            char_count=1800, end_of_chapter_summary="ok",
        ))
        monkeypatch.setattr(pipeline.draft_review, "review", lambda **_: ReviewVerdict(verdict="pass", issues=[]))
        monkeypatch.setattr(pipeline.canon_preparation, "quality_evaluator", lambda **_: CanonQualityGateOutcome())
        project_id = seeded_book(pipeline._SessionFactory, chapters=1, policy=policy)
        with pipeline._SessionFactory() as session:
            project = session.get(Project, project_id)
            assert project.creation_status == "writing"
            assert project.active_genesis_revision_id
            assert BookStateRepository(session).latest_canon_chapter(project_id) == 0

        result = write_one_chapter(pipeline, project_id=project_id)

        assert result.status == "completed"
        assert result.completed_chapters == [1]
        assert result.failed_chapters == []
        assert result.paused_chapters == []
        with pipeline._SessionFactory() as session:
            plan = session.scalar(select(ChapterPlan).where(ChapterPlan.project_id == project_id))
            assert plan.status == "accepted"
            assert plan.band_plan_id
            assert BookStateRepository(session).latest_canon_chapter(project_id) == 1
    finally:
        pipeline.close()
