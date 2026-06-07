from __future__ import annotations

from sqlalchemy import select

from forwin.config import Config
from forwin.long_run_policy import LongRunPolicy
from forwin.models.base import get_engine, get_session_factory
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


class PassReviewHub:
    def review(self, **_kwargs) -> ReviewVerdict:
        return ReviewVerdict(
            verdict="pass",
            issues=[],
            review_summary="accepted by test reviewer",
        )


def _two_chapter_arc() -> dict[str, object]:
    return {
        "arc_synopsis": "failure stop policy",
        "setting_summary": "无",
        "chapters": [
            {
                "chapter_number": 1,
                "title": "第一章",
                "one_line": "开场",
                "goals": ["推进主线"],
            },
            {
                "chapter_number": 2,
                "title": "第二章",
                "one_line": "承接",
                "goals": ["继续推进主线"],
            },
        ],
        "characters": [],
        "locations": [],
        "factions": [],
        "relations": [],
        "plot_threads": [],
        "initial_time": {"label": "开始", "description": "开始"},
    }


def _writer_output(chapter_number: int) -> WriterOutput:
    return WriterOutput(
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        body="正文" * 900,
        char_count=1800,
        end_of_chapter_summary="ok",
        state_changes=[],
        new_events=[],
        thread_beats=[],
        time_advance=None,
    )


def _config(database_url: str, *, stop_on_chapter_failure: bool | None = None) -> Config:
    config = Config(
        database_url=database_url,
        minimax_api_key="",
        minimax_model="fake-model",
        chapter_review_form_mode="off",
        operation_mode="blackbox",
        freeze_failed_candidates=False,
        auto_band_checkpoint=False,
        manual_checkpoints_enabled=False,
    )
    if stop_on_chapter_failure is not None:
        object.__setattr__(
            config,
            "long_run_policy",
            LongRunPolicy(stop_on_chapter_failure=stop_on_chapter_failure),
        )
    return config


def test_generic_chapter_failure_stops_run_by_default() -> None:
    db_path = postgres_test_url("chapter-failure-stop-default")
    orchestrator = WritingOrchestrator(_config(db_path))
    calls: list[int] = []
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: _two_chapter_arc()
        orchestrator.review_hub = PassReviewHub()
        orchestrator._apply_canon_candidate = lambda **_kwargs: CanonApplyOutcome()
        orchestrator._strict_progression_block = lambda **_kwargs: ("", "", "")

        def write_chapter_with_fallback(**kwargs):
            chapter_number = int(kwargs["chapter_number"])
            calls.append(chapter_number)
            if chapter_number == 1:
                raise RuntimeError("generic writer failure")
            return _writer_output(chapter_number)

        orchestrator._write_chapter_with_attention_fallback = write_chapter_with_fallback

        result = orchestrator.run("p", "g", 2)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            statuses = [
                (row.chapter_number, row.status)
                for row in session.execute(
                    select(ChapterPlan).order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            ]
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert calls == [1]
    assert result.status == "failed"
    assert result.completed_chapters == []
    assert result.failed_chapters == [1]
    assert statuses == [(1, "failed"), (2, "planned")]


def test_generic_chapter_failure_can_continue_when_policy_disables_stop() -> None:
    db_path = postgres_test_url("chapter-failure-stop-disabled")
    orchestrator = WritingOrchestrator(
        _config(db_path, stop_on_chapter_failure=False)
    )
    calls: list[int] = []
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: _two_chapter_arc()
        orchestrator.review_hub = PassReviewHub()
        orchestrator._apply_canon_candidate = lambda **_kwargs: CanonApplyOutcome()
        orchestrator._strict_progression_block = lambda **_kwargs: ("", "", "")

        def write_chapter_with_fallback(**kwargs):
            chapter_number = int(kwargs["chapter_number"])
            calls.append(chapter_number)
            if chapter_number == 1:
                raise RuntimeError("generic writer failure")
            return _writer_output(chapter_number)

        orchestrator._write_chapter_with_attention_fallback = write_chapter_with_fallback

        result = orchestrator.run("p", "g", 2)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            statuses = [
                (row.chapter_number, row.status)
                for row in session.execute(
                    select(ChapterPlan).order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            ]
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert calls == [1, 2]
    assert result.status == "partial_failed"
    assert result.completed_chapters == [2]
    assert result.failed_chapters == [1]
    assert statuses == [(1, "failed"), (2, "accepted")]
