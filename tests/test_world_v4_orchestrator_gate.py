from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.models.world_v4 import WorldCompileRunV4Row, WorldDeltaRow
from forwin.models.book_state import GraphDeltaRow
from forwin.protocol.book_state import BookStateCompileResult
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.planning.world_contracts import ChapterWorldDeltaIntent, WorldContractRepository
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.state.updater import StateUpdater


def _setup_project(session):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="V4 gate",
        premise="殖民地防线与异常通讯",
        genre="科幻",
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


def test_apply_canon_candidate_runs_v4_compiler_before_legacy_state_update() -> None:
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "orchestrator-v4.db")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = WritingOrchestrator(
            Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
        )
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            result = orchestrator._apply_canon_candidate(  # noqa: SLF001
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
            compile_run = session.execute(select(WorldCompileRunV4Row)).scalar_one()
            delta_count = session.scalar(select(func.count()).select_from(WorldDeltaRow))

        assert result is None
        assert compile_run.committed is True
        retrieval_packs = json.loads(compile_run.retrieval_pack_json)
        assert retrieval_packs["writing"]["hidden_objective_truths"] == []
        assert retrieval_packs["review"]["must_not_reveal"] == ["father_sieged"]
        assert delta_count == 2


def test_apply_canon_candidate_blocks_v4_review_failure() -> None:
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "orchestrator-v4-block.db")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = WritingOrchestrator(
            Config(
                db_path=db_path,
                artifact_root=str(Path(tmp) / "artifacts"),
                minimax_api_key="",
                minimax_model="fake-model",
            )
        )
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            frozen = orchestrator._apply_canon_candidate(  # noqa: SLF001
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
            compile_run = session.execute(select(WorldCompileRunV4Row)).scalar_one()
            delta_count = session.scalar(select(func.count()).select_from(WorldDeltaRow))

        assert frozen
        assert compile_run.committed is False
        retrieval_packs = json.loads(compile_run.retrieval_pack_json)
        assert retrieval_packs["writing"]["must_not_reveal"] == ["father_sieged"]
        assert retrieval_packs["compiler"]["metadata"]["hidden_truth_included"] is True
        assert delta_count == 0


def test_book_state_compile_failure_rolls_back_v4_rows(monkeypatch) -> None:
    def fail_compile(self, approved_changes, *, compiler_run_id: str = ""):
        return BookStateCompileResult(
            project_id=approved_changes.project_id,
            chapter_number=approved_changes.chapter_number,
            compiler_run_id=compiler_run_id,
            committed=False,
            blocked_reasons=["forced test failure"],
        )

    monkeypatch.setattr("forwin.orchestrator.loop.BookStateCompiler.compile", fail_compile)
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "orchestrator-v4-bookstate-rollback.db")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = WritingOrchestrator(
            Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
        )
        with Session.begin() as session:
            repo, updater, _checker = orchestrator._make_state_helpers(session)  # noqa: SLF001
            project, _chapter = _setup_project(session)
            result = orchestrator._apply_canon_candidate(  # noqa: SLF001
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
            compile_runs = session.scalar(select(func.count()).select_from(WorldCompileRunV4Row))
            world_deltas = session.scalar(select(func.count()).select_from(WorldDeltaRow))
            graph_deltas = session.scalar(select(func.count()).select_from(GraphDeltaRow))

        assert result
        assert compile_runs == 0
        assert world_deltas == 0
        assert graph_deltas == 0


def test_accept_review_respects_canon_gate_block(monkeypatch) -> None:
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "accept-review-block.db")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        orchestrator = WritingOrchestrator(
            Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
        )
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
        monkeypatch.setattr(orchestrator, "_apply_canon_candidate", lambda **_kwargs: "book-state-review-gate-blocked")
        monkeypatch.setattr(orchestrator, "_run_phase3_pass", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("phase3 should not run")))
        monkeypatch.setattr(orchestrator, "_compile_world_model_after_acceptance", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("world compile should not run")))

        result = orchestrator.accept_review(project.id, 1)

        with Session() as session:
            status = session.scalar(select(ChapterPlan.status).where(ChapterPlan.id == chapter.id))

        assert "needs_review" in result["message"]
        assert result["frozen_artifact"] == "book-state-review-gate-blocked"
        assert status == "needs_review"
