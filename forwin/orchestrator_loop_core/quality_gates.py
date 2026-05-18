from __future__ import annotations

from forwin.orchestrator_loop_core.common import *

@staticmethod
def _is_timeout_like(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in ("timed out", "timeout", "read operation timed out")
    )

@staticmethod
def _is_transient_llm_like(exc: Exception) -> bool:
    current: BaseException | None = exc
    while current is not None:
        message = str(current).lower()
        compact = " ".join(message.split())
        if any(
            token in compact
            for token in (
                "http 529",
                "status code 529",
                "529 unknown status code",
                "429",
                "500",
                "502",
                "503",
                "504",
                "temporarily unavailable",
                "service unavailable",
                "rate limit",
                "too many requests",
                "overloaded",
                "connection reset",
                "connection refused",
                "remoteprotocolerror",
                "server disconnected",
                "network error",
                "timed out",
                "timeout",
                "read operation timed out",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False

@staticmethod
def _transient_retry_delay(attempt: int) -> float:
    return min(20.0, 3.0 * (2 ** max(0, attempt - 1)))

def _current_model_identity(self) -> tuple[str, str]:
    profile_id = ""
    profile = getattr(self.config, "llm_fallback_profiles", None) or []
    primary = profile[0] if isinstance(profile, list) and profile else {}
    if isinstance(primary, dict):
        profile_id = str(primary.get("id", "")).strip()
    return profile_id, str(self.config.minimax_model or "").strip()

def _audit_operation_id(self) -> str:
    return str(self._governance_task_id or self._governance_root_event_id or "").strip()

def _drain_llm_attempt_events(self) -> list[dict[str, object]]:
    drain = getattr(getattr(self.writer, "llm_client", None), "drain_llm_attempt_events", None)
    if not callable(drain):
        return []
    events = drain()
    return [dict(item) for item in events if isinstance(item, dict)] if isinstance(events, list) else []

@staticmethod
def _safe_prompt_trace_attempts(
    attempts: list[dict[str, object]],
    *,
    fallback_attempt_no: int = 0,
    exc: BaseException | None = None,
    duration_ms: int = 0,
) -> list[dict[str, object]]:
    allowed_keys = {
        "attempt_group_id",
        "profile_id",
        "profile_name",
        "model",
        "preferred_provider_kind",
        "preferred_model",
        "base_url_host",
        "requested_temperature",
        "requested_max_tokens",
        "timeout_seconds",
        "attempt_no",
        "http_status",
        "provider_request_id",
        "duration_ms",
        "input_chars",
        "output_chars",
        "task_family",
        "stage_key",
        "llm_task_route",
        "retry_after",
        "sleep_ms",
        "error_class",
        "error_message",
        "error_category",
        "timeout_kind",
        "retryable",
        "fallback_eligible",
        "final_failure",
    }
    safe_attempts: list[dict[str, object]] = []
    for attempt in attempts:
        safe: dict[str, object] = {
            key: value
            for key, value in attempt.items()
            if key in allowed_keys and value is not None
        }
        if "error_message" in safe:
            safe["error_message"] = safe_error_summary(str(safe.get("error_message") or ""))
        safe_attempts.append(safe)
    if not safe_attempts and exc is not None:
        safe_attempts.append(
            {
                "attempt_no": int(fallback_attempt_no or 0),
                "duration_ms": max(0, int(duration_ms or 0)),
                "error_class": exc.__class__.__name__,
                "error_message": safe_error_summary(exc),
                "error_category": "unknown",
                "final_failure": True,
            }
        )
    return safe_attempts

@staticmethod
def _error_category_from_attempts(attempts: list[dict[str, object]], exc: BaseException) -> str:
    for attempt in reversed(attempts):
        category = str(attempt.get("error_category") or "").strip()
        if category and category != "unknown":
            return category
    message = str(exc).lower()
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "429" in message or "rate limit" in message:
        return "rate_limit"
    if any(token in message for token in ("529", "500", "502", "503", "504", "overload")):
        return "provider_overload"
    if "400" in message or "bad request" in message:
        return "bad_request"
    if "parse" in message or "json" in message or "schema" in message:
        return "parse_or_schema"
    return "unknown"

@staticmethod
def _diagnostic_kind_for_failure(exc: BaseException, error_category: str) -> str:
    message = str(exc).lower()
    if error_category == "bad_request" or "400" in message or "bad request" in message:
        return "provider_bad_request"
    if error_category == "parse_or_schema" or any(
        token in message for token in ("parse", "json", "schema")
    ):
        return "parse_or_schema_failure"
    return "writer_failure_without_draft"

def _record_failure_prompt_trace(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    context,
    stage_key: str,
    template_id: str,
    source_event_id: str,
    exc: BaseException,
    duration_ms: int,
    attempts: list[dict[str, object]],
    skill_layers: list[object] | None,
    fallback_stage: str = "",
) -> str:
    if not isinstance(updater, StateUpdater):
        return ""
    fallback_attempt_no = 0
    if attempts:
        try:
            fallback_attempt_no = int(attempts[-1].get("attempt_no") or 0)
        except (TypeError, ValueError):
            fallback_attempt_no = 0
    safe_attempts = self._safe_prompt_trace_attempts(
        attempts,
        fallback_attempt_no=fallback_attempt_no,
        exc=exc,
        duration_ms=duration_ms,
    )
    error_category = self._error_category_from_attempts(safe_attempts, exc)
    selected_skills = ChapterWriter._selected_skills_from_layers(skill_layers)
    operation_id = self._audit_operation_id()
    model_profile_id, model_name = self._current_model_identity()
    trace_payload = {
        "trace_scope": "writer",
        "stage_key": stage_key,
        "template_id": template_id,
        "template_version": "v1",
        "effective_system_prompt": "",
        "prompt_layers": [],
        "input_snapshot": audit_payload(
            stage=stage_key,
            status="failed",
            operation_id=operation_id,
            chapter_number=chapter_number,
            writer_mode=str(getattr(self.writer, "writer_mode", "") or ""),
            selected_skills=selected_skills,
        ),
        "model_profile": {
            "profile_id": model_profile_id,
            "model": model_name,
            "base_url": str(getattr(self.llm_client, "base_url", "") or ""),
        },
        "attempts": safe_attempts,
        "output_summary": audit_payload(
            stage=stage_key,
            status="failed",
            operation_id=operation_id,
            duration_ms=duration_ms,
            error_category=error_category,
            chapter_number=chapter_number,
            context_chapter_number=int(getattr(context, "chapter_number", chapter_number) or chapter_number),
            error_class=exc.__class__.__name__,
            error_summary=safe_error_summary(exc),
            fallback_stage=fallback_stage,
            attempt_count=len(safe_attempts),
            attempt_group_ids=attempt_group_ids(safe_attempts),
        ),
    }
    trace_id = self._save_prompt_trace_payload(
        session=updater.session,
        updater=updater,
        project_id=project_id,
        prompt_trace=trace_payload,
        decision_event_id=source_event_id,
    )
    artifact_manifest: list[dict[str, object]] = []
    try:
        manifest = self.artifact_store.save_observability_diagnostic(
            project_id=project_id,
            chapter_number=chapter_number,
            kind=self._diagnostic_kind_for_failure(exc, error_category),
            source_event_id=source_event_id,
            trace_id=trace_id,
            payload={
                "schema_version": "v4.5.1-audit",
                "project_id": project_id,
                "chapter_number": chapter_number,
                "stage": stage_key,
                "status": "failed",
                "operation_id": operation_id,
                "error_class": exc.__class__.__name__,
                "error_summary": safe_error_summary(exc),
                "error_category": error_category,
                "attempts": safe_attempts,
                "selected_skills": selected_skills,
            },
        )
        artifact_manifest.append(manifest)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to persist observability diagnostic artifact.", exc_info=True)
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
        scope="chapter",
        summary=f"第{chapter_number}章失败 prompt trace 已落盘。",
        parent_event_id=source_event_id,
        related_object_type="prompt_trace",
        related_object_id=trace_id,
        payload=audit_payload(
            stage=stage_key,
            status="failed",
            operation_id=operation_id,
            duration_ms=duration_ms,
            error_category=error_category,
            trace_id=trace_id,
            source_event_id=source_event_id,
            artifact_manifest=artifact_manifest,
        ),
    )
    return trace_id

def _record_model_fallback_payloads(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    parent_stage: str,
    events: list[dict[str, Any]],
) -> None:
    for item in events:
        if not isinstance(item, dict):
            continue
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.FALLBACK_PROFILE_SWITCHED,
            scope="chapter",
            summary=(
                f"writer fallback: {str(item.get('from_model') or '-')} -> "
                f"{str(item.get('to_model') or '-')}"
            ),
            payload=audit_payload(
                stage=parent_stage,
                status="profile_switched",
                operation_id=self._audit_operation_id(),
                model_profile_id=str(item.get("to_profile_id") or ""),
                model=str(item.get("to_model") or ""),
                error_summary=safe_error_summary(str(item.get("reason") or "")),
                from_model_profile_id=str(item.get("from_profile_id") or ""),
                from_model=str(item.get("from_model") or ""),
            ),
        )

