from __future__ import annotations

from forwin.orchestrator_loop_core.common import *
from forwin.review_engine.audit import (
    build_decision_event_payload,
    build_legacy_compatibility_payload,
    digest_decision_input,
)
from forwin.review_engine.types import Decision, DecisionInput

def _project_governance(self, project: Project):
    governance = normalize_project_governance(
        getattr(project, "governance_json", "{}"),
        fallback_operation_mode=self.config.operation_mode,
        fallback_review_interval=self.config.review_interval_chapters,
    )
    return governance

def _record_decision_event(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    event_family: str,
    event_type: str,
    summary: str,
    reason: str = "",
    scope: str = "project",
    actor_type: str = "system",
    band_id: str = "",
    chapter_number: int = 0,
    task_id: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    payload: dict[str, Any] | None = None,
    parent_event_id: str = "",
    causal_root_id: str = "",
):
    row = updater.save_decision_event(
        DecisionEventInfo(
            project_id=project_id,
            task_id=task_id or self._governance_task_id,
            band_id=band_id,
            chapter_number=chapter_number,
            scope=scope,
            event_family=event_family,
            event_type=ensure_decision_event_type(event_type),
            actor_type=actor_type,
            summary=summary,
            reason=reason,
            payload=payload or {},
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            parent_event_id=parent_event_id,
            causal_root_id=causal_root_id or self._governance_root_event_id,
        )
    )
    if not self._governance_root_event_id:
        self._governance_root_event_id = str(row.causal_root_id or row.id or "")
    return row

def _record_engine_decision_event(
    self,
    *,
    updater: StateUpdater,
    decision: Decision,
    decision_input: DecisionInput,
    shadow_mismatch: bool = False,
    live_or_shadow: str = "shadow",
    baseline_outcome: str = "",
    engine_outcome: str = "",
    live_source: str = "",
    shadow_source: str = "",
    engine_live: bool = False,
    baseline_shadow_evaluated: bool = False,
    baseline_safety_net_used: bool = False,
    severe_mismatch: bool = False,
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
) -> None:
    try:
        payload = build_decision_event_payload(
            decision=decision,
            input_digest=digest_decision_input(decision_input),
            shadow_mismatch=shadow_mismatch,
            live_or_shadow=live_or_shadow,
            baseline_outcome=baseline_outcome,
            engine_outcome=engine_outcome,
            live_source=live_source,
            shadow_source=shadow_source,
            engine_live=engine_live,
            baseline_shadow_evaluated=baseline_shadow_evaluated,
            baseline_safety_net_used=baseline_safety_net_used,
            severe_shadow_mismatch=severe_mismatch,
        )
        self._record_decision_event(
            updater=updater,
            project_id=decision_input.project_id,
            chapter_number=decision_input.chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_ENGINE_DECISION,
            scope="chapter",
            summary=f"engine decided {decision.outcome} via {decision.rule_id}",
            reason=decision.reason,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            payload=payload,
            parent_event_id=parent_event_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to record review engine decision event project=%s chapter=%s rule=%s: %s",
            decision_input.project_id,
            decision_input.chapter_number,
            decision.rule_id,
            exc,
        )

def _record_legacy_compatibility_event(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    compat_layer: str,
    compat_feature: str,
    usage_kind: str,
    source_module: str,
    usage_reason: str,
    chapter_number: int = 0,
    related_object_type: str = "",
    related_object_id: str = "",
    compat_key: str = "",
    legacy_identifier: str = "",
    canonical_identifier: str = "",
    related_stage: str = "",
    metadata: dict[str, Any] | None = None,
    parent_event_id: str = "",
) -> None:
    try:
        payload = build_legacy_compatibility_payload(
            compat_layer=compat_layer,
            compat_feature=compat_feature,
            usage_kind=usage_kind,
            source_module=source_module,
            usage_reason=usage_reason,
            compat_key=compat_key,
            legacy_identifier=legacy_identifier,
            canonical_identifier=canonical_identifier,
            related_stage=related_stage,
            metadata=metadata,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.LEGACY_COMPATIBILITY_USED,
            scope="chapter" if int(chapter_number or 0) else "project",
            summary=f"legacy compatibility used: {compat_feature}",
            reason=usage_reason,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            payload=payload,
            parent_event_id=parent_event_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to record legacy compatibility usage project=%s chapter=%s feature=%s: %s",
            project_id,
            chapter_number,
            compat_feature,
            exc,
        )

