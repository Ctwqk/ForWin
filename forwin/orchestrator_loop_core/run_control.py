from __future__ import annotations

import logging

from forwin.orchestrator_loop_core.result import ProvisionalGateSnapshot, RunResult
from forwin.orchestrator_loop_core.common import *
from forwin.planning.scenario_rehearsal_resolution import latest_blocking_scenario_rehearsal

logger = logging.getLogger(__name__)

def _bind_orchestrator_runtime_hooks(self) -> None:
    self.arc_envelope_manager.provisional_executor = self._run_provisional_band_preview
    self.arc_envelope_manager.scenario_progress_callback = (
        lambda **payload: self._emit_progress("stage_changed", **payload)
    )
    planning_services = getattr(self.arc_envelope_manager, "services", None)
    provisional_preview = getattr(planning_services, "provisional_preview", None)
    if provisional_preview is not None:
        provisional_preview.provisional_executor = self._run_provisional_band_preview
    scenario_rehearsal = getattr(planning_services, "scenario_rehearsal", None)
    if scenario_rehearsal is not None:
        scenario_rehearsal.progress_callback = (
            lambda **payload: self._emit_progress("stage_changed", **payload)
        )

# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def run(
    self,
    premise: str,
    genre: str = "玄幻",
    num_chapters: int = 3,
) -> RunResult:
    """Generate *num_chapters* chapters from a premise.

    Returns a run summary including the project ID and chapter outcomes.
    """
    session: Session = self._SessionFactory()
    try:
        repo, updater, checker = self._make_state_helpers(session)
        project_id = ""

        # Step 1: Plan arc -----------------------------------------------
        self._emit_progress(
            "stage_changed",
            stage="planning_arc",
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)
        print(f"\n{'='*60}")
        print("正在规划故事大纲...")
        print(f"{'='*60}")
        arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

        # Step 2: Create project + seed state ----------------------------
        self._emit_progress(
            "stage_changed",
            stage="creating_project",
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)
        title = arc_plan.get("arc_synopsis", premise[:30])[:60]
        setting_summary = arc_plan.get("setting_summary", "")
        project = updater.create_project(
            title=title,
            premise=premise,
            genre=genre,
            setting_summary=setting_summary,
            target_total_chapters=num_chapters,
            governance=new_project_governance(
                default_operation_mode=self.config.operation_mode,
                review_interval_chapters=self.config.review_interval_chapters,
            ).model_copy(
                update={
                    "progression_mode": self.config.progression_mode,
                    "auto_band_checkpoint": self.config.auto_band_checkpoint,
                    "band_warn_action": self.config.band_warn_action,
                    "manual_checkpoints_enabled": self.config.manual_checkpoints_enabled,
                    "future_constraints_enabled": self.config.future_constraints_enabled,
                }
            ),
        )
        project_id = project.id
        self._bind_governance_runtime(project_id=project_id, updater=updater)

        self._seed_state(updater, project_id, arc_plan, num_chapters)
        session.commit()
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="business_event",
            event_type=DecisionEventType.RUN_STARTED,
            scope="task",
            summary="生成 run 已启动。",
            related_object_type="project",
            related_object_id=project_id,
            payload={"requested_chapters": num_chapters},
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="business_event",
            event_type=DecisionEventType.PROJECT_CREATED,
            scope="project",
            summary="项目已创建并写入初始规划。",
            related_object_type="project",
            related_object_id=project_id,
            payload={"requested_chapters": num_chapters},
        )
        session.commit()
        self._emit_progress(
            "project_created",
            project_id=project_id,
            title=project.title,
            requested_chapters=num_chapters,
        )

        self._emit_progress(
            "stage_changed",
            stage="resolving_arc_envelope",
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)
        previous_provisional = self._latest_provisional_gate_snapshot(
            session,
            project_id,
        )
        self.arc_envelope_manager.ensure_active_arc_resolution(
            session=session,
            project_id=project_id,
            activation_chapter=1,
        )
        session.commit()
        blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
        if blocking_scenario is not None:
            return self._block_on_scenario_rehearsal(
                project_id=project_id,
                requested_chapters=num_chapters,
                row=blocking_scenario,
            )
        failed_provisional = self._new_failed_provisional_gate(
            session,
            project_id=project_id,
            previous_snapshot=previous_provisional,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
            scope="project",
            summary="provisional gate 已评估。",
            payload={
                "blocked": failed_provisional is not None,
                "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
            },
        )
        session.commit()
        if failed_provisional is not None:
            return self._block_on_provisional_failure(
                session=session,
                updater=updater,
                project_id=project_id,
                requested_chapters=num_chapters,
                gate=failed_provisional,
            )

        chapter_numbers = self._pending_chapter_numbers_for_active_arc(
            session=session,
            project_id=project_id,
        )
        if not chapter_numbers:
            return RunResult(
                project_id=project_id,
                requested_chapters=0,
            )

        print(f"项目创建完成: {project.title}")
        print(f"项目ID: {project_id}")

        result = self._run_project_chapters(
            session=session,
            repo=repo,
            updater=updater,
            checker=checker,
            project_id=project_id,
            chapter_numbers=chapter_numbers,
            requested_chapters=len(chapter_numbers),
        )
        print(f"\n{'='*60}")
        if result.status == "needs_review":
            print(
                "生成暂停："
                f"第 {result.paused_chapters[0]} 章被质量门阻断，需要修复或重试。"
            )
        elif result.status == "completed":
            print(f"生成完毕！本轮完成 {len(result.completed_chapters)} 章")
        else:
            print(
                "生成结束："
                f"成功 {len(result.completed_chapters)} 章，"
                f"失败 {len(result.failed_chapters)} 章"
            )
            print(
                "失败章节: "
                + ", ".join(str(chapter) for chapter in result.failed_chapters)
            )
        print(f"项目ID: {project_id}")
        print(f"数据库: {self.engine.url.render_as_string(hide_password=True)}")
        print(f"{'='*60}\n")

        self._emit_progress(
            "stage_changed",
            stage="paused_for_review" if result.status == "needs_review" else (
                "failed" if result.status in {"failed", "partial_failed"} else (
                    "cancelled" if result.status == "cancelled" else "completed"
                )
            ),
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=result.completed_chapters[-1] if result.completed_chapters else 0,
            completed_chapters=result.completed_chapters,
            failed_chapters=result.failed_chapters,
            paused_chapters=result.paused_chapters,
            frozen_artifacts=result.frozen_artifacts,
        )
        return result

    except Exception:
        session.rollback()
        self._emit_progress("stage_changed", stage="failed")
        raise
    finally:
        self._clear_governance_runtime()
        session.close()