def _apply_canon_quality_gate(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    verdict: ReviewVerdict,
) -> str:
    latest_draft, latest_review = self._latest_draft_and_review_for_chapter(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    draft_id = str(getattr(latest_draft, "id", "") or "")
    review_id = str(getattr(latest_review, "id", "") or "")
    analysis = analyze_writer_output_quality(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        draft_id=draft_id,
        persist=True,
        mode=str(getattr(self.config, "chapter_review_form_mode", "primary") or "primary"),
        llm_client=self.llm_client,
        return_raw_analyzer_results=True,
    )
    deferred_acceptance_errors = self._prepare_deferred_acceptance_if_needed(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        review_id=review_id,
        verdict=verdict,
        signals=analysis.signals,
        target_total_chapters=int(getattr(session.get(Project, project_id), "target_total_chapters", 0) or 0),
    )
    if deferred_acceptance_errors:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
            scope="chapter",
            summary=f"第{chapter_number}章 deferred acceptance 计划补丁失败。",
            reason=";".join(deferred_acceptance_errors),
            payload={"deferred_acceptance_errors": deferred_acceptance_errors},
        )
        return "deferred-acceptance-blocked"
    obligation_repo = NarrativeObligationRepository(session)
    gate_obligations = [
        *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number),
        *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
    ]
    patch_ids = sorted(
        {
            patch_id
            for obligation in gate_obligations
            for patch_id in obligation.linked_plan_patch_ids
            if patch_id
        }
    )
    project = session.get(Project, project_id)
    target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
    gate_analyzer_results = [
        item for item in analysis.raw_analyzer_results if isinstance(item, dict)
    ]
    gate_result = evaluate_canon_admission(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        review_id=review_id,
        review_verdict=verdict.verdict,
        signals=analysis.signals,
        obligations=gate_obligations,
        plan_patches=obligation_repo.list_patches_by_ids(patch_ids),
        mode=str(getattr(self.config, "canon_quality_gate", "strict") or "strict"),
        is_final_chapter=bool(target_total_chapters and chapter_number >= target_total_chapters),
        analyzer_results=gate_analyzer_results,
        min_blocking_confidence=float(getattr(self.config, "chapter_review_form_min_blocking_confidence", 0.8) or 0.8),
        require_evidence_for_block=True,
    )
    CanonQualityRepository(session).save_admission_run(gate_result, signals=analysis.signals)
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
        scope="chapter",
        summary=f"第{chapter_number}章 canon quality gate: {gate_result.verdict}",
        related_object_type="canon_admission_run",
        payload=gate_result.model_dump(mode="json"),
    )
    if gate_result.commit_allowed:
        return ""
    frozen_path = ""
    if self.config.freeze_failed_candidates:
        frozen_path = self.artifact_store.save_frozen_candidate(
            project_id=project_id,
            chapter_number=chapter_number,
            payload={
                "reason": "canon-quality-gate-blocked",
                "chapter_number": chapter_number,
                "writer_output": writer_output.model_dump(mode="json"),
                "review_verdict": verdict.model_dump(mode="json"),
                "canon_quality_gate": gate_result.model_dump(mode="json"),
                "canon_quality_signals": [
                    signal.model_dump(mode="json") for signal in analysis.signals
                ],
                "chapter_review_form_results": gate_analyzer_results,
            },
        )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
        scope="chapter",
        summary=f"第{chapter_number}章 canon quality gate 阻止 canon 写入。",
        reason=gate_result.gate_summary,
        payload=gate_result.model_dump(mode="json"),
    )
    return frozen_path or "canon-quality-gate-blocked"

