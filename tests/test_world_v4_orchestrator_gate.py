from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select

from forwin.canon import CanonAdmissionOutcome
from forwin.config import InfrastructureConfig
from forwin.models import DecisionEvent, Entity, EntityState
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.planning.world_contracts import ChapterWorldDeltaIntent, WorldContractRepository
from forwin.protocol.book_state import BookStateCompileResult
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.state_change import StateChangeCandidate
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _build_orchestrator(database_url: str, artifact_root: str):
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(
        update={
            "canon": policy.canon.model_copy(update={"quality_gate": "pulp_fatal"})
        }
    )
    return RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=database_url,
            artifact_root=artifact_root,
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
            minimax_model="fake-model",
        ),
        policy=policy,
        role="generation_worker",
    ).build_writing_orchestrator()


def _setup_project(session):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="BookState gate",
        premise="殖民地防线与异常通讯",
        genre="科幻",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    )
    arc = updater.create_arc_plan(project.id, "母星通讯危机", chapter_start=21, chapter_end=28)
    chapter = updater.create_chapter_plan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=23,
        title="乱码呼号",
        one_line="修复防线并收到异常通讯",
        goals=["修复防线", "处理乱码通讯"],
    )
    WorldContractRepository(session).save_chapter_intent(
        ChapterWorldDeltaIntent(
            intent_id="chapter_23_intent",
            project_id=project.id,
            chapter_plan_id=chapter.id,
            chapter_number=23,
            visible_delta_intents=["殖民地防线修复"],
            hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
            must_not_reveal=["father_sieged"],
            expected_observer_state_changes={
                "reader": "hidden -> hinted",
                "protagonist": "unknown -> suspected",
            },
        )
    )
    return project, chapter


def test_canon_admission_commits_book_state_without_projection_compatibility_event() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("orchestrator-bookstate-no-projection-compat")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            result = orchestrator.canon_admission.commit(
                runtime=orchestrator,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=23,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=23,
                    title="乱码呼号",
                    body="防线修复后，通讯台传出乱码和父亲旧部呼号。",
                    end_of_chapter_summary="收到异常通讯。",
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
            )

        with Session() as session:
            graph_deltas = session.scalar(select(func.count()).select_from(GraphDeltaRow))
            events = session.execute(select(DecisionEvent)).scalars().all()

        event_types = {event.event_type for event in events}
        payloads = "\n".join(event.payload_json or "" for event in events)
        assert isinstance(result, CanonAdmissionOutcome)
        assert not result.blocked
        assert graph_deltas > 0
        assert "legacy_projection_failed" not in event_types
        assert "projection.legacy_world_model_projection" not in payloads


def test_canon_admission_blocks_review_failure_before_book_state_commit() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("orchestrator-bookstate-review-block")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            outcome = orchestrator.canon_admission.commit(
                runtime=orchestrator,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=23,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=23,
                    title="提前揭示",
                    body="通讯接通后，父亲明确说自己已经在母星被围。",
                    end_of_chapter_summary="提前揭示母星危机。",
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
            )

        with Session() as session:
            graph_deltas = session.scalar(select(func.count()).select_from(GraphDeltaRow))

        assert isinstance(outcome, CanonAdmissionOutcome)
        assert outcome.blocked
        assert Path(outcome.blocked_path).is_file()
        assert outcome.block_kind == "book_state"
        assert graph_deltas == 0


def test_canon_admission_drops_unregistered_character_state_changes() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("orchestrator-state-filter")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            known = updater.create_entity(
                project_id=project.id,
                kind="character",
                name="陆明",
                description="主角",
                chapter=0,
            )
            result = orchestrator.canon_admission.commit(
                runtime=orchestrator,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=23,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=23,
                    title="状态候选",
                    body="陆明移动，旁人没有入册。",
                    end_of_chapter_summary="测试状态候选过滤。",
                    state_changes=[
                        StateChangeCandidate(
                            entity_name="陆明",
                            entity_kind="character",
                            field="location",
                            old_value="",
                            new_value="旧港火灾纪念碑广场",
                            reason="抵达现场",
                        ),
                        StateChangeCandidate(
                            entity_name="未入册角色",
                            entity_kind="character",
                            field="location",
                            old_value="",
                            new_value="旧港火灾纪念碑广场",
                            reason="候选输出包含未入册角色",
                        ),
                    ],
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
            )
            known_entity_id = known.id

        with Session() as session:
            known_state = session.execute(
                select(EntityState).where(EntityState.entity_id == known_entity_id)
            ).scalar_one()
            unknown_count = session.scalar(
                select(func.count())
                .select_from(Entity)
                .where(Entity.project_id == project.id, Entity.name == "未入册角色")
            )

        assert isinstance(result, CanonAdmissionOutcome)
        assert not result.blocked
        assert json.loads(known_state.state_json)["location"] == "旧港火灾纪念碑广场"
        assert unknown_count == 0