def _audit_current_plan_before_write(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    trigger_stage: str,
):
    project = session.get(Project, project_id)
    target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
    chapter_number = int(chapter_plan.chapter_number or 0)
    canon_quality_context = dict(getattr(context, "canon_quality_context", {}) or {})
    obligation_repo = NarrativeObligationRepository(session)
    result = FuturePlanAuditor(
        mode="chapter_review_form",
        plan_patch_validation_mode="chapter_review_form",
        llm_client=self.llm_client,
        min_blocking_confidence=float(getattr(self.config, "chapter_review_form_min_blocking_confidence", 0.8) or 0.8),
    ).audit_and_apply(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
        trigger_stage=trigger_stage,
        plans=[chapter_plan],
        canon_quality_context=canon_quality_context,
        obligations=obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number),
        target_total_chapters=target_total_chapters,
        include_current=True,
    )
    self._record_future_plan_audit_events(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        result=result,
    )
    if result.blocking_reasons:
        raise RuntimeError(
            "future_plan_audit_blocked:"
            + ";".join(str(reason) for reason in result.blocking_reasons)
        )
    if result.applied_plan_patch_ids:
        session.flush()
        return self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan)
    return context

def _audit_future_plans_after_acceptance(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    trigger_stage: str = "post_acceptance",
) -> FuturePlanAuditRun | None:
    project = session.get(Project, project_id)
    target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
    plans = self._future_plan_audit_plans(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
        include_current=False,
    )
    band_rows = self._future_plan_audit_band_rows(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
    )
    if not plans and not band_rows:
        return None
    obligation_repo = NarrativeObligationRepository(session)
    obligations = [
        *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number + 1),
        *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
    ]
    canon_quality_context = _build_canon_quality_context(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number + 1,
        target_total_chapters=target_total_chapters,
    )
    result = FuturePlanAuditor(
        mode="chapter_review_form",
        plan_patch_validation_mode="chapter_review_form",
        llm_client=self.llm_client,
        min_blocking_confidence=float(getattr(self.config, "chapter_review_form_min_blocking_confidence", 0.8) or 0.8),
    ).audit_and_apply(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
        trigger_stage=trigger_stage,
        plans=plans,
        canon_quality_context=canon_quality_context,
        obligations=obligations,
        target_total_chapters=target_total_chapters,
        include_current=False,
        band_rows=band_rows,
    )
    self._record_future_plan_audit_events(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        result=result,
    )
    return result

@staticmethod
def _future_plan_audit_plans(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
    include_current: bool,
) -> list[ChapterPlan]:
    lower_bound = int(current_chapter or 0)
    predicate = ChapterPlan.chapter_number >= lower_bound if include_current else ChapterPlan.chapter_number > lower_bound
    return list(
        session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                predicate,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
    )

@staticmethod
def _future_plan_audit_band_rows(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
) -> list[BandExperiencePlan]:
    return list(
        session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.chapter_end > int(current_chapter or 0),
            )
            .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.chapter_end.asc())
        ).scalars().all()
    )

def _record_future_plan_audit_events(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    result: FuturePlanAuditRun,
) -> None:
    if not result.inspected_chapters:
        return
    event = self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
        scope="project",
        summary=f"future plan audit: {result.status}",
        related_object_type="future_plan_audit_run",
        related_object_id=result.id,
        payload=result.model_dump(mode="json", exclude={"plan_patches"}),
    )
    if result.applied_plan_patch_ids:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.FUTURE_PLAN_PATCH_APPLIED,
            scope="project",
            summary=f"已应用 {len(result.applied_plan_patch_ids)} 个 future plan patch。",
            related_object_type="future_plan_audit_run",
            related_object_id=result.id,
            parent_event_id=str(event.id or ""),
            payload={
                "applied_plan_patch_ids": list(result.applied_plan_patch_ids),
                "issue_types": [issue.issue_type for issue in result.issues],
            },
        )