def _run_obligation_form_gate(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    obligations: list[NarrativeObligation],
) -> list[dict[str, Any]]:
    return []

def _prepare_deferred_acceptance_if_needed(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    review_id: str,
    verdict: ReviewVerdict,
    signals: list[Any],
    target_total_chapters: int,
) -> list[str]:
    outcome = ReviewOutcomeRouter().route(
        review=verdict,
        signals=signals,
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
    )
    if outcome.action not in {"defer_with_chapter_plan_patch", "defer_with_band_plan_patch"}:
        return []
    issue_type = str(outcome.primary_issue_class or "").strip()
    if not issue_type:
        return []
    existing = session.execute(
        select(NarrativeObligationRow)
        .where(
            NarrativeObligationRow.project_id == project_id,
            NarrativeObligationRow.origin_chapter_number == int(chapter_number or 0),
            NarrativeObligationRow.obligation_type == issue_type,
            NarrativeObligationRow.status.in_(("planned", "active")),
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return []

    bands = self._band_scope_candidates(
        session=session,
        project_id=project_id,
        current_chapter=chapter_number,
    )
    scope_decision = ObligationScopeRouter().route(
        issue_type=issue_type,
        priority=_priority_for_deferred_issue(issue_type),
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
        bands=bands,
    )
    if scope_decision.action not in {"defer_with_chapter_plan_patch", "defer_with_band_plan_patch"}:
        return [scope_decision.reason or f"deferred_acceptance_scope_unavailable:{issue_type}"]

    obligation_id = new_id()
    summary = _summary_for_deferred_issue(verdict=verdict, issue_type=issue_type, outcome_reason=outcome.reason)
    payoff_test = _payoff_test_for_deferred_issue(
        verdict=verdict,
        issue_type=issue_type,
        deadline_chapter=scope_decision.deadline_chapter,
        summary=summary,
    )
    obligation = NarrativeObligation(
        id=obligation_id,
        project_id=project_id,
        origin_chapter_number=int(chapter_number or 0),
        origin_draft_id=draft_id,
        origin_review_id=review_id,
        origin_signal_ids=[
            str(getattr(signal, "signal_id", "") or "")
            for signal in signals
            if str(getattr(signal, "signal_type", "") or "") == issue_type
            and str(getattr(signal, "signal_id", "") or "")
        ],
        obligation_type=issue_type,
        priority=_priority_for_deferred_issue(issue_type),
        status="proposed",
        summary=summary,
        deferral_reason=scope_decision.reason or outcome.reason,
        hardness="design_debt",
        deadline_chapter=int(scope_decision.deadline_chapter or 0),
        payoff_test=payoff_test,
        evidence_refs=[f"review:{review_id}"] if review_id else [],
        metadata={"minimum_scope": scope_decision.target_scope},
    )

    if scope_decision.action == "defer_with_band_plan_patch":
        band_row = self._band_row_by_id(
            session=session,
            project_id=project_id,
            band_id=scope_decision.target_band_id,
        )
        if band_row is None:
            return [f"target_band_not_found:{scope_decision.target_band_id}"]
        plan_patch = BandPlanPatcher().build_obligation_patch(
            project_id=project_id,
            band_row=band_row,
            obligations=[obligation],
            current_chapter=chapter_number,
            patch_type="band_defer_acceptance",
        )
    else:
        target_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == int(scope_decision.deadline_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if target_plan is None:
            return [f"target_chapter_plan_not_found:{scope_decision.deadline_chapter}"]
        plan_patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="defer_acceptance",
            target_scope="chapter",
            target_plan_id=str(target_plan.id or ""),
            target_arc_id=str(target_plan.arc_plan_id or ""),
            affected_chapters=[int(scope_decision.deadline_chapter or 0)],
            source_obligation_ids=[obligation_id],
            new_contract={
                "obligations_to_resolve": [obligation_id],
                "payoff_test": payoff_test,
                "summary": summary,
            },
            diff_summary=f"Bind deferred obligation {obligation_id} to chapter {scope_decision.deadline_chapter}.",
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation_id,
                    "priority": obligation.priority,
                    "summary": summary,
                    "payoff_test": payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation_id,
                    "payoff_test": payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[payoff_test],
        )
    result = DeferAcceptanceTransaction(session).run(
        obligation=obligation,
        plan_patch=plan_patch,
        current_chapter=chapter_number,
        target_total_chapters=target_total_chapters,
    )
    return [] if result.success else list(result.errors)

@staticmethod
def _band_scope_candidates(
    *,
    session: Session,
    project_id: str,
    current_chapter: int,
) -> list[BandScopeCandidate]:
    rows = session.execute(
        select(BandExperiencePlan)
        .where(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.chapter_end > int(current_chapter or 0),
        )
        .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.chapter_end.asc())
    ).scalars().all()
    if not rows:
        return []
    plans = session.execute(
        select(ChapterPlan)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number > int(current_chapter or 0),
            ChapterPlan.status.in_(("planned", "failed")),
        )
    ).scalars().all()
    planned_numbers = [int(plan.chapter_number or 0) for plan in plans]
    result: list[BandScopeCandidate] = []
    for row in rows:
        start = int(row.chapter_start or 0)
        end = int(row.chapter_end or 0)
        result.append(
            BandScopeCandidate(
                band_id=str(row.band_id or ""),
                arc_id=str(row.arc_id or ""),
                chapter_start=start,
                chapter_end=end,
                planned_chapters=[number for number in planned_numbers if start <= number <= end],
            )
        )
    return result

