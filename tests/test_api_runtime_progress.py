from __future__ import annotations

import unittest
from types import SimpleNamespace

from forwin import api_runtime
from forwin.api_runtime import _build_task_progress_changes
from forwin.config import InfrastructureConfig
from forwin.generation.task_payload import (
    GenerationTaskExecutionPayload,
    build_execution_context,
)
from forwin.runtime.policy import RuntimePolicy


class ApiRuntimeProgressTests(unittest.TestCase):
    def test_nonterminal_generation_stages_keep_task_running(self) -> None:
        writing = _build_task_progress_changes(
            "stage_changed",
            {"stage": "writing_chapter", "current_chapter": 2},
        )
        chapter_failed = _build_task_progress_changes(
            "stage_changed",
            {"stage": "chapter_failed", "current_chapter": 1},
        )

        self.assertEqual(writing["status"], "running")
        self.assertEqual(chapter_failed["status"], "running")

    def test_terminal_failed_stage_marks_task_failed(self) -> None:
        changes = _build_task_progress_changes(
            "stage_changed",
            {"stage": "failed", "current_chapter": 2},
        )

        self.assertEqual(changes["status"], "failed")
        self.assertEqual(changes["current_stage"], "failed")


def test_task_pipeline_builder_consumes_execution_context(monkeypatch) -> None:
    infrastructure = InfrastructureConfig()
    payload = GenerationTaskExecutionPayload(
        mode="continue",
        policy_version=1,
        policy_snapshot=RuntimePolicy.for_profile("standard"),
        root_event_id="root-1",
    )
    context = build_execution_context(
        infrastructure,
        payload,
        task_id="task-1",
    )
    calls: dict[str, object] = {}
    pipeline = SimpleNamespace()

    class FakeContainer:
        @classmethod
        def from_config(cls, received_infrastructure, *, policy, role):
            calls["infrastructure"] = received_infrastructure
            calls["policy"] = policy
            calls["role"] = role
            return cls()

        def build_chapter_pipeline(self, **kwargs):
            calls.update(kwargs)
            return pipeline

    monkeypatch.setattr(api_runtime, "RuntimeContainer", FakeContainer)

    result = api_runtime._build_chapter_pipeline_for_task(context)

    assert result is pipeline
    assert calls == {
        "infrastructure": infrastructure,
        "policy": context.policy,
        "role": "generation_worker",
        "progress_callback": None,
        "should_abort": None,
        "should_pause": None,
        "task_id": "task-1",
        "root_event_id": "root-1",
    }


if __name__ == "__main__":
    unittest.main()