def _record_generation_audit_checkpoint_if_due(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    requested_chapters: int,
    last_requested_chapter: int,
    completed_chapters: list[int],
    failed_chapters: list[int],
    paused_chapters: list[int],
    future_plan_audit_result: FuturePlanAuditRun | None,
    governance,
) -> bool:
    interval = _positive_int(
        getattr(governance, "generation_audit_interval_chapters", 0)
    )
    if interval <= 0:
        return False
    chapter_number = int(chapter_number or 0)
    if chapter_number <= 0 or chapter_number % interval != 0:
        return False
    project_pause_enabled = bool(getattr(governance, "generation_audit_pause_enabled", False))
    runtime_pause_enabled = bool(getattr(self.config, "generation_audit_pause_enabled", False))
    pause_enabled = bool(project_pause_enabled and runtime_pause_enabled)
    has_next_requested = chapter_number != int(last_requested_chapter or 0)
    will_pause = bool(pause_enabled and has_next_requested)
    payload = self._generation_audit_checkpoint_payload(
        session=session,
        project_id=project_id,
        checkpoint_chapter=chapter_number,
        interval=interval,
        requested_chapters=requested_chapters,
        completed_chapters=[*completed_chapters, chapter_number],
        failed_chapters=failed_chapters,
        paused_chapters=paused_chapters,
        future_plan_audit_result=future_plan_audit_result,
        will_pause=will_pause,
        pause_enabled=pause_enabled,
        project_pause_enabled=project_pause_enabled,
        runtime_pause_enabled=runtime_pause_enabled,
        next_chapter=(chapter_number + 1 if has_next_requested else 0),
    )
    summary = (
        f"第{chapter_number}章命中 {interval} 章生成审计检查点，运行将暂停。"
        if will_pause
        else f"第{chapter_number}章命中 {interval} 章生成审计检查点，已记录摘要。"
    )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
        scope="project",
        summary=summary,
        related_object_type="generation_audit_checkpoint",
        related_object_id=f"{project_id}:{chapter_number}",
        payload=payload,
    )
    return will_pause

def _generation_audit_checkpoint_payload(
    self,
    *,
    session: Session,
    project_id: str,
    checkpoint_chapter: int,
    interval: int,
    requested_chapters: int,
    completed_chapters: list[int],
    failed_chapters: list[int],
    paused_chapters: list[int],
    future_plan_audit_result: FuturePlanAuditRun | None,
    will_pause: bool,
    pause_enabled: bool,
    project_pause_enabled: bool,
    runtime_pause_enabled: bool,
    next_chapter: int,
) -> dict[str, Any]:
    window_start = max(1, int(checkpoint_chapter or 0) - max(1, int(interval or 1)) + 1)
    window_end = int(checkpoint_chapter or 0)
    plans = (
        session.query(ChapterPlan)
        .filter(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number >= window_start,
            ChapterPlan.chapter_number <= window_end,
        )
        .order_by(ChapterPlan.chapter_number.asc())
        .all()
    )
    status_by_chapter = {
        str(int(plan.chapter_number or 0)): str(plan.status or "")
        for plan in plans
    }
    accepted_chapters = [
        int(plan.chapter_number or 0)
        for plan in plans
        if str(plan.status or "") == "accepted"
    ]
    needs_review_chapters = [
        int(plan.chapter_number or 0)
        for plan in plans
        if str(plan.status or "") == "needs_review"
    ]
    failed_window_chapters = [
        int(plan.chapter_number or 0)
        for plan in plans
        if str(plan.status or "") == "failed"
    ]
    high_risk_chapters = [
        int(plan.chapter_number or 0)
        for plan in plans
        if str(plan.canon_risk_level or "") == "high"
    ]
    repair_attempts_by_chapter = {
        str(int(plan.chapter_number or 0)): int(plan.repair_attempt_count or 0)
        for plan in plans
        if int(plan.repair_attempt_count or 0) > 0
    }
    residual_issue_count_by_chapter: dict[str, int] = {}
    for plan in plans:
        raw_issues = str(plan.residual_review_issues_json or "[]")
        try:
            parsed_issues = json.loads(raw_issues)
        except (json.JSONDecodeError, TypeError):
            parsed_issues = []
        if isinstance(parsed_issues, list) and parsed_issues:
            residual_issue_count_by_chapter[str(int(plan.chapter_number or 0))] = len(parsed_issues)
    obligation_repo = NarrativeObligationRepository(session)
    active_obligations = obligation_repo.list_active_for_context(
        project_id,
        chapter_number=window_end + 1,
    )
    return {
        "checkpoint_chapter": window_end,
        "checkpoint_interval": int(interval or 0),
        "window_start": window_start,
        "window_end": window_end,
        "requested_chapters": int(requested_chapters or 0),
        "completed_chapters": sorted({int(item) for item in completed_chapters}),
        "failed_chapters": sorted({int(item) for item in [*failed_chapters, *failed_window_chapters]}),
        "paused_chapters": sorted({int(item) for item in paused_chapters}),
        "accepted_chapters": accepted_chapters,
        "needs_review_chapters": needs_review_chapters,
        "status_by_chapter": status_by_chapter,
        "high_risk_chapters": high_risk_chapters,
        "repair_attempts_by_chapter": repair_attempts_by_chapter,
        "residual_issue_count_by_chapter": residual_issue_count_by_chapter,
        "open_obligation_ids": [item.id for item in active_obligations],
        "p0_p1_open_obligation_ids": [
            item.id for item in active_obligations if item.priority in {"P0", "P1"}
        ],
        "future_plan_audit": _future_plan_audit_checkpoint_payload(
            future_plan_audit_result
        ),
        "pause": {
            "enabled": pause_enabled,
            "project_enabled": project_pause_enabled,
            "runtime_enabled": runtime_pause_enabled,
            "will_pause": will_pause,
            "reason": "generation_audit_checkpoint" if will_pause else "",
        },
        "next_chapter": int(next_chapter or 0),
    }