def run_existing_project(
    self,
    project_id: str,
    *,
    num_chapters: int,
) -> RunResult:
    session: Session = self._SessionFactory()
    try:
        repo, updater, checker = self._make_state_helpers(session)
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")

        existing_plans = session.query(ChapterPlan).filter(
            ChapterPlan.project_id == project_id
        ).count()
        if existing_plans:
            session.close()
            return self.continue_project(project_id, max_chapters=num_chapters)

        premise = project.premise
        genre = project.genre or "玄幻"
        self._bind_governance_runtime(project_id=project_id, updater=updater)

        self._emit_progress(
            "stage_changed",
            stage="planning_arc",
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)

        print(f"\n{'='*60}")
        print(f"正在为项目《{project.title}》规划故事大纲...")
        print(f"{'='*60}")
        arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

        self._emit_progress(
            "stage_changed",
            stage="creating_project",
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)

        setting_summary = arc_plan.get("setting_summary", "")
        if setting_summary:
            project.setting_summary = setting_summary
        project.target_total_chapters = max(1, int(num_chapters or 1))
        if not str(project.title or "").strip():
            project.title = (arc_plan.get("arc_synopsis", premise[:30]) or "未命名项目")[:60]
        session.add(project)

        self._seed_state(updater, project_id, arc_plan, num_chapters)
        session.commit()

        self._emit_progress(
            "stage_changed",
            stage="resolving_arc_envelope",
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=0,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, num_chapters)
        previous_provisional = self._latest_provisional_gate_snapshot(
            session,
            project_id,
        )
        self.arc_envelope_manager.ensure_active_arc_resolution(
            session=session,
            project_id=project_id,
            activation_chapter=1,
        )
        session.commit()
        blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
        if blocking_scenario is not None:
            return self._block_on_scenario_rehearsal(
                project_id=project_id,
                requested_chapters=num_chapters,
                row=blocking_scenario,
            )
        failed_provisional = self._new_failed_provisional_gate(
            session,
            project_id=project_id,
            previous_snapshot=previous_provisional,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
            scope="project",
            summary="provisional gate 已评估。",
            payload={
                "blocked": failed_provisional is not None,
                "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
            },
        )
        session.commit()
        if failed_provisional is not None:
            return self._block_on_provisional_failure(
                session=session,
                updater=updater,
                project_id=project_id,
                requested_chapters=num_chapters,
                gate=failed_provisional,
            )

        chapter_numbers = self._pending_chapter_numbers_for_active_arc(
            session=session,
            project_id=project_id,
        )
        if not chapter_numbers:
            return RunResult(
                project_id=project_id,
                requested_chapters=0,
            )

        result = self._run_project_chapters(
            session=session,
            repo=repo,
            updater=updater,
            checker=checker,
            project_id=project_id,
            chapter_numbers=chapter_numbers,
            requested_chapters=len(chapter_numbers),
        )
        self._emit_progress(
            "stage_changed",
            stage="paused_for_review" if result.status == "needs_review" else (
                "failed" if result.status in {"failed", "partial_failed"} else (
                    "cancelled" if result.status == "cancelled" else "completed"
                )
            ),
            project_id=project_id,
            requested_chapters=num_chapters,
            current_chapter=result.completed_chapters[-1] if result.completed_chapters else 0,
            completed_chapters=result.completed_chapters,
            failed_chapters=result.failed_chapters,
            paused_chapters=result.paused_chapters,
            frozen_artifacts=result.frozen_artifacts,
        )
        return result
    except Exception:
        session.rollback()
        self._emit_progress("stage_changed", stage="failed", project_id=project_id)
        raise
    finally:
        self._clear_governance_runtime()
        session.close()

