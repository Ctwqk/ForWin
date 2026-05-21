from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.planning.future_plan_auditor import FuturePlanAuditRun
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


def _governance_json(*, interval: int = 2, pause_enabled: bool = True) -> str:
    return json.dumps(
        {
            "default_operation_mode": "blackbox",
            "progression_mode": "serial_canon_band_guard",
            "auto_band_checkpoint": True,
            "manual_checkpoints_enabled": True,
            "future_constraints_enabled": True,
            "generation_audit_interval_chapters": interval,
            "generation_audit_pause_enabled": pause_enabled,
        },
        ensure_ascii=False,
    )


def _writer_output(project_id: str, chapter_number: int) -> WriterOutput:
    return WriterOutput(
        project_id=project_id,
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        body=f"第{chapter_number}章正文。" * 120,
        char_count=800,
        end_of_chapter_summary=f"第{chapter_number}章摘要。",
    )


class GenerationAuditCheckpointTests(unittest.TestCase):
    def _setup_project(self, slug: str, *, chapter_count: int = 3, pause_enabled: bool = True):
        db_path = postgres_test_url(slug)
        engine = get_engine(db_path)
        init_db(engine)
        session_factory = get_session_factory(engine)
        project_id = new_id()
        arc_id = new_id()
        with session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="生成审计检查点",
                    premise="测试 premise",
                    genre="悬疑",
                    setting_summary="",
                    target_total_chapters=chapter_count,
                    governance_json=_governance_json(interval=2, pause_enabled=pause_enabled),
                )
            )
            session.flush()
            session.add(
                ArcPlanVersion(
                    id=arc_id,
                    project_id=project_id,
                    version=1,
                    arc_synopsis="测试弧线",
                    status="active",
                )
            )
            for chapter_number in range(1, chapter_count + 1):
                session.add(
                    ChapterPlan(
                        id=new_id(),
                        project_id=project_id,
                        arc_plan_id=arc_id,
                        chapter_number=chapter_number,
                        title=f"第{chapter_number}章",
                        one_line=f"第{chapter_number}章计划",
                        goals_json="[]",
                        status="planned",
                    )
                )
            session.commit()
        return db_path, engine, session_factory, project_id

    def _run_with_fast_pipeline(
        self,
        orchestrator: WritingOrchestrator,
        *,
        session_factory,
        project_id: str,
        chapter_numbers: list[int],
    ):
        def fake_write(**kwargs):
            return _writer_output(project_id, int(kwargs["chapter_number"]))

        def fake_review(**kwargs):
            return kwargs["writer_output"], ReviewVerdict(verdict="pass", issues=[]), False

        def fake_future_audit(**kwargs):
            chapter_number = int(kwargs["chapter_number"])
            return FuturePlanAuditRun(
                project_id=project_id,
                current_chapter=chapter_number,
                trigger_stage="post_acceptance",
                inspected_chapters=[chapter_number + 1],
                status="pass",
            )

        with session_factory() as session:
            repo, updater, checker = orchestrator._make_state_helpers(session)
            with (
                patch.object(orchestrator.retrieval_broker, "build_chapter_context", return_value=SimpleNamespace(canon_quality_context={})),
                patch.object(orchestrator.retrieval_broker.memory_index, "upsert_chapter", return_value=None),
                patch.object(orchestrator, "_audit_current_plan_before_write", side_effect=lambda **kwargs: kwargs["context"]),
                patch.object(orchestrator, "_write_chapter_with_attention_fallback", side_effect=fake_write),
                patch.object(orchestrator, "_review_and_maybe_rewrite", side_effect=fake_review),
                patch.object(orchestrator, "_apply_canon_candidate", return_value=""),
                patch.object(orchestrator, "_run_phase3_pass", return_value=None),
                patch.object(orchestrator, "_audit_future_plans_after_acceptance", side_effect=fake_future_audit),
                patch.object(orchestrator, "_compile_world_model_after_acceptance", return_value=True),
            ):
                return orchestrator._run_project_chapters(
                    session=session,
                    repo=repo,
                    updater=updater,
                    checker=checker,
                    project_id=project_id,
                    chapter_numbers=chapter_numbers,
                    requested_chapters=len(chapter_numbers),
                )

    def test_generation_audit_checkpoint_logs_without_pausing_by_runtime_default(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "generation-audit-checkpoint-log-only",
            chapter_count=3,
            pause_enabled=True,
        )
        orchestrator = WritingOrchestrator(
            Config(database_url=db_path, minimax_api_key="", minimax_model="fake-model")
        )
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1, 2, 3],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1, 2, 3])
            self.assertEqual(result.paused_chapters, [])
            with session_factory() as session:
                event = (
                    session.query(DecisionEvent)
                    .filter(
                        DecisionEvent.project_id == project_id,
                        DecisionEvent.event_type == DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    )
                    .one()
                )
                payload = json.loads(event.payload_json or "{}")
                self.assertEqual(payload["checkpoint_chapter"], 2)
                self.assertEqual(payload["window_start"], 1)
                self.assertEqual(payload["window_end"], 2)
                self.assertEqual(payload["accepted_chapters"], [1, 2])
                self.assertFalse(payload["pause"]["will_pause"])
                self.assertFalse(payload["pause"]["runtime_enabled"])
                self.assertEqual(payload["next_chapter"], 3)
                self.assertEqual(payload["future_plan_audit"]["status"], "pass")
        finally:
            orchestrator.llm_client.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_generation_audit_checkpoint_can_pause_when_runtime_allows_it(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "generation-audit-checkpoint-pause",
            chapter_count=3,
            pause_enabled=True,
        )
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                generation_audit_pause_enabled=True,
            )
        )
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1, 2, 3],
            )

            self.assertEqual(result.status, "paused")
            self.assertEqual(result.completed_chapters, [1, 2])
            self.assertEqual(result.paused_chapters, [2])
            with session_factory() as session:
                event = (
                    session.query(DecisionEvent)
                    .filter(
                        DecisionEvent.project_id == project_id,
                        DecisionEvent.event_type == DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    )
                    .one()
                )
                payload = json.loads(event.payload_json or "{}")
                self.assertTrue(payload["pause"]["will_pause"])
                self.assertTrue(payload["pause"]["runtime_enabled"])
        finally:
            orchestrator.llm_client.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_generation_audit_checkpoint_does_not_pause_at_last_requested_chapter(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "generation-audit-checkpoint-final",
            chapter_count=2,
        )
        orchestrator = WritingOrchestrator(
            Config(database_url=db_path, minimax_api_key="", minimax_model="fake-model")
        )
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1, 2],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1, 2])
            self.assertEqual(result.paused_chapters, [])
            with session_factory() as session:
                event = (
                    session.query(DecisionEvent)
                    .filter(
                        DecisionEvent.project_id == project_id,
                        DecisionEvent.event_type == DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
                    )
                    .one()
                )
                payload = json.loads(event.payload_json or "{}")
                self.assertFalse(payload["pause"]["will_pause"])
                self.assertEqual(payload["next_chapter"], 0)
        finally:
            orchestrator.llm_client.close()
            orchestrator.engine.dispose()
            engine.dispose()