def _previous_band_row(
    self,
    session: Session,
    *,
    project_id: str,
    current_start: int,
) -> BandExperiencePlan | None:
    active_arc = StateRepository(session).get_active_arc_plan(project_id)
    if active_arc is None:
        return None
    return (
        session.query(BandExperiencePlan)
        .filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == active_arc.id,
            BandExperiencePlan.chapter_end < current_start,
        )
        .order_by(BandExperiencePlan.chapter_end.desc(), BandExperiencePlan.created_at.desc())
        .first()
    )

def _manual_boundary_checkpoint(
    self,
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
    boundary_kind: str,
) -> BandCheckpoint | None:
    return (
        session.query(BandCheckpoint)
        .filter(
            BandCheckpoint.project_id == project_id,
            BandCheckpoint.trigger_source == "manual_boundary",
            BandCheckpoint.status == "pending",
            BandCheckpoint.boundary_kind == boundary_kind,
            BandCheckpoint.boundary_chapter == chapter_number,
        )
        .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
        .first()
    )

def _strict_progression_block(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater | None = None,
    project: Project,
    chapter_number: int,
) -> tuple[str, str, str]:
    governance = self._project_governance(project)
    mode = str(governance.progression_mode or "serial_canon_band_guard")
    if chapter_number > 1:
        previous_plan = repo.get_chapter_plan(project.id, chapter_number - 1)
        if previous_plan is not None and previous_plan.status != "accepted":
            return (
                "chapter_not_canon",
                "",
                chapter_blocking_message("chapter_not_canon", chapter_number=chapter_number - 1),
            )
    if mode != "serial_canon_band_guard":
        return "", "", ""
    band_row = repo.get_band_row_for_chapter(project.id, chapter_number)
    if band_row is None or not band_is_first_chapter(band_row.chapter_start, chapter_number):
        return "", "", ""
    previous_band = self._previous_band_row(
        session,
            project_id=project.id,
            current_start=int(band_row.chapter_start or 0),
        )
    if previous_band is None:
        return "", "", ""
    latest_checkpoint = repo.get_latest_band_checkpoint(project.id, band_id=previous_band.band_id)
    if latest_checkpoint is None and bool(governance.auto_band_checkpoint) and updater is not None:
        latest_checkpoint = self._create_auto_band_checkpoint(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project.id,
            chapter_number=int(previous_band.chapter_end or 0),
        )
    if latest_checkpoint is None:
        return (
            "band_checkpoint_pending",
            previous_band.band_id,
            chapter_blocking_message("band_checkpoint_pending", band_id=previous_band.band_id),
        )
    checkpoint_status = str(latest_checkpoint.status or "")
    if checkpoint_status in {"pass", "overridden"}:
        return "", "", ""
    if checkpoint_status == "warn" and str(governance.band_warn_action or "") == "continue":
        return "", "", ""
    code = {
        "pending": "band_checkpoint_pending",
        "warn": "band_checkpoint_warn",
        "fail": "band_checkpoint_fail",
        "error": "band_checkpoint_fail",
    }.get(checkpoint_status, "band_checkpoint_pending")
    return (
        code,
        previous_band.band_id,
        chapter_blocking_message(code, band_id=previous_band.band_id),
    )