def _emit_progress(self, event: str, **payload: Any) -> None:
    if event == "stage_changed":
        try:
            self._record_stage_transition(payload)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring stage transition tracking error.", exc_info=True)
    if self.progress_callback is None:
        return
    try:
        self.progress_callback(event, payload)
    except Exception:  # noqa: BLE001
        logger.debug("Ignoring progress callback error.", exc_info=True)

def _bind_governance_runtime(
    self,
    *,
    project_id: str,
    updater: StateUpdater,
) -> None:
    self._governance_runtime_project_id = str(project_id or "").strip()
    self._governance_runtime_updater = updater
    self._governance_stage_name = ""
    self._governance_stage_started_at = 0.0
    self._governance_stage_chapter_number = 0
    self._governance_stage_span = None

def _clear_governance_runtime(self) -> None:
    self._finish_governance_stage_span(next_stage="", chapter_number=0)
    self._governance_runtime_project_id = ""
    self._governance_runtime_updater = None
    self._governance_stage_name = ""
    self._governance_stage_started_at = 0.0
    self._governance_stage_chapter_number = 0
    self._governance_stage_span = None

def _start_governance_stage_span(self, *, project_id: str, stage: str, chapter_number: int) -> None:
    if self._governance_stage_span is not None:
        return
    context = OperationContext(
        project_id=project_id,
        task_id=self._governance_task_id,
        chapter_number=int(chapter_number or 0),
        stage=stage,
        operation_id=self._audit_operation_id(),
    )
    span = self.observability.span(
        context,
        f"stage.{stage}",
        span_kind="stage",
        component="orchestrator",
        tags={"stage": stage},
    )
    span.__enter__()
    self._governance_stage_span = span

def _finish_governance_stage_span(self, *, next_stage: str, chapter_number: int) -> None:
    span = self._governance_stage_span
    if span is None:
        return
    try:
        span.tag("next_stage", str(next_stage or ""))
        stage_chapter_number = int(
            getattr(self, "_governance_stage_chapter_number", 0)
            or chapter_number
            or 0
        )
        if stage_chapter_number:
            span.metric("chapter_number", stage_chapter_number)
        span.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        logger.debug("Ignoring governance stage span close failure.", exc_info=True)
    finally:
        self._governance_stage_span = None

