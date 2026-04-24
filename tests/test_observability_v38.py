from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import forwin.api as api_module
from forwin.api_runtime import run_orchestrator_task
from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.genesis import PromptTrace
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.observability import LogRecorder, OperationContext, redact_payload, stack_hash
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter


class ObservabilityCoreTests(unittest.TestCase):
    def test_redaction_removes_nested_credentials_without_mutating_source(self) -> None:
        payload = {
            "api_key": "sk-secret",
            "headers": {"Authorization": "Bearer token"},
            "nested": [{"cookies": "session=abc"}, {"safe": "value"}],
            "raw_prompt": "full prompt",
        }

        redacted = redact_payload(payload)

        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"][0]["cookies"], "[REDACTED]")
        self.assertEqual(redacted["raw_prompt"], "[REDACTED]")
        self.assertEqual(payload["api_key"], "sk-secret")

    def test_log_recorder_persists_redacted_decision_event_with_context(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(str(Path(tmp) / "obs.db"))
            init_db(engine)
            session_factory = get_session_factory(engine)
            try:
                with session_factory() as session:
                    updater = StateUpdater(session)
                    project = updater.create_project(
                        title="观测测试",
                        premise="premise",
                        genre="玄幻",
                        target_total_chapters=1,
                    )
                    root = updater.save_decision_event(
                        DecisionEventInfo(
                            project_id=project.id,
                            task_id="task-1",
                            scope="task",
                            event_family="audit_action",
                            event_type=DecisionEventType.GENERATION_REQUESTED,
                            summary="root",
                        )
                    )
                    recorder = LogRecorder(updater=updater)
                    row = recorder.record_event(
                        OperationContext(
                            project_id=project.id,
                            task_id="task-1",
                            chapter_number=3,
                            stage="writing_chapter",
                            causal_root_id=root.id,
                            parent_event_id=root.id,
                        ),
                        event_family="runtime_observation",
                        event_type=DecisionEventType.TASK_OPERATION_STARTED,
                        scope="task",
                        summary="task op",
                        payload={"api_key": "sk-secret", "duration_ms": 12},
                        related_object_type="generation_task",
                        related_object_id="task-1",
                    )
                    session.commit()

                    payload_json = json.loads(row.payload_json)

                self.assertEqual(row.task_id, "task-1")
                self.assertEqual(row.chapter_number, 3)
                self.assertEqual(row.parent_event_id, root.id)
                self.assertEqual(row.causal_root_id, root.id)
                self.assertEqual(payload_json["api_key"], "[REDACTED]")
                self.assertEqual(payload_json["stage"], "writing_chapter")
                self.assertEqual(payload_json["duration_ms"], 12)
            finally:
                engine.dispose()

    def test_stack_hash_is_stable_for_same_exception_shape(self) -> None:
        def make_error() -> Exception:
            try:
                raise ValueError("same")
            except Exception as exc:  # noqa: BLE001
                return exc

        self.assertEqual(stack_hash(make_error()), stack_hash(make_error()))


class ObservabilityReadApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "read-api.db")
        self.artifact_root = Path(self.tmpdir.name) / "artifacts"
        self.old_config = api_module._config
        self.old_engine = api_module._engine
        self.old_factory = api_module._SessionFactory
        api_module._config = api_module.Config(
            db_path=self.db_path,
            artifact_root=str(self.artifact_root),
            minimax_api_key="",
        )
        api_module._engine = get_engine(self.db_path)
        init_db(api_module._engine)
        api_module._SessionFactory = get_session_factory(api_module._engine)

    def tearDown(self) -> None:
        if api_module._engine is not None:
            api_module._engine.dispose()
        api_module._config = self.old_config
        api_module._engine = self.old_engine
        api_module._SessionFactory = self.old_factory
        self.tmpdir.cleanup()

    def _seed_project(self) -> tuple[str, str, str]:
        project_id = new_id()
        arc_id = new_id()
        task_id = "task-v38"
        with api_module._get_session() as session:
            updater = StateUpdater(session)
            session.add(Project(id=project_id, title="V38", premise="p", genre="玄幻"))
            session.flush()
            session.add(
                ArcPlanVersion(
                    id=arc_id,
                    project_id=project_id,
                    version=1,
                    arc_synopsis="arc",
                    status="active",
                )
            )
            session.flush()
            plan = updater.create_chapter_plan(project_id, arc_id, 1, "第一章", "开场", ["推进"])
            plan.status = "accepted"
            session.add(
                GenerationTask(
                    id=task_id,
                    project_id=project_id,
                    status="completed",
                    current_stage="completed",
                    requested_chapters=1,
                    current_chapter=1,
                )
            )
            root = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    task_id=task_id,
                    scope="task",
                    event_family="audit_action",
                    event_type=DecisionEventType.GENERATION_REQUESTED,
                    summary="created",
                    related_object_type="generation_task",
                    related_object_id=task_id,
                )
            )
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    task_id=task_id,
                    chapter_number=1,
                    scope="chapter",
                    event_family="business_event",
                    event_type=DecisionEventType.CANON_COMMIT,
                    summary="canon",
                    payload={"duration_ms": 7},
                    parent_event_id=root.id,
                    causal_root_id=root.id,
                )
            )
            trace = updater.save_prompt_trace(
                project_id=project_id,
                trace_scope="writer",
                stage_key="chapter_draft",
                template_id="writer:single",
                attempts_json=json.dumps([{"attempt_no": 1, "model": "fake"}]),
                output_summary_json=json.dumps({"char_count": 4}),
            )
            artifact_path = self.artifact_root / "projects" / project_id / "raw.txt"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("artifact-body", encoding="utf-8")
            session.commit()
            return project_id, task_id, trace.id

    def test_task_timeline_and_prompt_trace_detail_are_queryable(self) -> None:
        project_id, task_id, trace_id = self._seed_project()

        timeline = api_module.get_task_timeline(task_id)
        trace = api_module.get_prompt_trace_detail(trace_id)

        self.assertEqual(timeline.task_id, task_id)
        self.assertEqual(timeline.project_id, project_id)
        self.assertEqual([item.event_type for item in timeline.events], ["generation_requested", "canon_commit"])
        self.assertEqual(trace.id, trace_id)
        self.assertEqual(trace.attempts[0]["attempt_no"], 1)

    def test_chapter_ledger_and_artifact_read_are_queryable_and_restricted(self) -> None:
        project_id, _task_id, trace_id = self._seed_project()
        artifact_uri = str(self.artifact_root / "projects" / project_id / "raw.txt")

        ledger = api_module.get_chapter_observability_ledger(project_id, 1)
        artifact = api_module.read_artifact_preview(uri=artifact_uri, preview_chars=8)

        self.assertEqual(ledger.project_id, project_id)
        self.assertEqual(ledger.chapter_number, 1)
        self.assertIn("canon_commit", [item.event_type for item in ledger.events])
        self.assertIn(trace_id, ledger.prompt_trace_ids)
        self.assertEqual(artifact.preview, "artifact")
        self.assertTrue(artifact.truncated)
        with self.assertRaises(api_module.HTTPException):
            api_module.read_artifact_preview(uri="/etc/passwd")