@staticmethod
def _band_row_by_id(
    *,
    session: Session,
    project_id: str,
    band_id: str,
) -> BandExperiencePlan | None:
    return session.execute(
        select(BandExperiencePlan)
        .where(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.band_id == band_id,
        )
        .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
        .limit(1)
    ).scalar_one_or_none()

def _latest_draft_and_review_for_chapter(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> tuple[ChapterDraft | None, ChapterReview | None]:
    chapter_plan = session.execute(
        select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
    ).scalar_one_or_none()
    if chapter_plan is None:
        return None, None
    latest_draft = session.execute(
        select(ChapterDraft)
        .where(ChapterDraft.chapter_plan_id == chapter_plan.id)
        .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_draft is None:
        return None, None
    latest_review = session.execute(
        select(ChapterReview)
        .where(ChapterReview.draft_id == latest_draft.id)
        .order_by(ChapterReview.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest_draft, latest_review

def _apply_canon_candidate(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    verdict: ReviewVerdict,
) -> str | None:
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.CANON_COMMIT_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 canon 写入开始。",
        payload={
            "state_changes_count": len(getattr(writer_output, "state_changes", []) or []),
            "events_count": len(getattr(writer_output, "new_events", []) or []),
            "thread_beats_count": len(getattr(writer_output, "thread_beats", []) or []),
        },
    )
    try:
        quality_blocked_path = self._apply_canon_quality_gate(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
            verdict=verdict,
        )
        if quality_blocked_path:
            return quality_blocked_path
        v4_blocked_path = self._apply_world_v4_gate(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
            verdict=verdict,
        )
        if v4_blocked_path:
            return v4_blocked_path
        self._validate_subworld_admission(
            repo=repo,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
        )
        self._ensure_genesis_canon_seed_entities(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project_id,
        )
        filtered_state_changes = self._filter_supported_state_changes(
            writer_output.state_changes
        )
        filtered_state_changes = self._filter_resolvable_state_changes(
            repo,
            project_id,
            chapter_number,
            filtered_state_changes,
        )
        updater.apply_state_changes(
            project_id, chapter_number, filtered_state_changes
        )
        filtered_events = self._filter_resolvable_events(
            repo,
            project_id,
            chapter_number,
            writer_output.new_events,
        )
        updater.apply_events(
            project_id, chapter_number, filtered_events
        )
        updater.apply_thread_beats(
            project_id, chapter_number, writer_output.thread_beats
        )
        if writer_output.time_advance:
            updater.apply_time_advance(
                project_id, chapter_number, writer_output.time_advance
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="business_event",
            event_type=DecisionEventType.CANON_COMMIT,
            scope="chapter",
            summary=f"第{chapter_number}章 canon 写入成功。",
            payload={"issue_count": len(verdict.issues)},
        )
        NarrativeObligationRepository(session).activate_planned_for_chapter(
            project_id,
            origin_chapter_number=chapter_number,
        )
        CandidateDraftRepository(session).mark_canon_committed(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        return None
    except Exception as exc:
        logger.exception(
            "Canon update failed for chapter %d; keeping saved draft and review.",
            chapter_number,
        )
        frozen_path = ""
        if self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "canon-update-failed",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "review_verdict": verdict.model_dump(mode="json"),
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon 写入失败。",
        )
        session.rollback()
        CandidateDraftRepository(session).mark_canon_failed(
            project_id=project_id,
            chapter_number=chapter_number,
            failure_reason=str(exc),
            canon_artifact_path=frozen_path,
        )
        return frozen_path or None



__all__ = ['_is_timeout_like', '_is_transient_llm_like', '_transient_retry_delay', '_current_model_identity', '_audit_operation_id', '_drain_llm_attempt_events', '_safe_prompt_trace_attempts', '_error_category_from_attempts', '_diagnostic_kind_for_failure', '_record_failure_prompt_trace', '_record_model_fallback_payloads', '_apply_canon_quality_gate', '_run_obligation_form_gate', '_prepare_deferred_acceptance_if_needed', '_band_scope_candidates', '_band_row_by_id', '_latest_draft_and_review_for_chapter', '_apply_canon_candidate']