def _record_stage_transition(self, payload: dict[str, Any]) -> None:
    updater = self._governance_runtime_updater
    project_id = str(payload.get("project_id") or self._governance_runtime_project_id or "").strip()
    stage = str(payload.get("stage") or "").strip()
    if updater is None or not project_id or not stage:
        return
    now = time.perf_counter()
    chapter_number = int(payload.get("current_chapter") or 0)
    if self._governance_stage_name and self._governance_stage_name != stage:
        stage_chapter_number = int(
            getattr(self, "_governance_stage_chapter_number", 0)
            or chapter_number
            or 0
        )
        duration_ms = max(0, int((now - self._governance_stage_started_at) * 1000))
        stage_payload = {
            "stage": self._governance_stage_name,
            "next_stage": stage,
            "duration_ms": duration_ms,
        }
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=stage_chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.STAGE_EXITED,
            scope="task",
            summary=f"阶段 {self._governance_stage_name} 已结束。",
            payload=stage_payload,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=stage_chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
            scope="task",
            summary=f"阶段 {self._governance_stage_name} 用时 {duration_ms}ms。",
            payload=stage_payload,
        )
        self._finish_governance_stage_span(next_stage=stage, chapter_number=stage_chapter_number)
    if self._governance_stage_name != stage:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.STAGE_ENTERED,
            scope="task",
            summary=f"阶段 {stage} 已开始。",
            payload={"stage": stage},
        )
        self._governance_stage_name = stage
        self._governance_stage_started_at = now
        self._governance_stage_chapter_number = chapter_number
        self._start_governance_stage_span(
            project_id=project_id,
            stage=stage,
            chapter_number=chapter_number,
        )

@staticmethod
def _latest_provisional_gate_snapshot(
    session: Session,
    project_id: str,
) -> ProvisionalGateSnapshot | None:
    row = session.query(ProvisionalBandExecution).filter(
        ProvisionalBandExecution.project_id == project_id
    ).order_by(
        ProvisionalBandExecution.created_at.desc(),
        ProvisionalBandExecution.id.desc(),
    ).first()
    if row is None:
        return None
    try:
        raw_numbers = json.loads(row.chapter_numbers_json or "[]")
    except (json.JSONDecodeError, TypeError):
        raw_numbers = []
    chapter_numbers: list[int] = []
    if isinstance(raw_numbers, list):
        for item in raw_numbers:
            try:
                chapter_number = int(item)
            except (TypeError, ValueError):
                continue
            if chapter_number > 0:
                chapter_numbers.append(chapter_number)
    return ProvisionalGateSnapshot(
        id=row.id,
        aggregate_verdict=str(row.aggregate_verdict or "").strip().lower(),
        failure_count=max(0, int(row.failure_count or 0)),
        issue_count=max(0, int(row.issue_count or 0)),
        chapter_numbers=chapter_numbers,
    )

def _new_failed_provisional_gate(
    self,
    session: Session,
    *,
    project_id: str,
    previous_snapshot: ProvisionalGateSnapshot | None,
) -> ProvisionalGateSnapshot | None:
    latest = self._latest_provisional_gate_snapshot(session, project_id)
    if latest is None:
        return None
    if not bool(getattr(self.config, "provisional_preview_enabled", False)):
        return None
    if previous_snapshot is not None and latest.id == previous_snapshot.id:
        return None
    if latest.aggregate_verdict == "fail" or latest.failure_count > 0:
        return latest
    return None

