from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from forwin.config import InfrastructureConfig
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ChapterPlan
from forwin.generation.pipeline import ChapterPipeline
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url


def _build_pipeline(
    database_url: str,
    *,
    writer_attention_retries: int | None = None,
) -> ChapterPipeline:
    policy_payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    if writer_attention_retries is not None:
        policy_payload["writer_attention_retries"] = writer_attention_retries
    policy = RuntimePolicy.model_validate(policy_payload)
    infrastructure = InfrastructureConfig(
        database_url=database_url,
        artifact_root="/tmp/forwin-writer-attention-tests",
        retrieval_backend="qdrant",
        qdrant_url=":memory:",
        embedding_backend="hash",
        minimax_api_key="",
        minimax_model="fake-model",
    )
    return RuntimeContainer.from_config(
        infrastructure,
        policy=policy,
        role="generation_worker",
    ).build_chapter_pipeline()


class WriterAttentionFallbackTests(unittest.TestCase):
    def test_transient_classifier_accepts_529_unknown_status_code_wrapped_message(
        self,
    ) -> None:
        exc = ValueError(
            "ChapterWriter preview generation failed after retries: "
            "Server error '529 Unknown Status Code' for url "
            "'https://api.minimaxi.com/v1/chat/completions'"
        )

        self.assertTrue(ChapterPipeline._is_transient_llm_like(exc))
        self.assertTrue(ChapterPipeline._should_degrade_provisional_preview(exc))

    def test_provisional_preview_generation_failure_degrades_to_shadow_plan(
        self,
    ) -> None:
        exc = ValueError(
            "ChapterWriter preview generation failed after retries: "
            "preview response body is empty"
        )

        self.assertTrue(ChapterPipeline._should_degrade_provisional_preview(exc))

    def test_blackbox_writer_failure_uses_preview_fallback_before_needs_review(
        self,
    ) -> None:
        with TemporaryDirectory():
            db_path = postgres_test_url("writer-fallback")
            engine = get_engine(db_path)
            init_db(engine)

            pipeline = _build_pipeline(db_path)
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
                        pipeline.writer,
                        "write_chapter",
                        side_effect=TimeoutError("The read operation timed out"),
                    ),
                    patch.object(
                        pipeline.writer,
                        "write_preview_chapter",
                        return_value=preview_output,
                    ) as mocked_preview,
                ):
                    result = pipeline._write_chapter_with_attention_fallback(
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
                preview_kwargs = mocked_preview.call_args.kwargs
                self.assertEqual(
                    preview_kwargs["timeout_seconds"],
                    pipeline.writer.single_call_timeout_seconds,
                )
                self.assertFalse(preview_kwargs["retry_on_timeout"])
                updater.mark_chapter_status.assert_not_called()
                self.assertEqual(paused_chapters, [])
                self.assertEqual(frozen_artifacts, [])
            finally:
                pipeline.llm_client.close()
                pipeline.engine.dispose()
                engine.dispose()

    def test_preview_fallback_records_auditable_span_with_effective_model(self) -> None:
        with TemporaryDirectory():
            db_path = postgres_test_url("writer-preview-span")
            engine = get_engine(db_path)
            init_db(engine)

            pipeline = _build_pipeline(db_path)
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
                        pipeline.writer,
                        "write_chapter",
                        side_effect=TimeoutError("The read operation timed out"),
                    ),
                    patch.object(
                        pipeline.writer,
                        "write_preview_chapter",
                        return_value=preview_output,
                    ),
                ):
                    result = pipeline._write_chapter_with_attention_fallback(
                        context=SimpleNamespace(chapter_number=1),
                        project_id="project-1",
                        chapter_number=1,
                        updater=updater,
                        paused_chapters=[],
                        frozen_artifacts=[],
                    )

                self.assertIs(result, preview_output)
                infos = [
                    call.args[0] for call in updater.save_decision_event.call_args_list
                ]
                event_types = [info.event_type for info in infos]
                self.assertIn(
                    DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED, event_types
                )
                self.assertIn(
                    DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED, event_types
                )
                failed = next(
                    info
                    for info in infos
                    if info.event_type == DecisionEventType.LLM_REQUEST_FAILED
                )
                succeeded = next(
                    info
                    for info in infos
                    if info.event_type
                    == DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED
                )
                self.assertEqual(succeeded.parent_event_id, f"row-{failed.event_type}")
                self.assertEqual(succeeded.payload["effective_model"], "backup-model")
                self.assertEqual(
                    succeeded.payload["effective_profile_id"], "backup-profile"
                )
                self.assertEqual(succeeded.payload["successful_attempt_no"], 1)
                self.assertEqual(succeeded.payload["output_chars"], 4)
            finally:
                pipeline.llm_client.close()
                pipeline.engine.dispose()
                engine.dispose()

    def test_writer_call_receives_repair_model_preference(self) -> None:
        db_path = postgres_test_url("writer-repair-model-preference")
        pipeline = _build_pipeline(db_path)
        try:
            captured: dict[str, str] = {}

            def fake_write_chapter(
                context,  # noqa: ANN001
                *,
                llm_preferred_provider_kind: str = "",
                llm_preferred_model: str = "",
                **_kwargs,  # noqa: ANN003
            ) -> WriterOutput:
                captured["provider"] = llm_preferred_provider_kind
                captured["model"] = llm_preferred_model
                return WriterOutput(
                    project_id="project-1",
                    chapter_number=1,
                    title="第1章",
                    body="修复正文",
                    char_count=4,
                    end_of_chapter_summary="修复摘要",
                )

            updater = Mock()
            updater.save_decision_event.side_effect = lambda info: SimpleNamespace(
                id=f"row-{info.event_type}",
                causal_root_id=info.causal_root_id,
                event_type=info.event_type,
                payload=info.payload,
                parent_event_id=info.parent_event_id,
            )

            with patch.object(
                pipeline.writer, "write_chapter", side_effect=fake_write_chapter
            ):
                result = pipeline._write_chapter_with_attention_fallback(
                    context=SimpleNamespace(chapter_number=1),
                    project_id="project-1",
                    chapter_number=1,
                    updater=updater,
                    paused_chapters=[],
                    frozen_artifacts=[],
                    trace_stage_key="chapter_rewrite",
                    llm_preferred_provider_kind="deepseek",
                    llm_preferred_model="deepseek-reasoner",
                )

            self.assertIsNotNone(result)
            self.assertEqual(
                captured, {"provider": "deepseek", "model": "deepseek-reasoner"}
            )
            started = next(
                call.args[0]
                for call in updater.save_decision_event.call_args_list
                if call.args[0].event_type == DecisionEventType.LLM_REQUEST_STARTED
            )
            self.assertEqual(started.payload["preferred_provider_kind"], "deepseek")
            self.assertEqual(started.payload["preferred_model"], "deepseek-reasoner")
        finally:
            pipeline.llm_client.close()
            pipeline.engine.dispose()

    def test_transient_llm_failure_stops_before_advancing_to_next_chapter(self) -> None:
        with TemporaryDirectory():
            db_path = postgres_test_url("transient-llm")
            pipeline = _build_pipeline(
                db_path,
                writer_attention_retries=2,
            )
            try:
                pipeline.arc_director.plan_arc = lambda premise, genre, num_chapters: {
                    "arc_synopsis": "瞬时故障",
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
                            "one_line": "展开",
                            "goals": ["继续"],
                        },
                        {
                            "chapter_number": 3,
                            "title": "第三章",
                            "one_line": "转折",
                            "goals": ["升级"],
                        },
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
                    patch.object(
                        pipeline.writer, "write_chapter", side_effect=transient_fail
                    ),
                    patch.object(
                        pipeline.writer,
                        "write_preview_chapter",
                        side_effect=RuntimeError("HTTP 529 Unknown Status Code"),
                    ),
                    patch(
                        "forwin.generation.pipeline_core.writer_attention.time.sleep",
                        return_value=None,
                    ),
                ):
                    result = pipeline.run("p", "玄幻", 3)

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
                pipeline.llm_client.close()
                pipeline.engine.dispose()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_chapters, [1])
        self.assertEqual(statuses, [(1, "failed"), (2, "planned"), (3, "planned")])
        self.assertEqual(chapter_calls, [1, 1])


if __name__ == "__main__":
    unittest.main()