def _create_auto_band_checkpoint(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
) -> BandCheckpoint | None:
    active_arc = repo.get_active_arc_plan(project_id)
    band_row = None
    if active_arc is not None:
        band_row = (
            session.query(BandExperiencePlan)
            .filter(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.chapter_start <= chapter_number,
                BandExperiencePlan.chapter_end == chapter_number,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .first()
        )
    if band_row is None:
        band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
    if band_row is None or int(band_row.chapter_end or 0) != chapter_number:
        return None
    existing_boundary_checkpoint = (
        session.query(BandCheckpoint)
        .filter(
            BandCheckpoint.project_id == project_id,
            BandCheckpoint.band_id == band_row.band_id,
            BandCheckpoint.trigger_source == "auto_band_end",
            BandCheckpoint.boundary_kind == "band_end",
            BandCheckpoint.boundary_chapter == chapter_number,
        )
        .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
        .first()
    )
    if (
        existing_boundary_checkpoint is not None
        and str(existing_boundary_checkpoint.status or "") in {"pending", "warn", "fail", "error"}
    ):
        return existing_boundary_checkpoint
    band_plans = (
        session.query(ChapterPlan)
        .filter(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number >= int(band_row.chapter_start or 0),
            ChapterPlan.chapter_number <= int(band_row.chapter_end or 0),
        )
        .order_by(ChapterPlan.chapter_number.asc())
        .all()
    )
    unresolved = [
        row for row in repo.list_band_checkpoints(project_id, band_id=band_row.band_id)
        if row.status == "pending"
    ]
    constraints_enabled = (
        bool(repo.future_constraints_enabled(project_id))
        if hasattr(repo, "future_constraints_enabled")
        else True
    )
    issues: list[BandCheckpointIssueInfo] = []
    status = "pass"
    chapter_bodies: list[str] = []
    chapter_summaries: list[str] = []
    unresolved_review_chapters: list[int] = []
    review_fail_chapters: list[int] = []
    review_metas: list[dict[str, Any]] = []
    for plan in band_plans:
        if str(plan.status or "") == "needs_review":
            unresolved_review_chapters.append(int(plan.chapter_number or 0))
        latest_draft = (
            session.query(ChapterDraft)
            .filter(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .first()
        )
        if latest_draft is None:
            continue
        chapter_bodies.append(str(latest_draft.body_text or ""))
        chapter_summaries.append(str(latest_draft.summary or ""))
        latest_review = (
            session.query(ChapterReview)
            .filter(ChapterReview.draft_id == latest_draft.id)
            .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
            .first()
        )
        if latest_review is not None and str(latest_review.verdict or "") == "fail":
            review_fail_chapters.append(int(plan.chapter_number or 0))
        if latest_review is not None:
            try:
                review_meta = json.loads(latest_review.review_meta_json or "{}") or {}
            except (json.JSONDecodeError, TypeError):
                review_meta = {}
            try:
                review_issues = json.loads(latest_review.issues_json or "[]") or []
            except (json.JSONDecodeError, TypeError):
                review_issues = []
            if isinstance(review_meta, dict):
                review_meta["chapter_number"] = int(plan.chapter_number or 0)
                review_meta["issue_types"] = [
                    str(item.get("issue_type") or item.get("rule_name") or "")
                    for item in review_issues
                    if isinstance(item, dict)
                ]
                review_metas.append(review_meta)
    latest_provisional = (
        session.query(ProvisionalBandExecution)
        .filter(
            ProvisionalBandExecution.project_id == project_id,
            ProvisionalBandExecution.arc_id == band_row.arc_id,
            ProvisionalBandExecution.band_id == band_row.band_id,
        )
        .order_by(ProvisionalBandExecution.created_at.desc(), ProvisionalBandExecution.id.desc())
        .first()
    )
    provisional_failed = bool(
        latest_provisional is not None
        and (
            str(latest_provisional.aggregate_verdict or "") == "fail"
            or int(latest_provisional.failure_count or 0) > 0
        )
    )
    if any(plan.status != "accepted" for plan in band_plans):
        status = "fail"
        issues.append(
            BandCheckpointIssueInfo(
                code="band_not_fully_accepted",
                severity="error",
                issue_group=issue_group_for_issue(code="intra_band_consistency"),
                description="band 内仍有章节未 accepted。",
            )
        )
    if unresolved:
        status = "warn" if status == "pass" else status
        issues.append(
            BandCheckpointIssueInfo(
                code="pending_checkpoint_exists",
                severity="warning",
                issue_group=issue_group_for_issue(code="intra_band_consistency"),
                description="同 band 仍存在未处理 checkpoint。",
            )
        )
    intra_band_issues = evaluate_intra_band_consistency(
        unresolved_review_chapters=unresolved_review_chapters,
        review_fail_chapters=review_fail_chapters,
        provisional_failed=provisional_failed,
        pending_checkpoint_count=len(unresolved),
        reviewer="governance",
        target_scope="band",
    )
    for issue in intra_band_issues:
        issues.append(
            BandCheckpointIssueInfo(
                code="intra_band_consistency",
                severity=issue.severity,
                issue_group=issue.issue_group,
                description=issue.description,
                detail="; ".join(issue.evidence_refs),
            )
        )
    combined_text = band_combined_text(
        chapter_bodies=chapter_bodies,
        chapter_summaries=chapter_summaries,
    )
    band_task_issues = evaluate_task_contract(
        repo.get_band_task_contract_for_chapter(project_id, chapter_number),
        combined_text=combined_text,
        reviewer="governance",
        issue_type="band_task_completion",
        target_scope="band",
    )
    for issue in band_task_issues:
        issues.append(
            BandCheckpointIssueInfo(
                code="band_task_completion",
                severity=issue.severity,
                issue_group=issue.issue_group,
                description=issue.description,
                detail="; ".join(issue.evidence_refs),
            )
        )
    try:
        schedule_payload = json.loads(band_row.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        schedule_payload = {}
    band_schedule = (
        BandDelightSchedule.model_validate(schedule_payload)
        if isinstance(schedule_payload, dict)
        else None
    )
    obligation_repo = NarrativeObligationRepository(session)
    band_obligations = [
        *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number + 1),
        *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
    ]
    band_obligation_issues = evaluate_band_obligation_contract(
        band_schedule,
        obligations=band_obligations,
        band_end_chapter=chapter_number,
        reviewer="governance",
        target_scope="band",
    )
    for issue in band_obligation_issues:
        issues.append(
            BandCheckpointIssueInfo(
                code="band_obligation_completion",
                severity=issue.severity,
                issue_group=issue.issue_group,
                description=issue.description,
                detail="; ".join(issue.evidence_refs),
            )
        )
    director_issues = evaluate_director_imbalance(
        review_metas=review_metas,
        band_stall_guard=int(getattr(band_row, "stall_guard_max_gap", 0) or 0),
        reviewer="governance",
        target_scope="band",
    )
    for issue in director_issues:
        issues.append(
            BandCheckpointIssueInfo(
                code="director_imbalance",
                severity=issue.severity,
                issue_group=issue.issue_group,
                description=issue.description,
                detail="; ".join(issue.evidence_refs),
            )
        )
    next_band_summary = repo.get_next_band_summary(project_id, chapter_number)
    constraint_chapter = (
        int(next_band_summary.chapter_start or 0)
        if next_band_summary is not None
        else chapter_number + 1
    )
    future_constraints = (
        repo.list_active_narrative_constraints(
            project_id,
            chapter_number=max(chapter_number, constraint_chapter),
        )
        if constraints_enabled
        else []
    )
    if constraints_enabled:
        compatibility_issues = evaluate_constraint_issues(
            future_constraints,
            combined_text=combined_text,
            state_changes=[],
            events=[],
            thread_beats=[],
            reviewer="governance",
            issue_type="next_band_compatibility",
            target_scope="band",
        )
        compatibility_issues.extend(
            evaluate_next_band_task_compatibility(
                next_band_summary=next_band_summary,
                combined_text=combined_text,
                reviewer="governance",
                target_scope="band",
            )
        )
        for issue in compatibility_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="next_band_compatibility" if issue.severity == "error" else "future_constraint",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        next_band_targets = [
            *[
                task.target_name
                for task in (next_band_summary.band_task_contract if next_band_summary is not None else [])
                if str(task.target_name or "").strip()
            ],
            *[
                constraint.subject_name
                for constraint in future_constraints
                if str(constraint.subject_name or "").strip()
            ],
        ]
        future_risk_issues = evaluate_resource_closure_risk(
            combined_text=combined_text,
            next_band_targets=list(dict.fromkeys(next_band_targets)),
            reviewer="governance",
            target_scope="band",
        )
        for issue in future_risk_issues:
            category = ""
            for ref in issue.evidence_refs:
                text = str(ref or "")
                if text.startswith("category="):
                    category = text.split("=", 1)[1].strip()
                    break
            issues.append(
                BandCheckpointIssueInfo(
                    code="future_resource_preservation",
                    severity="warning",
                    category=category,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
    if status != "fail" and any(issue.severity == "error" for issue in issues):
        status = "fail"
    elif status == "pass" and any(issue.severity == "warning" for issue in issues):
        status = "warn"
    summary = "band checkpoint 通过。" if status == "pass" else "band checkpoint 需要人工处理。"
    row = updater.save_band_checkpoint(
        BandCheckpointDetail(
            project_id=project_id,
            arc_id=band_row.arc_id,
            band_id=band_row.band_id,
            chapter_start=int(band_row.chapter_start or 0),
            chapter_end=int(band_row.chapter_end or 0),
            trigger_source="auto_band_end",
            boundary_kind="band_end",
            boundary_chapter=chapter_number,
            status=status,
            summary=summary,
            issues=issues,
        )
    )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        band_id=band_row.band_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.BAND_CHECKPOINT_CREATED,
        scope="band",
        summary=summary,
        related_object_type="band_checkpoint",
        related_object_id=row.id,
            payload={
                "status": status,
                "chapter_review_form_result": {},
                "band_checkpoint_mode": "chapter_review_form",
            },
        )
    return row

# ------------------------------------------------------------------
# State seeding from arc plan
# ------------------------------------------------------------------

@staticmethod
def _filter_supported_state_changes(changes):
    filtered = []
    for change in changes:
        known_fields = KNOWN_STATE_FIELDS.get(change.entity_kind, set())
        if known_fields and change.field not in known_fields:
            logger.warning(
                "Dropping unsupported state change field %r for entity kind %r.",
                change.field,
                change.entity_kind,
            )
            continue
        filtered.append(change)
    return filtered



__all__ = ['_project_governance', '_record_decision_event', '_record_engine_decision_event', '_record_legacy_compatibility_event', '_audit_current_plan_before_write', '_audit_future_plans_after_acceptance', '_future_plan_audit_plans', '_future_plan_audit_band_rows', '_record_future_plan_audit_events', '_record_generation_audit_checkpoint_if_due', '_generation_audit_checkpoint_payload', '_previous_band_row', '_manual_boundary_checkpoint', '_strict_progression_block', '_create_auto_band_checkpoint', '_filter_supported_state_changes']
