from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from forwin.config import Config
from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.writer import WriterOutput


class WriterAttentionFallbackTests(unittest.TestCase):
    def test_transient_classifier_accepts_529_unknown_status_code_wrapped_message(self) -> None:
        exc = ValueError(
            "ChapterWriter preview generation failed after retries: "
            "Server error '529 Unknown Status Code' for url "
            "'https://api.minimaxi.com/v1/chat/completions'"
        )

        self.assertTrue(WritingOrchestrator._is_transient_llm_like(exc))
        self.assertTrue(WritingOrchestrator._should_degrade_provisional_preview(exc))

    def test_provisional_preview_generation_failure_degrades_to_shadow_plan(self) -> None:
        exc = ValueError(
            "ChapterWriter preview generation failed after retries: "
            "preview response body is empty"
        )

        self.assertTrue(WritingOrchestrator._should_degrade_provisional_preview(exc))

    def test_blackbox_writer_failure_uses_preview_fallback_before_needs_review(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("writer-fallback")
            engine = get_engine(db_path)
            init_db(engine)

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                )
            )
            try:
                preview_output = WriterOutput(
                    project_id="project-1",
                    chapter_number=1,
                    title="第1章",
                    body="预演正文",
                    char_count=4,
                    end_of_chapter_summary="预演摘要",
                    generation_meta={"mode": "provisional_preview"},
                )
                updater = Mock()
                paused_chapters: list[int] = []
                frozen_artifacts: list[str] = []

                with (
                    patch.object(
                        orchestrator.writer,
                        "write_chapter",
                        side_effect=TimeoutError("The read operation timed out"),
                    ),
                    patch.object(
                        orchestrator.writer,
                        "write_preview_chapter",
                        return_value=preview_output,
                    ) as mocked_preview,
                ):
                    result = orchestrator._write_chapter_with_attention_fallback(
                        context=SimpleNamespace(chapter_number=1),
                        project_id="project-1",
                        chapter_number=1,
                        updater=updater,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                    )

                self.assertIs(result, preview_output)
                self.assertTrue(result.generation_meta["fallback_from_writer_error"])
                self.assertEqual(
                    result.generation_meta["writer_fallback_error"],
                    "The read operation timed out",
                )
                mocked_preview.assert_called_once()
                updater.mark_chapter_status.assert_not_called()
                self.assertEqual(paused_chapters, [])
                self.assertEqual(frozen_artifacts, [])
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_preview_fallback_records_auditable_span_with_effective_model(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("writer-preview-span")
            engine = get_engine(db_path)
            init_db(engine)

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                )
            )
            try:
                preview_output = WriterOutput(
                    project_id="project-1",
                    chapter_number=1,
                    title="第1章",
                    body="预演正文",
                    char_count=4,
                    end_of_chapter_summary="预演摘要",
                    generation_meta={
                        "mode": "provisional_preview",
                        "prompt_trace": {
                            "attempts": [
                                {
                                    "attempt_group_id": "group-1",
                                    "attempt_no": 1,
                                    "model": "backup-model",
                                    "profile_id": "backup-profile",
                                    "output_chars": 4,
                                }
                            ],
                            "output_summary": {"char_count": 4},
                        },
                    },
                )
                updater = Mock()
                updater.save_decision_event.side_effect = lambda info: SimpleNamespace(
                    id=f"row-{info.event_type}",
                    causal_root_id=info.causal_root_id,
                    event_type=info.event_type,
                    payload=info.payload,
                    parent_event_id=info.parent_event_id,
                )

                with (
                    patch.object(
                        orchestrator.writer,
                        "write_chapter",
                        side_effect=TimeoutError("The read operation timed out"),
                    ),
                    patch.object(
                        orchestrator.writer,
                        "write_preview_chapter",
                        return_value=preview_output,
                    ),
                ):
                    result = orchestrator._write_chapter_with_attention_fallback(
                        context=SimpleNamespace(chapter_number=1),
                        project_id="project-1",
                        chapter_number=1,
                        updater=updater,
                        paused_chapters=[],
                        frozen_artifacts=[],
                    )

                self.assertIs(result, preview_output)
                infos = [call.args[0] for call in updater.save_decision_event.call_args_list]
                event_types = [info.event_type for info in infos]
                self.assertIn(DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED, event_types)
                self.assertIn(DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED, event_types)
                failed = next(info for info in infos if info.event_type == DecisionEventType.LLM_REQUEST_FAILED)
                succeeded = next(
                    info for info in infos
                    if info.event_type == DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED
                )
                self.assertEqual(succeeded.parent_event_id, f"row-{failed.event_type}")
                self.assertEqual(succeeded.payload["effective_model"], "backup-model")
                self.assertEqual(succeeded.payload["effective_profile_id"], "backup-profile")
                self.assertEqual(succeeded.payload["successful_attempt_no"], 1)
                self.assertEqual(succeeded.payload["output_chars"], 4)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_transient_llm_failure_stops_before_advancing_to_next_chapter(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("transient-llm")
            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    blackbox_writer_attention_retries=2,
                    freeze_failed_candidates=False,
                )
            )
            try:
                orchestrator.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "瞬时故障",
                    "setting_summary": "无",
                    "chapters": [
                        {"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]},
                        {"chapter_number": 2, "title": "第二章", "one_line": "展开", "goals": ["继续"]},
                        {"chapter_number": 3, "title": "第三章", "one_line": "转折", "goals": ["升级"]},
                    ],
                    "characters": [],
                    "locations": [],
                    "factions": [],
                    "relations": [],
                    "plot_threads": [],
                    "initial_time": {"label": "开始", "description": "开始"},
                }

                chapter_calls: list[int] = []

                def transient_fail(context: SimpleNamespace) -> WriterOutput:
                    chapter_calls.append(int(context.chapter_number))
                    raise RuntimeError("HTTP 529 Unknown Status Code")

                with (
                    patch.object(orchestrator.writer, "write_chapter", side_effect=transient_fail),
                    patch.object(
                        orchestrator.writer,
                        "write_preview_chapter",
                        side_effect=RuntimeError("HTTP 529 Unknown Status Code"),
                    ),
                    patch("forwin.orchestrator.loop.time.sleep", return_value=None),
                ):
                    result = orchestrator.run("p", "玄幻", 3)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_chapters, [1])
        self.assertEqual(statuses, [(1, "failed"), (2, "planned"), (3, "planned")])
        self.assertEqual(chapter_calls, [1, 1])


if __name__ == "__main__":
    unittest.main()
