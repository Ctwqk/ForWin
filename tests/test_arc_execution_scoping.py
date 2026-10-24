from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forwin.config import InfrastructureConfig
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.generation.pipeline_core.result import RunResult
from forwin.generation.pipeline_core.structural_patches import (
    evaluate_structural_patch_completion_debt,
)
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url
from tests.pipeline_harness import seeded_book


def _build_pipeline(database_url: str, *, progress_callback=None):  # noqa: ANN001
    return RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=database_url,
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
            minimax_model="fake-model",
        ),
        policy=RuntimePolicy.for_profile("standard"),
        role="generation_worker",
    ).build_chapter_pipeline(progress_callback=progress_callback)


class ArcExecutionScopingTests(unittest.TestCase):
    def test_arc_completion_blocks_unresolved_arc_patch_debt(self) -> None:
        result = evaluate_structural_patch_completion_debt(
            project_id="project-1",
            chapter_number=20,
            is_arc_final_chapter=True,
            is_book_final_chapter=False,
            active_patch_debt=[{"patch_id": "patch-arc", "target_scope": "arc"}],
        )

        self.assertFalse(result["commit_allowed"])
        self.assertIn("unresolved_arc_patch_debt:patch-arc", result["blocking_reasons"])

    def test_book_completion_blocks_unresolved_book_patch_debt(self) -> None:
        result = evaluate_structural_patch_completion_debt(
            project_id="project-1",
            chapter_number=30,
            is_arc_final_chapter=True,
            is_book_final_chapter=True,
            active_patch_debt=[{"patch_id": "patch-book", "target_scope": "book"}],
        )

        self.assertFalse(result["commit_allowed"])
        self.assertIn(
            "unresolved_book_patch_debt:patch-book", result["blocking_reasons"]
        )

    def test_continue_project_runs_every_unwritten_chapter_up_to_the_last_arc(self) -> None:
        with TemporaryDirectory():
            db_path = postgres_test_url("continue-scope")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = seeded_book(
                session_factory, chapters=4, arc_ranges=[(1, 2), (3, 4)],
            )

            captured: dict[str, object] = {}
            progress_events: list[tuple[str, dict[str, object]]] = []
            pipeline = _build_pipeline(
                db_path,
                progress_callback=lambda event, payload: progress_events.append(
                    (event, dict(payload))
                ),
            )

            def fake_run_project_chapters(**kwargs):
                captured["chapter_numbers"] = list(kwargs["chapter_numbers"])
                captured["requested_chapters"] = kwargs["requested_chapters"]
                return RunResult(
                    project_id=kwargs["project_id"],
                    requested_chapters=kwargs["requested_chapters"],
                )

            try:
                with patch.object(
                    pipeline,
                    "_run_project_chapters",
                    side_effect=fake_run_project_chapters,
                ):
                    pipeline.continue_project(project_id)
            finally:
                pipeline.llm_client.close()
                pipeline.engine.dispose()
                engine.dispose()

        # Past the active arc too: run control activates arc two when the run reaches it.
        self.assertEqual(captured["chapter_numbers"], [1, 2, 3, 4])
        self.assertEqual(captured["requested_chapters"], 4)
        resolving_payload = next(
            payload
            for event, payload in progress_events
            if event == "stage_changed"
            and payload.get("stage") == "resolving_arc_envelope"
        )
        self.assertNotIn("requested_chapters", resolving_payload)
        self.assertEqual(resolving_payload["pending_chapter_count"], 4)

if __name__ == "__main__":
    unittest.main()
