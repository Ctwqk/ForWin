from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from forwin.config import Config
from forwin.governance import (
    PlanTaskItem,
    ProjectGovernanceSettings,
    derive_chapter_task_contract,
    governance_to_json,
)
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import BandCheckpoint, NarrativeConstraint
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.reviewer import HistoricalReviewHub
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.governance_checks import (
    evaluate_director_imbalance,
    evaluate_resource_closure_risk,
    evaluate_task_contract,
)


def _fake_checker(verdict: str = "pass"):
    return SimpleNamespace(check=lambda *_args, **_kwargs: ReviewVerdict(verdict=verdict, issues=[]))


class GovernanceReviewAndCheckpointTests(unittest.TestCase):
    def test_director_imbalance_warns_without_hard_blocking(self) -> None:
        issues = evaluate_director_imbalance(
            review_metas=[
                {
                    "chapter_number": 1,
                    "planned_reward_tags": ["mystery"],
                    "delivered_reward_tags": [],
                    "review_notes": ["setup 伏笔继续铺垫"],
                    "issue_types": ["setup", "payoff_miss"],
                },
                {
                    "chapter_number": 2,
                    "planned_reward_tags": ["mystery", "emotion"],
                    "delivered_reward_tags": [],
                    "review_notes": ["setup 继续延后"],
                    "issue_types": ["setup", "payoff_miss"],
                },
            ],
            band_stall_guard=1,
            reviewer="governance",
            target_scope="band",
        )

        self.assertEqual(
            [
                (
                    issue.rule_name,
                    issue.severity,
                    issue.issue_group,
                    tuple(issue.evidence_refs),
                )
                for issue in issues
            ],
            [
                (
                    "director_payoff_consecutive_missing",
                    "warning",
                    "director_imbalance",
                    ("chapters=1,2",),
                ),
                (
                    "director_reward_gap_exceeded",
                    "warning",
                    "director_imbalance",
                    ("chapters=2", "stall_guard=1"),
                ),
                (
                    "director_setup_without_delivery",
                    "warning",
                    "director_imbalance",
                    ("setup_like_count=2", "delivery_count=0"),
                ),
                (
                    "director_mystery_without_clarification",
                    "warning",
                    "director_imbalance",
                    ("planned_mystery=2", "delivered_mystery=0"),
                ),
            ],
        )

    def test_historical_review_warns_on_unfulfilled_chapter_task(self) -> None:
        hub = HistoricalReviewHub(
            experience_review_enabled=False,
            lint_review_enabled=False,
        )
        context = ChapterContextPack(
            project_id="proj-review",
            project_title="治理测试",
            premise="premise",
            genre="玄幻",
            setting_summary="setting",
            chapter_number=3,
            chapter_plan_title="第三章",
            chapter_plan_one_line="主角推进任务",
            chapter_goals=["拿到玉佩"],
            chapter_task_contract=[
                PlanTaskItem(
                    task_type="plot_advance",
                    description="拿到玉佩",
                    target_name="玉佩",
                    source="manual",
                )
            ],
            chapter_experience_plan=ChapterExperiencePlan(),
        )
        writer_output = WriterOutput(
            chapter_number=3,
            title="第三章",
            body="主角追查旧案，但本章只确认了新的线索。",
            end_of_chapter_summary="主角继续调查，仍未完成本章核心目标。",
        )

        verdict = hub.review(
            project_id="proj-review",
            repo=None,
            context=context,
            writer_output=writer_output,
            continuity_checker=_fake_checker(),
        )

        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual(
            [
                (issue.issue_type, issue.rule_name, issue.severity, issue.description)
                for issue in verdict.issues
            ],
            [
                (
                    "plan_task_fulfillment",
                    "plan_task_unfulfilled",
                    "warning",
                    "规划任务未明显交付：拿到玉佩",
                )
            ],
        )

    def test_derived_chapter_tasks_skip_single_character_goal_fragments(self) -> None:
        tasks = derive_chapter_task_contract(["揭", "拿到玉佩"])

        self.assertEqual([task.description for task in tasks], ["拿到玉佩"])

    def test_task_contract_review_skips_stale_single_character_derived_goal(self) -> None:
        issues = evaluate_task_contract(
            [
                PlanTaskItem(
                    task_type="plot_advance",
                    description="揭",
                    source="derived_from_goals",
                )
            ],
            combined_text="本章推进了调查，但没有逐字复述坏掉的单字目标。",
            reviewer="governance",
            issue_type="plan_task_fulfillment",
            target_scope="chapter",
        )

        self.assertEqual(issues, [])

    def test_derived_chapter_tasks_skip_continuity_guard_goals(self) -> None:
        tasks = derive_chapter_task_contract(
            [
                "承接上一章 accepted canon，不改写已发生事实。",
                (
                    "第24章 accepted canon：陆明独自进入核心系统底层电梯确认启动紧急重置协议；"
                    "韩青被系统巡检员抓获；记忆重置倒计时不超过9分钟。"
                    "第25-36章必须紧接此状态。"
                ),
                "陆明在核心系统底层取得恢复后门的第一段证据。",
            ]
        )

        self.assertEqual(
            [task.description for task in tasks],
            ["陆明在核心系统底层取得恢复后门的第一段证据。"],
        )

    def test_task_contract_review_skips_continuity_guard_warning(self) -> None:
        issues = evaluate_task_contract(
            [
                PlanTaskItem(
                    task_type="plot_advance",
                    description="承接上一章 accepted canon，不改写已发生事实。",
                    source="derived_from_goals",
                ),
                PlanTaskItem(
                    task_type="plot_advance",
                    description=(
                        "第24章 accepted canon：陆明独自进入核心系统底层电梯确认启动紧急重置协议；"
                        "韩青被系统巡检员抓获；记忆重置倒计时不超过9分钟。"
                    ),
                    source="derived_from_goals",
                ),
            ],
            combined_text="陆明继续进入核心系统底层，确认韩青被关押，并推进救援线索。",
            reviewer="governance",
            issue_type="plan_task_fulfillment",
            target_scope="chapter",
        )

        self.assertEqual(issues, [])

    def test_historical_review_fails_on_hard_future_constraint(self) -> None:
        hub = HistoricalReviewHub(
            experience_review_enabled=False,
            lint_review_enabled=False,
        )
        context = ChapterContextPack(
            project_id="proj-constraint",
            project_title="治理测试",
            premise="premise",
            genre="玄幻",
            setting_summary="setting",
            chapter_number=5,
            chapter_plan_title="第五章",
            chapter_plan_one_line="危机升级",
            chapter_goals=["保留小明到后续 arc"],
            active_future_constraints=[
                {
                    "id": "nc-1",
                    "project_id": "proj-constraint",
                    "constraint_type": "character_availability",
                    "level": "hard",
                    "subject_name": "小明",
                    "description": "小明后续 arc 仍需可用",
                    "payload": {},
                    "effective_from_chapter": 1,
                    "protect_until_chapter": 20,
                    "status": "active",
                }
            ],
            chapter_experience_plan=ChapterExperiencePlan(),
        )
        writer_output = WriterOutput(
            chapter_number=5,
            title="第五章",
            body="小明在大战中阵亡。",
            end_of_chapter_summary="小明死亡，战局逆转。",
            state_changes=[
                {
                    "entity_name": "小明",
                    "entity_kind": "character",
                    "field": "status",
                    "old_value": "alive",
                    "new_value": "死亡",
                    "reason": "大战牺牲",
                }
            ],
        )

        verdict = hub.review(
            project_id="proj-constraint",
            repo=None,
            context=ChapterContextPack.model_validate(context.model_dump(mode="json")),
            writer_output=WriterOutput.model_validate(writer_output.model_dump(mode="json")),
            continuity_checker=_fake_checker(),
        )

        self.assertEqual(verdict.verdict, "fail")
        self.assertEqual(
            [
                (
                    issue.issue_type,
                    issue.rule_name,
                    issue.severity,
                    issue.description,
                    tuple(issue.evidence_refs),
                )
                for issue in verdict.issues
            ],
            [
                (
                    "future_constraint",
                    "future_constraint_violation",
                    "error",
                    "叙事约束被触发：小明后续 arc 仍需可用",
                    (
                        "constraint=nc-1",
                        "constraint_type=character_availability",
                        "state_change=小明:status->死亡",
                    ),
                )
            ],
        )

    def test_auto_band_checkpoint_warns_when_band_task_is_unfulfilled(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-band-checkpoint")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="band 治理测试",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
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
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                updater = StateUpdater(session)
                chapter_plan = updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开局推进",
                    ["拿到玉佩"],
                    task_contract=[
                        PlanTaskItem(
                            task_type="plot_advance",
                            description="拿到玉佩",
                            target_name="玉佩",
                            source="manual",
                        )
                    ],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[
                        PlanTaskItem(
                            task_type="plot_advance",
                            description="拿到玉佩",
                            target_name="玉佩",
                            source="manual",
                        )
                    ],
                )
                draft_output = WriterOutput(
                    chapter_number=1,
                    title="第一章",
                    body="主角只摸清了敌方的行动路线，还没推进关键目标。",
                    end_of_chapter_summary="主角准备继续追查下一步线索。",
                )
                updater.save_draft(
                    chapter_plan_id=chapter_plan.id,
                    writer_output=draft_output,
                    raw_response="artifact://draft-meta",
                    model_name="fake-model",
                )
                updater.mark_chapter_status(project_id, 1, "accepted")
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, updater, _checker = orchestrator._make_state_helpers(session)
                    row = orchestrator._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=1,
                    )
                    self.assertEqual(row.status, "warn")
                    self.assertEqual(
                        json.loads(row.issues_json or "[]"),
                        [
                            {
                                "code": "band_task_completion",
                                "severity": "warning",
                                "category": "",
                                "issue_group": "director_imbalance",
                                "description": "规划任务未明显交付：拿到玉佩",
                                "detail": "task_type=plot_advance; task_source=manual",
                            }
                        ],
                    )
                    row_again = orchestrator._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=1,
                    )
                    self.assertEqual(row_again.id, row.id)
                    checkpoint_count = session.query(BandCheckpoint).filter(
                        BandCheckpoint.project_id == project_id,
                        BandCheckpoint.band_id == "band-1",
                        BandCheckpoint.trigger_source == "auto_band_end",
                        BandCheckpoint.boundary_kind == "band_end",
                        BandCheckpoint.boundary_chapter == 1,
                    ).count()
                    self.assertEqual(checkpoint_count, 1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_auto_band_checkpoint_uses_band_ending_at_boundary_when_bands_overlap(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-overlap-band-checkpoint")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="overlap band checkpoint",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
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
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                updater = StateUpdater(session)
                chapter_plan = updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开局推进",
                    ["推进主线"],
                )
                updater.save_draft(
                    chapter_plan_id=chapter_plan.id,
                    writer_output=WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body="主角按计划推进主线，完成本段目标。",
                        end_of_chapter_summary="本段目标完成。",
                    ),
                    raw_response="artifact://draft-meta",
                    model_name="fake-model",
                )
                updater.mark_chapter_status(project_id, 1, "accepted")
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-ending-1",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[],
                )
                session.commit()
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-overlap",
                        chapter_start=1,
                        chapter_end=2,
                    ),
                    task_contract=[],
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, updater, _checker = orchestrator._make_state_helpers(session)
                    row = orchestrator._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=1,
                    )
                    self.assertIsNotNone(row)
                    self.assertEqual(row.band_id, "band-ending-1")
                    self.assertEqual(row.boundary_chapter, 1)
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_band_warn_action_continue_allows_next_band_after_warn_checkpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-band-warn-continue")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="band warn continue",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                band_warn_action="continue",
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
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                updater = StateUpdater(session)
                for chapter_number in (1, 2):
                    updater.create_chapter_plan(
                        project_id,
                        arc_id,
                        chapter_number,
                        f"第{chapter_number}章",
                        "推进",
                        ["推进主线"],
                    )
                    if chapter_number == 1:
                        updater.mark_chapter_status(project_id, 1, "accepted")
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-2",
                        chapter_start=2,
                        chapter_end=2,
                    ),
                    task_contract=[],
                )
                session.add(
                    BandCheckpoint(
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

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, _updater, _checker = orchestrator._make_state_helpers(session)
                    project = repo.get_project(project_id)
                    self.assertEqual(
                        orchestrator._strict_progression_block(
                            session=session,
                            repo=repo,
                            project=project,
                            chapter_number=2,
                        ),
                        ("", "", ""),
                    )
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_strict_progression_materializes_missing_auto_band_checkpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-missing-auto-checkpoint")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="missing checkpoint",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                band_warn_action="continue",
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
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                updater = StateUpdater(session)
                for chapter_number in (1, 2):
                    updater.create_chapter_plan(
                        project_id,
                        arc_id,
                        chapter_number,
                        f"第{chapter_number}章",
                        "推进",
                        ["推进主线"],
                    )
                    if chapter_number == 1:
                        updater.mark_chapter_status(project_id, 1, "accepted")
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-2",
                        chapter_start=2,
                        chapter_end=2,
                    ),
                    task_contract=[],
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, updater, _checker = orchestrator._make_state_helpers(session)
                    project = repo.get_project(project_id)
                    self.assertEqual(
                        orchestrator._strict_progression_block(
                            session=session,
                            repo=repo,
                            updater=updater,
                            project=project,
                            chapter_number=2,
                        ),
                        ("", "", ""),
                    )
                    checkpoint = repo.get_latest_band_checkpoint(project_id, band_id="band-1")
                    self.assertIsNotNone(checkpoint)
                    self.assertEqual(checkpoint.status, "pass")
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_strict_progression_ignores_superseded_arc_previous_band(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-superseded-previous-band")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            old_arc_id = new_id()
            active_arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="superseded previous band",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                            )
                        ),
                    )
                )
                session.flush()
                session.add_all(
                    [
                        ArcPlanVersion(
                            id=old_arc_id,
                            project_id=project_id,
                            version=1,
                            arc_synopsis="旧弧线",
                            status="superseded",
                        ),
                        ArcPlanVersion(
                            id=active_arc_id,
                            project_id=project_id,
                            version=2,
                            arc_synopsis="当前弧线",
                            status="active",
                        ),
                    ]
                )
                updater = StateUpdater(session)
                old_plan = updater.create_chapter_plan(
                    project_id,
                    old_arc_id,
                    1,
                    "第一章",
                    "旧 arc 章节",
                    ["旧 arc"],
                )
                updater.mark_chapter_status(project_id, 1, "accepted")
                active_plan = updater.create_chapter_plan(
                    project_id,
                    active_arc_id,
                    2,
                    "第二章",
                    "当前 arc 起点",
                    ["当前 arc"],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=old_arc_id,
                    schedule=BandDelightSchedule(
                        band_id="old-band",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=active_arc_id,
                    schedule=BandDelightSchedule(
                        band_id="active-band",
                        chapter_start=2,
                        chapter_end=3,
                    ),
                    task_contract=[],
                )
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, updater, _checker = orchestrator._make_state_helpers(session)
                    project = repo.get_project(project_id)
                    self.assertEqual(
                        orchestrator._strict_progression_block(
                            session=session,
                            repo=repo,
                            updater=updater,
                            project=project,
                            chapter_number=2,
                        ),
                        ("", "", ""),
                    )
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()

    def test_future_resource_preservation_risks_are_categorized(self) -> None:
        samples = {
            "character_locked_out": "小明彻底解决旧案后阵亡，后续再也无法登场。",
            "thread_closed_too_early": "这条长线在本章已经完全结束，主线正式落幕。",
            "relationship_closed_too_early": "两人彻底决裂，关系不可逆地结束。",
            "secret_over_explained": "隐藏身份的真相被彻底公开，秘密完全曝光。",
            "growth_arc_completed_too_early": "主角完成成长，终于成为终极形态，再无成长空间。",
        }
        for category, text in samples.items():
            with self.subTest(category=category):
                issues = evaluate_resource_closure_risk(
                    combined_text=text,
                    next_band_targets=["小明", "主线", "关系", "秘密", "主角"],
                    reviewer="governance",
                    target_scope="band",
                )
                target_name = {
                    "character_locked_out": "小明",
                    "thread_closed_too_early": "主线",
                    "relationship_closed_too_early": "关系",
                    "secret_over_explained": "秘密",
                    "growth_arc_completed_too_early": "主角",
                }[category]
                self.assertEqual(
                    [
                        (
                            issue.entity_names,
                            issue.evidence_refs,
                            issue.severity,
                            issue.issue_group,
                        )
                        for issue in issues
                    ],
                    [
                        (
                            [target_name],
                            [f"target={target_name}", f"category={category}"],
                            "warning",
                            "director_imbalance",
                        )
                    ],
                )

    def test_future_constraint_protect_until_includes_boundary_chapter(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("constraint-boundary")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)
            project_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="constraint boundary",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(future_constraints_enabled=True)
                        ),
                    )
                )
                session.flush()
                session.add(
                    NarrativeConstraint(
                        project_id=project_id,
                        constraint_type="character_availability",
                        level="hard",
                        subject_name="小明",
                        description="小明第五章结束前仍需可用",
                        effective_from_chapter=1,
                        protect_until_chapter=5,
                        status="active",
                    )
                )
                session.commit()
                repo = StateRepository(session)
                self.assertEqual(len(repo.list_active_narrative_constraints(project_id, chapter_number=5)), 1)
                self.assertEqual(len(repo.list_active_narrative_constraints(project_id, chapter_number=6)), 0)

    def test_future_constraints_enabled_controls_band_checkpoint_constraints(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = postgres_test_url("governance-constraints-toggle")
            engine = get_engine(db_path)
            init_db(engine)
            session_factory = get_session_factory(engine)

            project_id = new_id()
            arc_id = new_id()
            with session_factory() as session:
                session.add(
                    Project(
                        id=project_id,
                        title="future constraints toggle",
                        premise="premise",
                        genre="玄幻",
                        setting_summary="",
                        governance_json=governance_to_json(
                            ProjectGovernanceSettings(
                                progression_mode="serial_canon_band_guard",
                                auto_band_checkpoint=True,
                                future_constraints_enabled=False,
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
                        arc_synopsis="测试弧线",
                        status="active",
                    )
                )
                session.flush()
                session.add(
                    NarrativeConstraint(
                        project_id=project_id,
                        constraint_type="character_availability",
                        level="hard",
                        subject_name="小明",
                        description="小明后续仍需可用",
                        effective_from_chapter=1,
                        protect_until_chapter=10,
                        status="active",
                    )
                )
                updater = StateUpdater(session)
                chapter_plan = updater.create_chapter_plan(
                    project_id,
                    arc_id,
                    1,
                    "第一章",
                    "开局推进",
                    ["推进主线"],
                )
                updater.save_band_experience_plan(
                    project_id=project_id,
                    arc_id=arc_id,
                    schedule=BandDelightSchedule(
                        band_id="band-1",
                        chapter_start=1,
                        chapter_end=1,
                    ),
                    task_contract=[],
                )
                updater.save_draft(
                    chapter_plan_id=chapter_plan.id,
                    writer_output=WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body="小明在大战中阵亡，但故事继续推进。",
                        end_of_chapter_summary="小明死亡。",
                    ),
                    raw_response="artifact://draft-meta",
                    model_name="fake-model",
                )
                updater.mark_chapter_status(project_id, 1, "accepted")
                session.commit()

            orchestrator = WritingOrchestrator(
                Config(
                    database_url=db_path,
                    minimax_api_key="",
                    minimax_model="fake-model",
                    operation_mode="blackbox",
                    auto_band_checkpoint=True,
                    progression_mode="serial_canon_band_guard",
                )
            )
            try:
                with session_factory() as session:
                    repo, updater, _checker = orchestrator._make_state_helpers(session)
                    row = orchestrator._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=1,
                    )
                    self.assertEqual(row.status, "pass")
                    self.assertEqual(json.loads(row.issues_json or "[]"), [])
                    session.query(Project).filter(Project.id == project_id).update(
                        {
                            "governance_json": governance_to_json(
                                ProjectGovernanceSettings(
                                    progression_mode="serial_canon_band_guard",
                                    auto_band_checkpoint=True,
                                    future_constraints_enabled=True,
                                )
                            )
                        }
                    )
                    session.commit()
                    row = orchestrator._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=1,
                    )
                    self.assertEqual(row.status, "fail")
                    issues = json.loads(row.issues_json or "[]")
                    self.assertEqual(len(issues), 1)
                    self.assertEqual(
                        {
                            "code": issues[0]["code"],
                            "severity": issues[0]["severity"],
                            "category": issues[0]["category"],
                            "issue_group": issues[0]["issue_group"],
                            "description": issues[0]["description"],
                        },
                        {
                            "code": "next_band_compatibility",
                            "severity": "error",
                            "category": "",
                            "issue_group": "fact_conflict",
                            "description": "叙事约束被触发：小明后续仍需可用",
                        },
                    )
                    self.assertRegex(
                        issues[0]["detail"],
                        r"^constraint=[0-9a-f]+; constraint_type=character_availability; subject=小明$",
                    )
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
