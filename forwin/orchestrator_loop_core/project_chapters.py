from __future__ import annotations

import logging

from forwin.checker.hard_floor import run_hard_floor
from forwin.checker.pulp_policy import evaluate_pulp_beat_policy
from forwin.maintenance.deferred import DeferredMaintenanceRecord, record_deferred_maintenance
from forwin.orchestrator_loop_core.result import RunResult
from forwin.orchestrator_loop_core.common import *

logger = logging.getLogger(__name__)


_STRUCTURED_EXTRACTION_PARTS = (
    "state_event_extraction",
    "thread_time_extraction",
    "lore_timeline_notes_extraction",
)


def _coerce_canon_apply_outcome(value: object):
    from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome

    if isinstance(value, CanonApplyOutcome):
        return value
    if value:
        return CanonApplyOutcome(blocked_path=str(value), block_kind="legacy_block")
    return CanonApplyOutcome()


def _record_pulp_beat_evaluation(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    hard_floor,
) -> None:
    metadata = getattr(hard_floor, "metadata", {}) or {}
    pulp_beat = metadata.get("pulp_beat") if isinstance(metadata, dict) else None
    if not isinstance(pulp_beat, dict):
        return
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.PULP_BEAT_EVALUATED,
        scope="chapter",
        summary=f"第{chapter_number}章 pulp beat 已评估。",
        payload={
            "passed": bool(getattr(hard_floor, "passed", False)),
            "warning_reasons": list(getattr(hard_floor, "warning_reasons", []) or []),
            "pulp_beat": pulp_beat,
        },
    )


def _defer_structured_extraction_if_needed(
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    writer_output,
) -> None:
    meta = dict(getattr(writer_output, "generation_meta", {}) or {})
    status = str(meta.get("structured_extraction", "") or "").strip()
    degraded_parts = [
        part
        for part in _STRUCTURED_EXTRACTION_PARTS
        if str(meta.get(part, "") or "").strip() == "degraded"
    ]
    if status not in {"degraded", "partial_degraded"} and not degraded_parts:
        return
    record_deferred_maintenance(
        updater,
        DeferredMaintenanceRecord(
            project_id=project_id,
            chapter_number=chapter_number,
            task_type="structured_extraction",
            reason=status or "structured_extraction_degraded",
            payload={
                "structured_extraction": status,
                "degraded_parts": degraded_parts,
            },
        ),
    )