def test_canon_admission_drops_fields_unsupported_by_resolved_entity_kind() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("orchestrator-state-filter-resolved-kind")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            faction = updater.create_entity(
                project_id=project.id,
                kind="faction",
                name="灰鹞网络",
                description="记忆资产二级市场中介网络",
                chapter=0,
            )
            result = orchestrator.canon_admission.commit(
                runtime=orchestrator,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=23,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=23,
                    title="错配状态候选",
                    body="灰鹞网络提出新的交易条件。",
                    end_of_chapter_summary="测试实体类型错配的状态候选过滤。",
                    state_changes=[
                        StateChangeCandidate(
                            entity_name="灰鹞网络",
                            entity_kind="character",
                            field="possession_state",
                            old_value="",
                            new_value="持有第7枚锚点",
                            reason="抽取器误把 faction 当成 character",
                        ),
                    ],
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
            )
            faction_id = faction.id

        with Session() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(EntityState)
                .where(EntityState.entity_id == faction_id)
            )

        assert isinstance(result, CanonAdmissionOutcome)
        assert not result.blocked
        assert state_count == 0


def test_book_state_compile_failure_rolls_back_graph_deltas(monkeypatch) -> None:
    def fail_compile(self, approved_changes, *, compiler_run_id: str = ""):
        return BookStateCompileResult(
            project_id=approved_changes.project_id,
            chapter_number=approved_changes.chapter_number,
            compiler_run_id=compiler_run_id,
            committed=False,
            blocked_reasons=["forced test failure"],
        )

    monkeypatch.setattr("forwin.book_state.review_gate_ext.BookStateCompiler.compile", fail_compile)
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("orchestrator-bookstate-rollback")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            result = orchestrator.canon_admission.commit(
                runtime=orchestrator,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=23,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=23,
                    title="乱码呼号",
                    body="防线修复后，通讯台传出乱码和父亲旧部呼号。",
                    end_of_chapter_summary="收到异常通讯。",
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
            )

        with Session() as session:
            graph_deltas = session.scalar(select(func.count()).select_from(GraphDeltaRow))

        assert isinstance(result, CanonAdmissionOutcome)
        assert result.blocked
        assert Path(result.blocked_path).is_file()
        assert result.block_kind == "book_state"
        assert graph_deltas == 0


def test_accept_review_respects_canon_gate_block(monkeypatch) -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("accept-review-block")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = _build_orchestrator(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            updater = StateUpdater(session)
            project = updater.create_project(title="Accept", premise="p", genre="g")
            arc = updater.create_arc_plan(project.id, "arc", chapter_start=1, chapter_end=1)
            chapter = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="一",
                one_line="一",
                goals=["一"],
            )
            draft = ChapterDraft(chapter_plan_id=chapter.id, version=1, body_text="正文", llm_raw_response="{}")
            session.add(draft)
            session.flush()
            session.add(ChapterReview(draft_id=draft.id, verdict="pass", issues_json="[]"))

        monkeypatch.setattr(
            orchestrator,
            "_load_writer_output_from_meta",
            lambda _meta: WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="一",
                body="正文",
                end_of_chapter_summary="总结",
            ),
        )
        monkeypatch.setattr(orchestrator, "_load_review_verdict", lambda _review: ReviewVerdict(verdict="pass", issues=[]))
        monkeypatch.setattr(
            orchestrator.canon_admission,
            "commit",
            lambda **_kwargs: CanonAdmissionOutcome(
                blocked_path="book-state-review-gate-blocked",
                block_kind="book_state",
            ),
        )
        monkeypatch.setattr(orchestrator, "_run_phase3_pass", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("phase3 should not run")))
        result = orchestrator.accept_review(project.id, 1)

        with Session() as session:
            status = session.scalar(select(ChapterPlan.status).where(ChapterPlan.id == chapter.id))

        assert "needs_review" in result["message"]
        assert status == "needs_review"
