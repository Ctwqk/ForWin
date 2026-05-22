from __future__ import annotations

from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.orchestrator_loop_core.common import *

def _make_state_helpers(
    self,
    session: Session,
) -> tuple[StateRepository, StateUpdater, ContinuityChecker]:
    repo = StateRepository(session)
    updater = StateUpdater(session)
    checker = ContinuityChecker(
        repo,
        min_chars=self.config.min_chapter_chars,
        max_chars=self.config.max_chapter_chars,
    )
    return repo, updater, checker

def _select_skill_layers(
    self,
    *,
    scope: str,
    stage_key: str,
    task_family: str,
) -> list[object]:
    selections = self.skill_router.select(
        scope=scope,
        stage_key=stage_key,
        task_family=task_family,
    )
    return self.skill_prompt_layer_builder.build(selections)

@staticmethod
def _filter_supported_kwargs(
    callable_obj: Callable[..., Any],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    target_callable = callable_obj
    side_effect = getattr(callable_obj, "side_effect", None)
    if callable(side_effect):
        target_callable = side_effect
    try:
        signature = inspect.signature(target_callable)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return dict(kwargs)
    return {
        key: value
        for key, value in kwargs.items()
        if key in parameters
    }

def _call_with_compatible_kwargs(
    self,
    callable_obj: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    return callable_obj(*args, **self._filter_supported_kwargs(callable_obj, kwargs))

def _save_prompt_trace_payload(
    self,
    *,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    prompt_trace: dict[str, object] | None,
    parent_trace_id: str = "",
    decision_event_id: str = "",
) -> str:
    payload = prompt_trace if isinstance(prompt_trace, dict) else {}
    if not payload:
        return ""
    input_snapshot = payload.get("input_snapshot") if isinstance(payload.get("input_snapshot"), dict) else {}
    output_summary = payload.get("output_summary") if isinstance(payload.get("output_summary"), dict) else {}
    trace_chapter_number = int(
        (input_snapshot or {}).get("chapter_number")
        or (output_summary or {}).get("chapter_number")
        or 0
    )
    payload = prepare_prompt_trace_payload(
        payload,
        artifact_store=self.artifact_store,
        project_id=project_id,
        chapter_number=trace_chapter_number,
    )
    project = session.get(Project, project_id)
    row = updater.save_prompt_trace(
        project_id=project_id,
        genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
        decision_event_id=str(decision_event_id or "").strip(),
        parent_trace_id=str(parent_trace_id or "").strip(),
        trace_scope=str(payload.get("trace_scope", "writer") or "writer"),
        stage_key=str(payload.get("stage_key", "") or ""),
        template_id=str(payload.get("template_id", "") or ""),
        template_version=str(payload.get("template_version", "v1") or "v1"),
        effective_system_prompt=str(payload.get("effective_system_prompt", "") or ""),
        prompt_layers_json=json.dumps(payload.get("prompt_layers", []), ensure_ascii=False),
        input_snapshot_json=json.dumps(payload.get("input_snapshot", {}), ensure_ascii=False),
        model_profile_json=json.dumps(payload.get("model_profile", {}), ensure_ascii=False),
        attempts_json=json.dumps(payload.get("attempts", []), ensure_ascii=False),
        output_summary_json=json.dumps(payload.get("output_summary", {}), ensure_ascii=False),
        backend=str(payload.get("backend", "") or ""),
        codex_job_id=str(payload.get("codex_job_id", "") or ""),
        permission_profile=str(payload.get("permission_profile", "") or ""),
        fallback_used=bool(payload.get("fallback_used", False)),
    )
    for event_payload in build_llm_decision_event_payloads(payload, prompt_trace_id=row.id):
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=trace_chapter_number,
            event_family=str(event_payload.get("event_family") or "runtime_observation"),
            event_type=str(event_payload.get("event_type") or DecisionEventType.LLM_REQUEST_FAILED),
            scope="chapter" if trace_chapter_number else "project",
            summary=str(event_payload.get("summary") or "LLM trace event."),
            payload=event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {},
            related_object_type="prompt_trace",
            related_object_id=row.id,
            parent_event_id=str(decision_event_id or "").strip(),
        )
    self._record_prompt_trace_performance_spans(
        project_id=project_id,
        chapter_number=trace_chapter_number,
        prompt_trace_id=row.id,
        trace_payload=payload,
    )
    return row.id

def _record_prompt_trace_performance_spans(
    self,
    *,
    project_id: str,
    chapter_number: int,
    prompt_trace_id: str,
    trace_payload: dict[str, object],
) -> None:
    attempts = trace_payload.get("attempts") if isinstance(trace_payload, dict) else []
    if not isinstance(attempts, list):
        return
    trace_scope = str(trace_payload.get("trace_scope") or "llm").strip() or "llm"
    fallback_stage = str(trace_payload.get("stage_key") or "").strip()
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        stage_key = str(attempt.get("stage_key") or fallback_stage or "").strip()
        try:
            duration_ms = max(0, int(attempt.get("duration_ms") or 0))
        except (TypeError, ValueError):
            duration_ms = 0
        tags = redact_payload(
            {
                "prompt_trace_id": prompt_trace_id,
                "trace_scope": trace_scope,
                "stage_key": stage_key,
                "profile_id": str(attempt.get("profile_id") or ""),
                "profile_name": str(attempt.get("profile_name") or ""),
                "model": str(attempt.get("model") or ""),
                "llm_task_route": str(attempt.get("llm_task_route") or ""),
                "http_status": int(attempt.get("http_status") or 0),
                "attempt_no": int(attempt.get("attempt_no") or 0),
                "attempt_group_id": str(attempt.get("attempt_group_id") or ""),
                "retryable": bool(attempt.get("retryable", False)),
                "fallback_eligible": bool(attempt.get("fallback_eligible", False)),
                "final_failure": bool(attempt.get("final_failure", False)),
                "parse_ok": bool(attempt.get("parse_ok", True)),
                "schema_ok": bool(attempt.get("schema_ok", True)),
            }
        )
        metrics = {
            "input_chars": int(attempt.get("input_chars") or 0),
            "output_chars": int(attempt.get("output_chars") or 0),
            "sleep_ms": int(attempt.get("sleep_ms") or 0),
        }
        failed = bool(
            attempt.get("error_class")
            or attempt.get("final_failure")
            or attempt.get("parse_error")
        )
        error = {}
        if failed:
            error = redact_payload(
                {
                    "error_class": str(attempt.get("error_class") or ""),
                    "error_message": str(
                        attempt.get("error_message")
                        or attempt.get("parse_error")
                        or attempt.get("error_category")
                        or ""
                    ),
                    "error_category": str(attempt.get("error_category") or ""),
                }
            )
        context = OperationContext(
            project_id=project_id,
            task_id=self._governance_task_id,
            chapter_number=int(chapter_number or 0),
            stage=stage_key,
            operation_id=self._audit_operation_id(),
        )
        parent_span = current_span()
        record = SpanRecord(
            context=context,
            span_name="llm.request",
            span_kind="llm",
            component=trace_scope,
            tags=tags,
            metrics=metrics,
            status="failed" if failed else "ok",
            error=error,
            trace_id=str(getattr(parent_span, "trace_id", "") or prompt_trace_id),
            span_id=new_id(),
            parent_span_id=str(getattr(parent_span, "span_id", "") or ""),
            start_time_unix_ms=int(time.time() * 1000),
            duration_ms=duration_ms,
            self_duration_ms=duration_ms,
        )
        try:
            self.observability._record_span(record)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring prompt trace performance span failure.", exc_info=True)



__all__ = ['_make_state_helpers', '_select_skill_layers', '_filter_supported_kwargs', '_call_with_compatible_kwargs', '_save_prompt_trace_payload', '_record_prompt_trace_performance_spans']