def _run_project_chapters(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_numbers: list[int],
    requested_chapters: int,
) -> RunResult:
    completed_chapters: list[int] = []
    failed_chapters: list[int] = []
    paused_chapters: list[int] = []
    frozen_artifacts: list[str] = []
    last_requested_chapter = max(chapter_numbers, default=0)
    project = repo.get_project(project_id)
    if project is None:
        raise ValueError(f"项目不存在: {project_id}")
    governance = self._project_governance(project)

    for chapter_num in chapter_numbers:
        if self._abort_requested():
            return self._cancelled_result(
                project_id,
                requested_chapters,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
                frozen_artifacts=frozen_artifacts,
                current_chapter=chapter_num,
            )
        if self._pause_requested():
            return self._paused_result(
                project_id,
                requested_chapters,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
                frozen_artifacts=frozen_artifacts,
                current_chapter=max(0, chapter_num - 1),
            )
        print(f"\n{'─'*60}")
        print(f"正在生成第 {chapter_num} 章...")
        print(f"{'─'*60}")

        chapter_plan = repo.get_chapter_plan(project_id, chapter_num)
        if chapter_plan is None:
            logger.error("Chapter plan %d not found, skipping.", chapter_num)
            failed_chapters.append(chapter_num)
            continue
        block_code, block_band_id, block_message = self._strict_progression_block(
            session=session,
            repo=repo,
            updater=updater,
            project=project,
            chapter_number=chapter_num,
        )
        if block_code:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                band_id=block_band_id,
                chapter_number=chapter_num,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.HARD_GATE_HIT,
                scope="chapter",
                summary=block_message,
                related_object_type="chapter_plan",
                related_object_id=chapter_plan.id,
                payload={"blocking_reason": block_code},
            )
            session.commit()
            paused_chapters.append(chapter_num)
            return self._paused_result(
                project_id,
                requested_chapters,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
                frozen_artifacts=frozen_artifacts,
                current_chapter=max(0, chapter_num - 1),
            )
        manual_start_checkpoint = self._manual_boundary_checkpoint(
            session,
            project_id=project_id,
            chapter_number=chapter_num,
            boundary_kind="chapter_start",
        )
        if manual_start_checkpoint is not None:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                band_id=manual_start_checkpoint.band_id,
                chapter_number=chapter_num,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                scope="chapter",
                summary="命中 chapter_start manual checkpoint，运行已暂停。",
                related_object_type="band_checkpoint",
                related_object_id=manual_start_checkpoint.id,
            )
            session.commit()
            paused_chapters.append(chapter_num)
            return self._paused_result(
                project_id,
                requested_chapters,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
                frozen_artifacts=frozen_artifacts,
                current_chapter=max(0, chapter_num - 1),
            )

        try:
            self._emit_progress(
                "stage_changed",
                stage="assembling_context",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            context = self.retrieval_broker.build_chapter_context(
                repo, project_id, chapter_plan
            )
            context = self._audit_current_plan_before_write(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_plan=chapter_plan,
                context=context,
                trigger_stage="pre_write",
            )
            context_summary = dict(
                getattr(self.retrieval_broker, "last_observability_summary", {}) or {}
            )
            if context_summary:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.CONTEXT_ASSEMBLED,
                    scope="chapter",
                    summary=f"第{chapter_num}章 context 已组装。",
                    payload=context_summary,
                )
                if any(
                    int(context_summary.get(key) or 0) > 0
                    for key in ("pruned_entities", "pruned_threads", "pruned_relations")
                ):
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.CONTEXT_PRUNED,
                        scope="chapter",
                        summary=f"第{chapter_num}章 context 已按 budget 裁剪。",
                        payload=context_summary,
                    )

            if self._abort_requested():
                return self._cancelled_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            if self._pause_requested():
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            self._emit_progress(
                "stage_changed",
                stage="writing_chapter",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            writer_output = self._write_chapter_with_attention_fallback(
                context=context,
                project_id=project_id,
                chapter_number=chapter_num,
                updater=updater,
                paused_chapters=paused_chapters,
                frozen_artifacts=frozen_artifacts,
            )
            if writer_output is None:
                if self._abort_requested():
                    return self._cancelled_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                session.commit()
                break
            if self._pause_requested():
                session.commit()
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            self._emit_progress(
                "stage_changed",
                stage="continuity_review",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            writer_output, verdict, force_accept_applied = self._review_and_maybe_rewrite(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_plan=chapter_plan,
                context=context,
                writer_output=writer_output,
            )
            repair_attempt_count = len(
                repo.list_chapter_rewrite_attempts(project_id, chapter_num)
            )
            residual_review_issues = self._review_issue_payloads(verdict)
            canon_risk_level = self._review_canon_risk(verdict)
            session.commit()
            if self._pause_requested():
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )

            if self.config.hard_floor_gate_enabled:
                hard_floor = run_hard_floor(
                    writer_output=writer_output,
                    context_pack=context,
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    config=self.config,
                )
                _record_pulp_beat_evaluation(
                    self,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    hard_floor=hard_floor,
                )
                pulp_policy = evaluate_pulp_beat_policy(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    hard_floor_result=hard_floor,
                    config=self.config,
                )
                if pulp_policy.fatal:
                    fail_reasons = [*hard_floor.fail_reasons, pulp_policy.reason]
                    hard_floor = hard_floor.model_copy(
                        update={
                            "passed": False,
                            "fail_reasons": fail_reasons,
                            "checks": {
                                **hard_floor.checks,
                                pulp_policy.reason: False,
                            },
                            "metadata": {
                                **hard_floor.metadata,
                                "pulp_beat_policy": pulp_policy.model_dump(mode="json"),
                            },
                        }
                    )
                if not hard_floor.passed:
                    hard_floor_issues = [
                        {
                            "reviewer": "hard_floor",
                            "rule_name": reason,
                            "severity": "error",
                            "message": f"hard floor failed: {reason}",
                        }
                        for reason in hard_floor.fail_reasons
                    ]
                    hard_floor_reason = "; ".join(hard_floor.fail_reasons)
                    summary = f"第{chapter_num}章 hard floor failed"
                    if hard_floor_reason:
                        summary = f"{summary}: {hard_floor_reason}"
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "failed",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=[
                            *residual_review_issues,
                            *hard_floor_issues,
                        ],
                        canon_risk_level="high",
                    )
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.HARD_GATE_HIT,
                        scope="chapter",
                        summary=summary,
                        reason=hard_floor_reason,
                        payload=hard_floor.model_dump(mode="json"),
                    )
                    session.commit()
                    failed_chapters.append(chapter_num)
                    break

            if self.config.operation_mode == "checkpoint":
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                break
            if self.config.operation_mode == "copilot" and verdict.verdict != "pass":
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                break
            if (
                self.config.operation_mode == "blackbox"
                and verdict.verdict == "fail"
                and not force_accept_applied
            ):
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                break

            should_apply_canon = (
                verdict.verdict == "pass"
                or (self.config.operation_mode == "blackbox" and verdict.verdict == "warn")
                or force_accept_applied
            )
            if not should_apply_canon:
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                break

            review_interval = max(0, int(self.config.review_interval_chapters or 0))
            if review_interval and chapter_num % review_interval == 0 and chapter_num != last_requested_chapter:
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level=canon_risk_level,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                break

            self._emit_progress(
                "stage_changed",
                stage="applying_canon",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            canon_outcome = _coerce_canon_apply_outcome(
                self._apply_canon_candidate(
                    session=session,
                    repo=repo,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    writer_output=writer_output,
                    verdict=verdict,
                )
            )
            if canon_outcome.blocked:
                frozen_path = canon_outcome.blocked_path
                if frozen_path:
                    frozen_artifacts.append(frozen_path)
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=residual_review_issues,
                    canon_risk_level="high",
                )
                session.commit()
                paused_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="paused_for_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                )
                repo, updater, checker = self._make_state_helpers(session)
                break
            if self._pause_requested():
                session.commit()
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )

            status = "accepted"
            updater.mark_chapter_status(
                project_id,
                chapter_num,
                status,
                acceptance_mode=(
                    "force_accept_after_repair" if force_accept_applied else "normal"
                ),
                repair_attempt_count=repair_attempt_count,
                residual_review_issues=(
                    residual_review_issues if force_accept_applied else []
                ),
                    canon_risk_level=canon_risk_level,
            )
            _defer_structured_extraction_if_needed(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
                writer_output=writer_output,
            )
            self._emit_progress(
                "stage_changed",
                stage="running_post_acceptance",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
                event_family="runtime_observation",
                event_type=DecisionEventType.MEMORY_INDEX_UPSERT_STARTED,
                scope="chapter",
                summary=f"第{chapter_num}章 memory index upsert 开始。",
            )
            try:
                self.retrieval_broker.memory_index.upsert_chapter(
                    project_id=project_id,
                    chapter_number=chapter_num,
                    title=writer_output.title,
                    summary=writer_output.end_of_chapter_summary,
                    body=writer_output.body,
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.MEMORY_INDEX_UPSERT_SUCCEEDED,
                    scope="chapter",
                    summary=f"第{chapter_num}章 memory index upsert 完成。",
                )
            except Exception as exc:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.MEMORY_INDEX_UPSERT_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_num}章 memory index upsert 失败。",
                    reason=str(exc),
                    payload={"error_class": exc.__class__.__name__, "error_summary": str(exc)},
                )
                long_run_policy = getattr(self.config, "long_run_policy", None)
                should_defer_observation_failure = bool(
                    getattr(long_run_policy, "defer_observation_failures", False)
                    or str(getattr(self.config, "quality_profile", "") or "") == "pulp"
                )
                if not should_defer_observation_failure:
                    raise
                record_deferred_maintenance(
                    updater,
                    DeferredMaintenanceRecord(
                        project_id=project_id,
                        chapter_number=chapter_num,
                        task_type="memory_index_upsert",
                        reason=str(exc),
                        payload={
                            "error_class": exc.__class__.__name__,
                            "error_summary": str(exc),
                        },
                    ),
                )
            self._run_phase3_pass(
                session=session,
                project_id=project_id,
                chapter_number=chapter_num,
            )
            future_plan_audit_result = self._audit_future_plans_after_acceptance(
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
                trigger_stage="post_acceptance",
            )
            future_plan_audit_blocked = bool(
                future_plan_audit_result is not None
                and future_plan_audit_result.blocking_reasons
            )
            world_model_ok = self._compile_world_model_after_acceptance(
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
            )
            if not world_model_ok:
                session.commit()
                completed_with_current = [*completed_chapters, chapter_num]
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_with_current,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            generation_audit_pause = self._record_generation_audit_checkpoint_if_due(
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
                requested_chapters=requested_chapters,
                last_requested_chapter=last_requested_chapter,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
                future_plan_audit_result=future_plan_audit_result,
                governance=governance,
            )
            checkpoint_row = None
            checkpoint_pause = False
            checkpoint_warn_pause = False
            if bool(governance.auto_band_checkpoint):
                try:
                    checkpoint_row = self._create_auto_band_checkpoint(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                    )
                except Exception as exc:
                    band_row = repo.get_band_row_for_chapter(project_id, chapter_num)
                    if band_row is None:
                        raise
                    checkpoint_row = updater.save_band_checkpoint(
                        BandCheckpointDetail(
                            project_id=project_id,
                            arc_id=band_row.arc_id,
                            band_id=band_row.band_id,
                            chapter_start=int(band_row.chapter_start or 0),
                            chapter_end=int(band_row.chapter_end or 0),
                            trigger_source="auto_band_end",
                            boundary_kind="band_end",
                            boundary_chapter=chapter_num,
                            status="error",
                            summary="band checkpoint evaluator 异常，运行已暂停。",
                            issues=[
                                BandCheckpointIssueInfo(
                                    code="checkpoint_evaluator_error",
                                    severity="error",
                                    issue_group=issue_group_for_issue(code="runtime"),
                                    description="checkpoint evaluator 执行失败。",
                                    detail=f"{exc.__class__.__name__}: {exc}",
                                )
                            ],
                        )
                    )
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        band_id=band_row.band_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                        scope="band",
                        summary="band checkpoint evaluator 异常。",
                        reason=str(exc),
                        related_object_type="band_checkpoint",
                        related_object_id=checkpoint_row.id,
                        payload={
                            "status": "error",
                            "error_class": exc.__class__.__name__,
                            "error_summary": str(exc),
                        },
                    )
                if checkpoint_row is not None and checkpoint_row.status in {"fail", "error"}:
                    checkpoint_pause = True
                if (
                    checkpoint_row is not None
                    and checkpoint_row.status == "warn"
                    and str(governance.band_warn_action or "") == "pause"
                ):
                    checkpoint_warn_pause = True
            manual_after_accept = self._manual_boundary_checkpoint(
                session,
                project_id=project_id,
                chapter_number=chapter_num,
                boundary_kind="chapter_accepted",
            )
            manual_band_end = self._manual_boundary_checkpoint(
                session,
                project_id=project_id,
                chapter_number=chapter_num,
                boundary_kind="band_end",
            )
            session.commit()
            should_pause_for_checkpoint = checkpoint_pause or (
                checkpoint_warn_pause and chapter_num != last_requested_chapter
            )
            if (
                should_pause_for_checkpoint
                or manual_after_accept is not None
                or manual_band_end is not None
                or future_plan_audit_blocked
                or generation_audit_pause
            ):
                if should_pause_for_checkpoint and checkpoint_row is not None:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        band_id=checkpoint_row.band_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.BAND_CHECKPOINT_HIT,
                        scope="band",
                        summary="band checkpoint 命中阻断，运行已暂停。",
                        related_object_type="band_checkpoint",
                        related_object_id=checkpoint_row.id,
                        payload={"status": checkpoint_row.status},
                    )
                if manual_after_accept is not None:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        band_id=manual_after_accept.band_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                        scope="chapter",
                        summary="命中 chapter_accepted manual checkpoint，运行已暂停。",
                        related_object_type="band_checkpoint",
                        related_object_id=manual_after_accept.id,
                    )
                if manual_band_end is not None:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        band_id=manual_band_end.band_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                        scope="band",
                        summary="命中 band_end manual checkpoint，运行已暂停。",
                        related_object_type="band_checkpoint",
                        related_object_id=manual_band_end.id,
                    )
                if future_plan_audit_blocked and future_plan_audit_result is not None:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
                        scope="project",
                        summary="future plan audit 存在未修复阻断，运行已暂停。",
                        related_object_type="future_plan_audit_run",
                        related_object_id=future_plan_audit_result.id,
                        payload={
                            "blocking_reasons": list(future_plan_audit_result.blocking_reasons),
                            "inspected_chapters": list(future_plan_audit_result.inspected_chapters),
                        },
                    )
                session.commit()
                completed_with_current = [*completed_chapters, chapter_num]
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_with_current,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            if self._pause_requested():
                completed_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )

        except Exception as exc:
            logger.exception("Chapter %d failed.", chapter_num)
            session.rollback()
            repo, updater, checker = self._make_state_helpers(session)
            updater.mark_chapter_status(project_id, chapter_num, "failed")
            session.commit()
            failed_chapters.append(chapter_num)
            self._emit_progress(
                "stage_changed",
                stage="chapter_failed",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )
            print(f"  ✗ 第{chapter_num}章失败: {exc}")
            if isinstance(exc, TransientLLMChapterFailure) or self._is_transient_llm_like(exc):
                logger.warning(
                    "Stopping run after transient LLM failure on chapter %d to avoid cascading failures.",
                    chapter_num,
                )
                break
            continue

        completed_chapters.append(chapter_num)
        self._emit_progress(
            "chapter_completed",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=chapter_num,
            completed_chapters=completed_chapters,
            failed_chapters=failed_chapters,
            paused_chapters=paused_chapters,
        )

        issue_summary = ""
        if verdict.issues:
            issue_summary = " | 问题: " + "; ".join(
                i.description for i in verdict.issues[:3]
            )
        print(
            f"  ✓ 第{chapter_num}章 《{writer_output.title}》 "
            f"({writer_output.char_count}字) "
            f"审查: {verdict.verdict}{issue_summary}"
        )

    return RunResult(
        project_id=project_id,
        requested_chapters=requested_chapters,
        completed_chapters=completed_chapters,
        failed_chapters=failed_chapters,
        paused_chapters=paused_chapters,
        frozen_artifacts=frozen_artifacts,
    )



__all__ = ['_run_project_chapters']
