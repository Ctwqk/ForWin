from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import forwin.api as api_module
from fastapi import HTTPException

from forwin.config import Config
from forwin.governance import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
    DecisionEventInfo,
    PlanTaskItem,
    governance_to_json,
    ProjectGovernanceSettings,
)
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, Project
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.state.updater import StateUpdater


class GovernanceDecisionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_config = api_module._config
        self._old_engine = api_module._engine
        self._old_factory = api_module._SessionFactory

    def tearDown(self) -> None:
        if api_module._engine is not None:
            api_module._engine.dispose()
        api_module._config = self._old_config
        api_module._engine = self._old_engine
        api_module._SessionFactory = self._old_factory

    def _prime_api(self, db_path: str) -> None:
        api_module._engine = get_engine(db_path)
        init_db(api_module._engine)
        api_module._SessionFactory = get_session_factory(api_module._engine)

    def test_chapter_review_and_band_checkpoint_expose_decision_refs_and_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "governance_decisions.db")
            self._prime_api(db_path)

            project_id = new_id()
            arc_id = new_id()
            with api_module._get_session() as session:
                updater = StateUpdater(session)
                session.add(
                    Project(
                        id=project_id,
                        title="治理链路测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                future_constraints_enabled=True,
                            )
                        ),
                    )
                )
                session.flush()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="治理弧线",
                        status="active",
                    )
                )
                session.flush()
                chapter_plan = updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开场推进",
                    ["推进主线"],
                )
                writer_output = WriterOutput(
                    chapter_number=1,
                    title="第一章",
                    body="正文" * 1400,
                    char_count=2800,
                    end_of_chapter_summary="第一章总结",
                    state_changes=[],
                    new_events=[],
                    thread_beats=[],
                    time_advance=None,
                )
                draft = updater.save_draft(
                    chapter_plan_id=chapter_plan.id,
                    writer_output=writer_output,
                    raw_response="artifact://chapter-1",
                    model_name="fake-model",
                )
                review = updater.save_review(
                    draft.id,
                    ReviewVerdict(verdict="pass", issues=[]),
                )
                rewrite_attempt = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=1,
                    attempt_no=1,
                    trigger_review_id=review.id,
                    repair_scope="scene",
                    design_patch={"hook_type": "repair"},
                    source_draft_id=draft.id,
                    result_draft_id=draft.id,
                    result_verdict="pass",
                    forced_accept_applied=False,
                )
                updater.mark_chapter_status(project_id, 1, "accepted")
                checkpoint = updater.save_band_checkpoint(
                    BandCheckpointDetail(
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                        trigger_source="auto_band_end",
                        boundary_kind="band_end",
                        boundary_chapter=1,
                        status="warn",
                        summary="band checkpoint 需要人工处理。",
                        issues=[
                            BandCheckpointIssueInfo(
                                code="next_band_compatibility",
                                severity="warning",
                                description="下一 band 前提存在风险。",
                            )
                        ],
                    )
                )
                root = updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        scope="task",
                        event_family="audit_action",
                        event_type="continue_requested",
                        actor_type="api",
                        summary="继续生成任务已创建。",
                        related_object_type="generation_task",
                        related_object_id="task-governance-1",
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        chapter_number=1,
                        scope="chapter",
                        event_family="evaluation_verdict",
                        event_type="review_verdict_recorded",
                        summary="第1章 review verdict: pass",
                        related_object_type="chapter_review",
                        related_object_id=review.id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                    )
                )
                repair_started = updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        chapter_number=1,
                        scope="chapter",
                        event_family="evaluation_verdict",
                        event_type="repair_started",
                        summary="repair started",
                        related_object_type="chapter_review",
                        related_object_id=review.id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        chapter_number=1,
                        scope="chapter",
                        event_family="evaluation_verdict",
                        event_type="repair_succeeded",
                        summary="repair succeeded",
                        related_object_type="chapter_rewrite_attempt",
                        related_object_id=rewrite_attempt.id,
                        parent_event_id=repair_started.id,
                        causal_root_id=root.id,
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        chapter_number=1,
                        scope="chapter",
                        event_family="business_event",
                        event_type="canon_commit",
                        summary="canon commit",
                        related_object_type="chapter_review",
                        related_object_id=review.id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        chapter_number=1,
                        scope="chapter",
                        event_family="audit_action",
                        event_type="review_approved",
                        summary="review approved",
                        related_object_type="chapter_review",
                        related_object_id=review.id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                    )
                )
                checkpoint_created = updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        band_id="band-1",
                        chapter_number=1,
                        scope="band",
                        event_family="evaluation_verdict",
                        event_type="band_checkpoint_created",
                        summary="band checkpoint created",
                        related_object_type="band_checkpoint",
                        related_object_id=checkpoint.id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                        payload={"status": "warn"},
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        band_id="band-1",
                        chapter_number=1,
                        scope="band",
                        event_family="audit_action",
                        event_type="band_checkpoint_overridden",
                        summary="band checkpoint overridden",
                        related_object_type="band_checkpoint",
                        related_object_id=checkpoint.id,
                        parent_event_id=checkpoint_created.id,
                        causal_root_id=root.id,
                        reason="人工认为该 warn 可接受",
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id="task-governance-1",
                        band_id="band-1",
                        chapter_number=1,
                        scope="project",
                        event_family="evaluation_verdict",
                        event_type="hard_gate_hit",
                        summary="band checkpoint warn",
                        related_object_type="project",
                        related_object_id=project_id,
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                        payload={"blocking_reason": "band_checkpoint_warn"},
                    )
                )
                session.commit()

            review_payload = api_module.get_chapter_review(project_id, 1)
            self.assertTrue(review_payload.decision_refs)
            self.assertTrue(
                {"review_verdict_recorded", "repair_succeeded", "canon_commit", "review_approved"}.issubset(
                    {item.event_type for item in review_payload.decision_refs}
                )
            )

            checkpoint_payload = api_module.get_band_checkpoint(project_id, "band-1")
            self.assertTrue(
                {"band_checkpoint_created", "band_checkpoint_overridden"}.issubset(
                    {item.event_type for item in checkpoint_payload.decision_refs}
                )
            )

            events_payload = api_module.list_project_decision_events(
                project_id,
                causal_root_id=review_payload.decision_refs[0].causal_root_id,
            )
            self.assertTrue(all(item.causal_root_id == events_payload.items[0].causal_root_id for item in events_payload.items))

            replay = api_module.get_project_causal_replay(
                project_id,
                scope="task",
                task_id="task-governance-1",
            )
            self.assertIsNotNone(replay.root_event)
            assert replay.root_event is not None
            self.assertEqual(replay.root_event.event_type, "continue_requested")
            self.assertTrue(any(item.event_type == "band_checkpoint_overridden" for item in replay.timeline))
            self.assertTrue(replay.linked_review_refs)
            self.assertTrue(replay.linked_checkpoint_refs)

            insights = api_module.get_project_governance_insights(project_id)
            self.assertTrue(any(item["name"] == "band_checkpoint_override" for item in insights.top_override_rule_types))
            self.assertTrue(any(item["name"] == "人工认为该 warn 可接受" for item in insights.top_override_reasons))
            self.assertTrue(any(item["name"] == "band_checkpoint_warn" for item in insights.most_common_blocking_reasons))
            self.assertTrue(any(item["name"] == "warn" for item in insights.recent_band_checkpoint_distribution))
            self.assertTrue(insights.recommended_adjustments)
            self.assertTrue(insights.recent_examples)

    def test_reason_is_required_for_governance_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "governance_reason_required.db")
            self._prime_api(db_path)
            api_module._config = Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            api_module._orchestrator = object()

            project_id = new_id()
            arc_id = new_id()
            with api_module._get_session() as session:
                updater = StateUpdater(session)
                session.add(
                    Project(
                        id=project_id,
                        title="治理理由测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                manual_checkpoints_enabled=True,
                            )
                        ),
                    )
                )
                session.flush()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="治理弧线",
                        status="active",
                    )
                )
                updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开场推进",
                    ["推进主线"],
                )
                updater.save_band_checkpoint(
                    BandCheckpointDetail(
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                        trigger_source="auto_band_end",
                        boundary_kind="band_end",
                        boundary_chapter=1,
                        status="warn",
                        summary="band checkpoint 需要人工处理。",
                    )
                )
                session.commit()

            with self.assertRaises(HTTPException) as governance_ctx:
                api_module.update_project_governance(
                    project_id,
                    api_module.ProjectGovernanceUpdateRequest(
                        progression_mode="serial_canon",
                        reason="",
                    ),
                )
            self.assertEqual(governance_ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as manual_ctx:
                api_module.create_manual_checkpoint(
                    project_id,
                    api_module.ManualCheckpointRequest(
                        boundary_kind="band_end",
                        boundary_chapter=1,
                        reason="",
                    ),
                )
            self.assertEqual(manual_ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as checkpoint_ctx:
                api_module.approve_band_checkpoint(
                    project_id,
                    "band-1",
                    api_module.BandCheckpointApproveRequest(status="pass", reason=""),
                )
            self.assertEqual(checkpoint_ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as review_ctx:
                api_module.approve_chapter_review(
                    project_id,
                    1,
                    api_module.ChapterReviewApproveRequest(continue_generation=False, reason=""),
                )
            self.assertEqual(review_ctx.exception.status_code, 400)

    def test_task_contract_api_updates_chapter_and_band_with_audit_event(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "task_contract_api.db")
            self._prime_api(db_path)

            project_id = new_id()
            arc_id = new_id()
            with api_module._get_session() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="任务合同测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(ProjectGovernanceSettings()),
                    )
                )
                session.flush()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="治理弧线",
                        status="active",
                    )
                )
                session.flush()
                updater = StateUpdater(session)
                updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开场推进",
                    ["推进主线"],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=api_module.BandDelightSchedule(
                        band_id="band-task",
                        chapter_start=1,
                        chapter_end=2,
                    ),
                )
                session.commit()

            chapter_response = api_module.update_chapter_task_contract(
                project_id,
                1,
                api_module.TaskContractUpdateRequest(
                    items=[
                        PlanTaskItem(
                            task_type="setup",
                            description="埋下玉佩伏笔",
                            target_name="玉佩",
                            required_keywords=["玉佩"],
                            source="manual",
                        )
                    ],
                    reason="人工补齐第一章任务合同",
                ),
            )
            self.assertEqual(chapter_response.scope, "chapter")
            self.assertEqual(chapter_response.items[0].target_name, "玉佩")
            self.assertEqual(
                api_module.get_chapter_task_contract(project_id, 1).items[0].task_type,
                "setup",
            )

            band_response = api_module.update_band_task_contract(
                project_id,
                "band-task",
                api_module.TaskContractUpdateRequest(
                    items=[
                        PlanTaskItem(
                            task_type="experience_delivery",
                            description="交付一次反杀爽点",
                            target_name="power",
                            source="manual",
                        )
                    ],
                    reason="人工补齐 band 任务合同",
                ),
            )
            self.assertEqual(band_response.scope, "band")
            self.assertEqual(api_module.get_band_task_contract(project_id, "band-task").items[0].target_name, "power")

            events = api_module.list_project_decision_events(project_id, event_family="audit_action")
            self.assertGreaterEqual(
                sum(1 for event in events.items if event.event_type == "plan_task_contract_updated"),
                2,
            )

    def test_constraint_api_rejects_unknown_type_level_and_status(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "constraint_validation.db")
            self._prime_api(db_path)
            project_id = new_id()
            with api_module._get_session() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="约束校验测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(ProjectGovernanceSettings()),
                    )
                )
                session.commit()

            for payload in (
                {"constraint_type": "unknown", "level": "hard", "status": "active"},
                {"constraint_type": "character_availability", "level": "blocker", "status": "active"},
                {"constraint_type": "character_availability", "level": "hard", "status": "deleted"},
            ):
                with self.subTest(payload=payload):
                    with self.assertRaises(HTTPException) as ctx:
                        api_module.create_project_constraint(
                            project_id,
                            api_module.NarrativeConstraintCreateRequest(
                                subject_name="小明",
                                description="非法约束不应入库",
                                reason="测试非法值",
                                **payload,
                            ),
                        )
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_constraint_lifecycle_requires_reason_and_arc_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "governance_constraint_lifecycle.db")
            self._prime_api(db_path)

            project_id = new_id()
            arc_id = new_id()
            with api_module._get_session() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="约束生命周期测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                future_constraints_enabled=True,
                            )
                        ),
                    )
                )
                session.flush()
                session.add(
                    ArcPlanVersion(
                        id=arc_id,
                        project_id=project_id,
                        version=1,
                        arc_synopsis="治理弧线",
                        status="active",
                    )
                )
                session.flush()
                session.add(
                    BandExperiencePlan(
                        project_id=project_id,
                        arc_id=arc_id,
                        band_id="band-arc",
                        chapter_start=1,
                        chapter_end=2,
                    )
                )
                updater = StateUpdater(session)
                root = updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        band_id="band-arc",
                        chapter_number=1,
                        scope="band",
                        event_family="business_event",
                        event_type="run_started",
                        summary="arc run started",
                    )
                )
                updater.save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        band_id="band-arc",
                        chapter_number=2,
                        scope="chapter",
                        event_family="business_event",
                        event_type="canon_commit",
                        summary="arc canon commit",
                        parent_event_id=root.id,
                        causal_root_id=root.id,
                    )
                )
                session.commit()

            with self.assertRaises(HTTPException) as create_ctx:
                api_module.create_project_constraint(
                    project_id,
                    api_module.NarrativeConstraintCreateRequest(
                        constraint_type="character_availability",
                        level="hard",
                        subject_name="小明",
                        description="小明后续仍需可用",
                        reason="",
                    ),
                )
            self.assertEqual(create_ctx.exception.status_code, 400)

            constraint = api_module.create_project_constraint(
                project_id,
                api_module.NarrativeConstraintCreateRequest(
                    constraint_type="character_availability",
                    level="hard",
                    subject_name="小明",
                    description="小明后续仍需可用",
                    reason="人工声明未来可用性",
                ),
            )
            self.assertEqual(constraint.status, "active")

            with self.assertRaises(HTTPException) as update_ctx:
                api_module.update_project_constraint(
                    project_id,
                    constraint.id,
                    api_module.NarrativeConstraintUpdateRequest(status="inactive", reason=""),
                )
            self.assertEqual(update_ctx.exception.status_code, 400)

            archived = api_module.update_project_constraint(
                project_id,
                constraint.id,
                api_module.NarrativeConstraintUpdateRequest(status="inactive", reason="约束已过期"),
            )
            self.assertEqual(archived.status, "inactive")

            events = api_module.list_project_decision_events(project_id, related_object_id=constraint.id)
            event_types = {event.event_type for event in events.items}
            self.assertIn("constraint_created", event_types)
            self.assertIn("constraint_archived", event_types)

            replay = api_module.get_project_causal_replay(project_id, scope="arc", arc_id=arc_id)
            self.assertTrue(replay.timeline)
            self.assertTrue(any(event.band_id == "band-arc" for event in replay.timeline))


if __name__ == "__main__":
    unittest.main()