class ApiRuntimeObservabilityTests(unittest.TestCase):
    def test_run_orchestrator_task_records_success_and_cleanup_events(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(str(Path(tmp) / "runtime-success.db"))
            init_db(engine)
            session_factory = get_session_factory(engine)
            project_id = new_id()
            try:
                with session_factory() as session:
                    updater = StateUpdater(session)
                    updater.create_project("Runtime", "premise", "玄幻", target_total_chapters=1)
                    project = session.query(Project).first()
                    project_id = project.id
                    session.commit()

                class FakeCloser:
                    def __init__(self) -> None:
                        self.closed = False

                    def close(self) -> None:
                        self.closed = True

                class FakeEngine:
                    def __init__(self) -> None:
                        self.disposed = False

                    def dispose(self) -> None:
                        self.disposed = True

                fake_llm = FakeCloser()
                fake_engine = FakeEngine()
                orchestrator = type(
                    "FakeOrchestrator",
                    (),
                    {"_SessionFactory": session_factory, "llm_client": fake_llm, "engine": fake_engine},
                )()
                updates: list[dict[str, object]] = []

                result = type(
                    "RunResult",
                    (),
                    {
                        "status": "completed",
                        "project_id": project_id,
                        "failed_chapters": [],
                        "paused_chapters": [],
                        "frozen_artifacts": [],
                    },
                )()

                run_orchestrator_task(
                    "task-runtime-success",
                    orchestrator,
                    lambda: result,
                    update_task=lambda task_id, **changes: updates.append({"task_id": task_id, **changes}),
                    logger=api_module.logger,
                    error_message="runtime failed",
                    default_project_id=project_id,
                )

                with session_factory() as session:
                    rows = session.query(api_module.DecisionEvent).filter(
                        api_module.DecisionEvent.project_id == project_id,
                        api_module.DecisionEvent.task_id == "task-runtime-success",
                    ).order_by(api_module.DecisionEvent.created_at.asc()).all()
                event_types = [row.event_type for row in rows]
                self.assertIn(DecisionEventType.TASK_OPERATION_STARTED, event_types)
                self.assertIn(DecisionEventType.TASK_OPERATION_SUCCEEDED, event_types)
                self.assertIn(DecisionEventType.TASK_CLEANUP_STARTED, event_types)
                self.assertIn(DecisionEventType.TASK_CLEANUP_FINISHED, event_types)
                self.assertTrue(fake_llm.closed)
                self.assertTrue(fake_engine.disposed)
            finally:
                engine.dispose()

    def test_run_orchestrator_task_records_failure_event_with_stack_hash(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = get_engine(str(Path(tmp) / "runtime-failure.db"))
            init_db(engine)
            session_factory = get_session_factory(engine)
            try:
                with session_factory() as session:
                    updater = StateUpdater(session)
                    project = updater.create_project("Runtime", "premise", "玄幻", target_total_chapters=1)
                    project_id = project.id
                    session.commit()

                class FakeCloser:
                    def close(self) -> None:
                        return None

                class FakeEngine:
                    def dispose(self) -> None:
                        return None

                orchestrator = type(
                    "FakeOrchestrator",
                    (),
                    {"_SessionFactory": session_factory, "llm_client": FakeCloser(), "engine": FakeEngine()},
                )()

                run_orchestrator_task(
                    "task-runtime-failure",
                    orchestrator,
                    lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                    update_task=lambda *_args, **_kwargs: None,
                    logger=api_module.logger,
                    error_message="runtime failed",
                    default_project_id=project_id,
                )

                with session_factory() as session:
                    row = session.query(api_module.DecisionEvent).filter(
                        api_module.DecisionEvent.project_id == project_id,
                        api_module.DecisionEvent.task_id == "task-runtime-failure",
                        api_module.DecisionEvent.event_type == DecisionEventType.TASK_OPERATION_FAILED,
                    ).one()
                    payload = json.loads(row.payload_json)
                self.assertEqual(payload["error_class"], "RuntimeError")
                self.assertEqual(payload["error_message"], "boom")
                self.assertTrue(payload["stack_hash"])
            finally:
                engine.dispose()


class WriterPromptTraceObservabilityTests(unittest.TestCase):
    def test_prompt_trace_includes_drained_llm_attempts(self) -> None:
        class FakeClient:
            provider = "fake"
            model = "fake-model"
            base_url = "https://example.invalid/v1"
            profile_id = ""
            profile_name = ""

            def chat(self, messages, **kwargs):  # noqa: ANN001
                return "ok"

            def drain_model_fallback_events(self):
                return []

            def drain_llm_attempt_events(self):
                return [{"attempt_no": 1, "model": "fake-model", "http_status": 200}]

            def close(self):
                return None

        writer = ChapterWriter(FakeClient())

        trace = writer._build_prompt_trace(
            base_messages=[{"role": "system", "content": "sys"}],
            skill_layers=[],
            template_id="writer:test",
            stage_key="chapter_draft",
            input_snapshot={"chapter_number": 1},
            output_summary={"char_count": 10},
        )

        self.assertEqual(trace["attempts"], [{"attempt_no": 1, "model": "fake-model", "http_status": 200}])


class RetrievalObservabilityTests(unittest.TestCase):
    def test_retrieval_broker_records_context_summary_and_pruning_counts(self) -> None:
        class FakeMemoryIndex:
            def search(self, **_kwargs):
                return []

        broker = RetrievalBroker(
            context_budget_chars=700,
            max_entities=1,
            max_threads=1,
            max_summaries=1,
            memory_index=FakeMemoryIndex(),
        )

        pack = type(
            "Pack",
            (),
            {
                "previous_chapter_summaries": ["s1", "s2"],
                "active_entities": [
                    type("Entity", (), {"importance": 10, "name": "A"})(),
                    type("Entity", (), {"importance": 1, "name": "B"})(),
                ],
                "active_threads": [
                    type("Thread", (), {"priority": 1, "name": "T1"})(),
                    type("Thread", (), {"priority": 2, "name": "T2"})(),
                ],
                "active_relations": [],
                "retrieved_memories": [],
                "chapter_plan_title": "title",
                "chapter_plan_one_line": "line",
                "chapter_goals": [],
                "project_id": "project",
                "chapter_number": 2,
                "model_copy": lambda self, update: type(
                    "Pack",
                    (),
                    {**self.__dict__, **update, "model_dump": self.model_dump, "model_copy": self.model_copy},
                )(),
                "model_dump": lambda self, mode="json": {
                    "previous_chapter_summaries": self.previous_chapter_summaries,
                    "active_entities": [item.name for item in self.active_entities],
                    "active_threads": [item.name for item in self.active_threads],
                    "active_relations": [],
                    "retrieved_memories": [],
                },
            },
        )()

        broker._finalize_context_summary(base_pack=pack, pack=broker._trim_pack(pack), memories=[])

        summary = broker.last_observability_summary
        self.assertEqual(summary["summaries_count_before"], 2)
        self.assertEqual(summary["summaries_count_after"], 1)
        self.assertEqual(summary["active_entities_count_before"], 2)
        self.assertEqual(summary["active_entities_count_after"], 1)
        self.assertEqual(summary["threads_count_before"], 2)
        self.assertEqual(summary["threads_count_after"], 1)


if __name__ == "__main__":
    unittest.main()