def _block_on_scenario_rehearsal(
    self,
    *,
    project_id: str,
    requested_chapters: int,
    row,
) -> RunResult:
    try:
        payload = json.loads(row.report_json or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    chapter_numbers = [
        int(item)
        for item in (payload.get("chapter_numbers") or [])
        if str(item).strip().lstrip("-").isdigit()
    ]
    paused_chapters = chapter_numbers or [1]
    status = str(payload.get("resolution_status") or "manual_patch_required")
    self._emit_progress(
        "stage_changed",
        stage="paused_for_review",
        project_id=project_id,
        requested_chapters=requested_chapters,
        current_chapter=paused_chapters[0],
        completed_chapters=[],
        failed_chapters=[],
        paused_chapters=paused_chapters,
    )
    logger.warning(
        "Scenario rehearsal paused canon writing for project=%s status=%s band=%s",
        project_id,
        status,
        getattr(row, "band_id", ""),
    )
    return RunResult(
        project_id=project_id,
        requested_chapters=requested_chapters,
        paused_chapters=paused_chapters,
        paused=True,
    )

def _block_on_provisional_failure(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    requested_chapters: int,
    gate: ProvisionalGateSnapshot,
) -> RunResult:
    failed_chapters = gate.chapter_numbers or [1]
    for chapter_number in failed_chapters:
        updater.mark_chapter_status(project_id, chapter_number, "failed")
    session.commit()
    self._emit_progress(
        "stage_changed",
        stage="provisional_failed",
        project_id=project_id,
        requested_chapters=requested_chapters,
        current_chapter=failed_chapters[0],
        completed_chapters=[],
        failed_chapters=failed_chapters,
        paused_chapters=[],
    )
    logger.error(
        "Provisional preview blocked canon writing for project=%s verdict=%s failures=%d issues=%d chapters=%s",
        project_id,
        gate.aggregate_verdict,
        gate.failure_count,
        gate.issue_count,
        failed_chapters,
    )
    return RunResult(
        project_id=project_id,
        requested_chapters=requested_chapters,
        failed_chapters=failed_chapters,
    )

@staticmethod
def _pending_chapter_numbers_for_active_arc(
    *,
    session: Session,
    project_id: str,
    max_chapters: int | None = None,
) -> list[int]:
    active_arc = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "active",
        )
        .order_by(ArcPlanVersion.created_at.desc(), ArcPlanVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active_arc is None:
        return []
    chapter_numbers = list(
        session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.arc_plan_id == active_arc.id,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
    )
    if max_chapters is not None:
        chapter_numbers = chapter_numbers[: max(1, int(max_chapters or 1))]
    return chapter_numbers

def _materialize_next_genesis_arc_if_needed(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project: Project,
) -> bool:
    if str(getattr(project, "creation_status", "") or "").strip() != "writing":
        return False
    if not str(getattr(project, "active_genesis_revision_id", "") or "").strip():
        return False
    revision = self.book_genesis.active_revision(session, project)
    if revision is None:
        return False
    remaining_pending = session.execute(
        select(ChapterPlan.chapter_number)
        .where(
            ChapterPlan.project_id == project.id,
            ChapterPlan.status.in_(("planned", "failed")),
        )
        .limit(1)
    ).scalar_one_or_none()
    if remaining_pending is not None:
        return False
    promoted = self.book_genesis.promote_next_arc_if_needed(
        session=session,
        updater=updater,
        project=project,
        revision=revision,
    )
    if promoted:
        session.commit()
    return promoted

def continue_project(
    self,
    project_id: str,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
) -> RunResult:
    session: Session = self._SessionFactory()
    try:
        repo, updater, checker = self._make_state_helpers(session)
        self._bind_governance_runtime(project_id=project_id, updater=updater)
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")

        chapter_plans = session.query(ChapterPlan).filter(
            ChapterPlan.project_id == project_id
        ).order_by(ChapterPlan.chapter_number).all()
        if not chapter_plans:
            raise ValueError(f"项目没有章节规划: {project_id}")

        waiting_review: list[int] = []
        for plan in chapter_plans:
            if plan.status != "needs_review":
                continue
            latest_draft = session.query(ChapterDraft).filter(
                ChapterDraft.chapter_plan_id == plan.id
            ).order_by(ChapterDraft.version.desc()).first()
            if latest_draft is None:
                logger.warning(
                    "Resetting orphan needs_review chapter back to planned for project=%s chapter=%d",
                    project_id,
                    plan.chapter_number,
                )
                plan.status = "planned"
                session.add(plan)
                continue
            waiting_review.append(plan.chapter_number)
        if waiting_review:
            waiting = ", ".join(str(number) for number in waiting_review)
            raise ValueError(f"仍有章节等待 review：{waiting}")
        session.commit()
        repo, updater, checker = self._make_state_helpers(session)
        workset = build_continue_generation_workset(
            session,
            project_id,
            max_chapters=max_chapters,
            resume_from_chapter=resume_from_chapter,
            source="orchestrator_continue",
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="business_event",
            event_type=DecisionEventType.RUN_STARTED,
            scope="task",
            summary="已有项目生成 run 已启动。",
            related_object_type="project",
            related_object_id=project_id,
            payload={
                "resolved_workset_count": workset.requested_chapters,
                "materialized_plan_count": workset.materialized_plan_count,
                "workset_reason": workset.reason,
            },
        )
        session.commit()

        pending_chapter_numbers = list(workset.chapter_numbers)
        if workset.reason == "future_arc_materialization_required":
            if self._materialize_next_genesis_arc_if_needed(
                session=session,
                updater=updater,
                project=project,
            ):
                repo, updater, checker = self._make_state_helpers(session)
                workset = build_continue_generation_workset(
                    session,
                    project_id,
                    max_chapters=max_chapters,
                    resume_from_chapter=resume_from_chapter,
                    source="orchestrator_continue",
                )
                pending_chapter_numbers = list(workset.chapter_numbers)
        if not pending_chapter_numbers:
            return RunResult(
                project_id=project_id,
                requested_chapters=0,
            )

        self._emit_progress(
            "stage_changed",
            stage="resolving_arc_envelope",
            project_id=project_id,
            pending_chapter_count=len(pending_chapter_numbers),
            resolved_workset_count=workset.requested_chapters,
            current_chapter=min(pending_chapter_numbers) - 1,
        )
        if self._abort_requested():
            return self._cancelled_result(project_id, len(pending_chapter_numbers))
        previous_provisional = self._latest_provisional_gate_snapshot(
            session,
            project_id,
        )
        self.arc_envelope_manager.ensure_active_arc_resolution(
            session=session,
            project_id=project_id,
            activation_chapter=min(pending_chapter_numbers),
        )
        session.commit()
        blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
        if blocking_scenario is not None:
            return self._block_on_scenario_rehearsal(
                project_id=project_id,
                requested_chapters=len(pending_chapter_numbers),
                row=blocking_scenario,
            )
        failed_provisional = self._new_failed_provisional_gate(
            session,
            project_id=project_id,
            previous_snapshot=previous_provisional,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
            scope="project",
            summary="provisional gate 已评估。",
            payload={
                "blocked": failed_provisional is not None,
                "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
            },
        )
        session.commit()
        if failed_provisional is not None:
            return self._block_on_provisional_failure(
                session=session,
                updater=updater,
                project_id=project_id,
                requested_chapters=len(pending_chapter_numbers),
                gate=failed_provisional,
            )

        workset = build_continue_generation_workset(
            session,
            project_id,
            max_chapters=max_chapters,
            source="orchestrator_continue",
        )
        chapter_numbers = list(workset.chapter_numbers)
        if not chapter_numbers:
            return RunResult(
                project_id=project_id,
                requested_chapters=0,
            )

        return self._run_project_chapters(
            session=session,
            repo=repo,
            updater=updater,
            checker=checker,
            project_id=project_id,
            chapter_numbers=chapter_numbers,
            requested_chapters=len(chapter_numbers),
        )
    finally:
        self._clear_governance_runtime()
        session.close()



__all__ = ['_bind_orchestrator_runtime_hooks', 'run', 'run_existing_project', '_emit_progress', '_bind_governance_runtime', '_clear_governance_runtime', '_start_governance_stage_span', '_finish_governance_stage_span', '_record_stage_transition', '_latest_provisional_gate_snapshot', '_new_failed_provisional_gate', '_block_on_scenario_rehearsal', '_block_on_provisional_failure', '_pending_chapter_numbers_for_active_arc', '_materialize_next_genesis_arc_if_needed', 'continue_project']
