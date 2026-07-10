from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.genesis import PromptTrace
from forwin.models.governance import BandCheckpoint, DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.planning.future_plan_auditor import FuturePlanAuditRun
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reckless_review import RECKLESS_REVIEW_MODEL
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _governance_json(
    *,
    interval: int = 2,
    pause_enabled: bool = True,
    review_delegation_mode: str = "human",
) -> str:
    return json.dumps(
        {
            "default_operation_mode": "blackbox",
            "review_delegation_mode": review_delegation_mode,
            "progression_mode": "serial_canon_band_guard",
            "auto_band_checkpoint": True,
            "manual_checkpoints_enabled": True,
            "future_constraints_enabled": True,
            "generation_audit_interval_chapters": interval,
            "generation_audit_pause_enabled": pause_enabled,
        },
        ensure_ascii=False,
    )


class FakeSparkGateLLM:
    def __init__(self, decision: str = "approve") -> None:
        self.decision = decision
        self.last_call_result = None
        self._attempts: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs):
        content = json.dumps(
            {
                "decision": self.decision,
                "reason": "Spark delegated checkpoint decision.",
                "risk_level": "medium",
                "findings": ["checkpoint reviewed"],
                "evidence": ["checkpoint snapshot"],
            }
        )
        self.calls.append({"messages": messages, **kwargs})
        self._attempts = [
            {
                "model": RECKLESS_REVIEW_MODEL,
                "http_status": 200,
                "attempt_no": 1,
                "_raw_request_payload": {"messages": messages},
                "_raw_response_text": content,
            }
        ]
        self.last_call_result = SimpleNamespace(
            backend="codex_bridge",
            fallback_used=False,
            trace={
                "backend": "codex_bridge",
                "model": RECKLESS_REVIEW_MODEL,
                "actual_model": RECKLESS_REVIEW_MODEL,
            },
        )
        return content

    def drain_llm_attempt_events(self):
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


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
    def _setup_project(
        self,
        slug: str,
        *,
        chapter_count: int = 3,
        pause_enabled: bool = True,
        review_delegation_mode: str = "human",
    ):
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
                    governance_json=_governance_json(
                        interval=2,
                        pause_enabled=pause_enabled,
                        review_delegation_mode=review_delegation_mode,
                    ),
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

    def test_reckless_mode_delegates_generation_audit_pause_and_continues(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "generation-audit-reckless",
            chapter_count=3,
            pause_enabled=True,
            review_delegation_mode="reckless",
        )
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
                generation_audit_pause_enabled=True,
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM()
        orchestrator.llm_client = fake_llm
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1, 2, 3],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1, 2, 3])
            self.assertEqual(len(fake_llm.calls), 1)
            self.assertIn("generation_audit_pause", fake_llm.calls[0]["messages"][1]["content"])
            with session_factory() as session:
                trace = session.query(PromptTrace).filter_by(
                    project_id=project_id,
                    trace_scope="reckless_review",
                ).one()
                self.assertEqual(trace.stage_key, "reckless_generation_audit_pause")
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_generation_audit_rejection_preserves_pause(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "generation-audit-reckless-reject",
            chapter_count=3,
            pause_enabled=True,
            review_delegation_mode="reckless",
        )
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
                generation_audit_pause_enabled=True,
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM(decision="reject")
        orchestrator.llm_client = fake_llm
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
            self.assertEqual(len(fake_llm.calls), 1)
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_mode_overrides_manual_chapter_start_checkpoint(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "manual-checkpoint-reckless",
            chapter_count=1,
            review_delegation_mode="reckless",
        )
        with session_factory() as session:
            arc_id = session.query(ArcPlanVersion.id).filter_by(project_id=project_id).scalar()
            checkpoint = BandCheckpoint(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id="manual:chapter:1",
                trigger_source="manual_boundary",
                boundary_kind="chapter_start",
                boundary_chapter=1,
                status="pending",
                summary="Operator requested chapter-start review.",
            )
            session.add(checkpoint)
            session.commit()
            checkpoint_id = checkpoint.id
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM()
        orchestrator.llm_client = fake_llm
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1],
            )

            self.assertEqual(result.status, "completed")
            with session_factory() as session:
                checkpoint = session.get(BandCheckpoint, checkpoint_id)
                self.assertEqual(checkpoint.status, "overridden")
                self.assertIsNotNone(checkpoint.resolved_at)
                self.assertIn("Spark delegated", checkpoint.reason)
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_mode_overrides_post_acceptance_manual_checkpoints(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "manual-post-acceptance-reckless",
            chapter_count=1,
            review_delegation_mode="reckless",
        )
        with session_factory() as session:
            arc_id = session.query(ArcPlanVersion.id).filter_by(project_id=project_id).scalar()
            checkpoints = [
                BandCheckpoint(
                    id=new_id(),
                    project_id=project_id,
                    arc_id=arc_id,
                    band_id=f"manual:{boundary_kind}:1",
                    trigger_source="manual_boundary",
                    boundary_kind=boundary_kind,
                    boundary_chapter=1,
                    status="pending",
                    summary=f"Operator requested {boundary_kind} review.",
                )
                for boundary_kind in ("chapter_accepted", "band_end")
            ]
            session.add_all(checkpoints)
            session.commit()
            checkpoint_ids = [row.id for row in checkpoints]
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM()
        orchestrator.llm_client = fake_llm
        try:
            result = self._run_with_fast_pipeline(
                orchestrator,
                session_factory=session_factory,
                project_id=project_id,
                chapter_numbers=[1],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(fake_llm.calls), 2)
            with session_factory() as session:
                rows = [session.get(BandCheckpoint, row_id) for row_id in checkpoint_ids]
                self.assertEqual([row.status for row in rows], ["overridden", "overridden"])
                self.assertTrue(all(row.resolved_at is not None for row in rows))
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_mode_overrides_pausing_band_warning(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "band-checkpoint-reckless",
            chapter_count=2,
            review_delegation_mode="reckless",
        )
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM()
        orchestrator.llm_client = fake_llm
        checkpoint_ids: list[str] = []

        def fake_band_checkpoint(**kwargs):
            if int(kwargs["chapter_number"]) != 1:
                return None
            session = kwargs["session"]
            arc_id = session.query(ArcPlanVersion.id).filter_by(project_id=project_id).scalar()
            row = BandCheckpoint(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id="band:1:1",
                chapter_start=1,
                chapter_end=1,
                trigger_source="auto_band_end",
                boundary_kind="band_end",
                boundary_chapter=1,
                status="warn",
                summary="Warning-only checkpoint.",
                issues_json=json.dumps(
                    [{"code": "payoff_warn", "severity": "warning"}]
                ),
            )
            session.add(row)
            session.flush()
            checkpoint_ids.append(row.id)
            return row

        try:
            with patch.object(
                orchestrator,
                "_create_auto_band_checkpoint",
                side_effect=fake_band_checkpoint,
            ):
                result = self._run_with_fast_pipeline(
                    orchestrator,
                    session_factory=session_factory,
                    project_id=project_id,
                    chapter_numbers=[1, 2],
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1, 2])
            self.assertEqual(len(fake_llm.calls), 1)
            with session_factory() as session:
                checkpoint = session.get(BandCheckpoint, checkpoint_ids[0])
                self.assertEqual(checkpoint.status, "overridden")
                self.assertIsNotNone(checkpoint.resolved_at)
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_mode_delegates_band_warning_on_final_batch_chapter(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "band-checkpoint-reckless-final",
            chapter_count=1,
            review_delegation_mode="reckless",
        )
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
            )
        )
        original_llm = orchestrator.llm_client
        fake_llm = FakeSparkGateLLM()
        orchestrator.llm_client = fake_llm
        checkpoint_ids: list[str] = []

        def fake_band_checkpoint(**kwargs):
            session = kwargs["session"]
            arc_id = session.query(ArcPlanVersion.id).filter_by(project_id=project_id).scalar()
            row = BandCheckpoint(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id="band:final",
                chapter_start=1,
                chapter_end=1,
                trigger_source="auto_band_end",
                boundary_kind="band_end",
                boundary_chapter=1,
                status="warn",
                summary="Final chapter warning requires delegated review.",
            )
            session.add(row)
            session.flush()
            checkpoint_ids.append(row.id)
            return row

        try:
            with patch.object(
                orchestrator,
                "_create_auto_band_checkpoint",
                side_effect=fake_band_checkpoint,
            ):
                result = self._run_with_fast_pipeline(
                    orchestrator,
                    session_factory=session_factory,
                    project_id=project_id,
                    chapter_numbers=[1],
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(fake_llm.calls), 1)
            with session_factory() as session:
                checkpoint = session.get(BandCheckpoint, checkpoint_ids[0])
                self.assertEqual(checkpoint.status, "overridden")
                self.assertIsNotNone(checkpoint.resolved_at)
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()

    def test_reckless_mode_overrides_band_fail_and_error_checkpoints(self) -> None:
        for checkpoint_status in ("fail", "error"):
            with self.subTest(checkpoint_status=checkpoint_status):
                db_path, engine, session_factory, project_id = self._setup_project(
                    f"band-checkpoint-reckless-{checkpoint_status}",
                    chapter_count=1,
                    review_delegation_mode="reckless",
                )
                orchestrator = WritingOrchestrator(
                    Config(
                        database_url=db_path,
                        minimax_api_key="",
                        minimax_model="fake-model",
                        review_delegation_mode="reckless",
                    )
                )
                original_llm = orchestrator.llm_client
                fake_llm = FakeSparkGateLLM()
                orchestrator.llm_client = fake_llm
                checkpoint_ids: list[str] = []

                def fake_band_checkpoint(**kwargs):
                    session = kwargs["session"]
                    arc_id = session.query(ArcPlanVersion.id).filter_by(
                        project_id=project_id
                    ).scalar()
                    row = BandCheckpoint(
                        id=new_id(),
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id=f"band:{checkpoint_status}",
                        chapter_start=1,
                        chapter_end=1,
                        trigger_source="auto_band_end",
                        boundary_kind="band_end",
                        boundary_chapter=1,
                        status=checkpoint_status,
                        summary=f"{checkpoint_status} checkpoint.",
                    )
                    session.add(row)
                    session.flush()
                    checkpoint_ids.append(row.id)
                    return row

                try:
                    with patch.object(
                        orchestrator,
                        "_create_auto_band_checkpoint",
                        side_effect=fake_band_checkpoint,
                    ):
                        result = self._run_with_fast_pipeline(
                            orchestrator,
                            session_factory=session_factory,
                            project_id=project_id,
                            chapter_numbers=[1],
                        )

                    self.assertEqual(result.status, "completed")
                    self.assertEqual(len(fake_llm.calls), 1)
                    with session_factory() as session:
                        checkpoint = session.get(BandCheckpoint, checkpoint_ids[0])
                        self.assertEqual(checkpoint.status, "overridden")
                finally:
                    original_llm.close()
                    orchestrator.engine.dispose()
                    engine.dispose()

    def test_reckless_checkpoint_flush_failure_rolls_back_review_audit(self) -> None:
        db_path, engine, session_factory, project_id = self._setup_project(
            "checkpoint-override-flush-failure",
            chapter_count=1,
            review_delegation_mode="reckless",
        )
        with session_factory() as session:
            arc_id = session.query(ArcPlanVersion.id).filter_by(project_id=project_id).scalar()
            checkpoint = BandCheckpoint(
                id=new_id(),
                project_id=project_id,
                arc_id=arc_id,
                band_id="manual:flush-failure",
                trigger_source="manual_boundary",
                boundary_kind="chapter_start",
                boundary_chapter=1,
                status="pending",
                summary="Flush failure checkpoint.",
            )
            session.add(checkpoint)
            session.commit()
            checkpoint_id = checkpoint.id
        orchestrator = WritingOrchestrator(
            Config(
                database_url=db_path,
                minimax_api_key="",
                minimax_model="fake-model",
                review_delegation_mode="reckless",
            )
        )
        original_llm = orchestrator.llm_client
        orchestrator.llm_client = FakeSparkGateLLM()
        try:
            with session_factory() as session:
                updater = StateUpdater(session)
                project = session.get(Project, project_id)
                checkpoint = session.get(BandCheckpoint, checkpoint_id)
                governance = orchestrator._project_governance(project)
                original_flush = session.flush

                def fail_override_flush(*args, **kwargs):
                    if checkpoint.status == "overridden":
                        raise RuntimeError("checkpoint override flush failed")
                    return original_flush(*args, **kwargs)

                with patch.object(session, "flush", side_effect=fail_override_flush):
                    approved = orchestrator._delegate_checkpoint_if_reckless(
                        updater=updater,
                        governance=governance,
                        checkpoint=checkpoint,
                        gate_kind="manual_checkpoint_chapter_start",
                        chapter_number=1,
                    )
                session.commit()
                session.expire_all()

                self.assertFalse(approved)
                self.assertEqual(session.get(BandCheckpoint, checkpoint_id).status, "pending")
                self.assertEqual(
                    session.query(PromptTrace).filter_by(
                        project_id=project_id,
                        trace_scope="reckless_review",
                    ).count(),
                    0,
                )
        finally:
            original_llm.close()
            orchestrator.engine.dispose()
            engine.dispose()
