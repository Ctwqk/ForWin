from __future__ import annotations

from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.models import Project
from forwin.models.world_model import WorldModelSnapshotRow
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.world_model.api import list_conflicts
from forwin.world_model.retriever import WorldModelRetriever


def test_world_conflicts_include_open_canon_quality_signals() -> None:
    engine = get_engine(postgres_test_url("canon_quality_world_conflicts"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="冲突测试", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            CanonQualityRepository(session).save_signals(
                [
                    CanonQualitySignal(
                        signal_id="sig-terminal",
                        project_id=project.id,
                        chapter_number=3,
                        signal_type="terminal_state_active_conflict",
                        severity="error",
                        target_scope="character",
                        subject_key="character:韩砚",
                        description="韩砚终止态后继续活跃。",
                        evidence_refs=["body:1-5"],
                    )
                ]
            )
            session.commit()

        conflicts = list_conflicts(project.id, get_session=session_factory)

        assert any(conflict.conflict_type == "terminal_state_active_conflict" for conflict in conflicts)
    finally:
        engine.dispose()


def test_world_context_includes_open_canon_quality_signals() -> None:
    engine = get_engine(postgres_test_url("canon_quality_world_context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="冲突上下文", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            session.add(
                WorldModelSnapshotRow(
                    project_id=project.id,
                    as_of_chapter=0,
                    version=1,
                    status="live",
                    snapshot_json="{}",
                    source_digest="digest",
                )
            )
            CanonQualityRepository(session).save_signals(
                [
                    CanonQualitySignal(
                        signal_id="sig-countdown",
                        project_id=project.id,
                        chapter_number=5,
                        signal_type="countdown_non_monotonic",
                        severity="error",
                        target_scope="ledger",
                        subject_key="countdown:main",
                        description="倒计时回升。",
                        evidence_refs=["body:1-5"],
                    )
                ]
            )
            session.commit()
            project_id = project.id

        with session_factory() as session:
            context = WorldModelRetriever(session).build_context(
                project_id=project_id,
                chapter_number=6,
            )

        assert any(conflict.conflict_type == "countdown_non_monotonic" for conflict in context.active_world_conflicts)
    finally:
        engine.dispose()
