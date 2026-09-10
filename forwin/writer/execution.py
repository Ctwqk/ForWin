"""Independent Writer execution; the coordinator owns pause and chapter progress."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from forwin.audit.events import DecisionEventType
from forwin.llm.compat import filter_supported_kwargs
from forwin.observability.llm_trace import safe_prompt_trace_attempts
from forwin.observability.payloads import (
    attempt_group_ids,
    audit_payload,
    event_error_payload,
    safe_error_summary,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.skills import SkillPromptLayerBuilder, SkillRouter
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore

from .chapter_writer import ChapterWriter
from .execution_errors import (
    TransientLLMChapterFailure,
    error_category_from_attempts,
    is_timeout_like,
    is_transient_llm_like,
    transient_retry_delay,
)
from .execution_telemetry import WriterExecutionTelemetry, prompt_trace_success_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WriterExecutionRequest:
    context: ChapterContextPack
    project_id: str
    chapter_number: int
    updater: StateUpdater
    trace_stage_key: str = "chapter_draft"
    llm_preferred_provider_kind: str = ""
    llm_preferred_model: str = ""


@dataclass(frozen=True, slots=True)
class WriterExecutionResult:
    output: WriterOutput | None = None
    error: Exception | None = None
    frozen_artifacts: tuple[str, ...] = ()

    @property
    def aborted(self) -> bool:
        return self.output is None and self.error is None

    def unwrap(self) -> WriterOutput | None:
        if self.error is not None:
            raise self.error
        return self.output


def _call_compatible(function, *args, **kwargs):
    return function(*args, **filter_supported_kwargs(function, kwargs))


class WriterExecution:
    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        writer: ChapterWriter,
        skill_router: SkillRouter,
        skill_prompt_layer_builder: SkillPromptLayerBuilder,
        artifact_store: ArtifactStore,
        telemetry: WriterExecutionTelemetry,
        should_abort: Callable[[], bool] | None = None,
    ):
        self.policy = policy
        self.writer = writer
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder
        self.artifact_store = artifact_store
        self.telemetry = telemetry
        self.should_abort = should_abort

    def execute(self, request: WriterExecutionRequest) -> WriterExecutionResult:
        frozen_artifacts: list[str] = []
        try:
            output = self._execute(request, frozen_artifacts)
        except Exception as exc:  # noqa: BLE001 - unwrap preserves the original failure.
            # Callers propagate the original exception through unwrap, after
            # retaining the artifacts this attempt produced.
            return WriterExecutionResult(
                error=exc, frozen_artifacts=tuple(frozen_artifacts)
            )
        return WriterExecutionResult(
            output=output, frozen_artifacts=tuple(frozen_artifacts)
        )

    def _abort_requested(self) -> bool:
        try:
            return bool(self.should_abort and self.should_abort())
        except Exception:
            logger.debug("Ignoring abort predicate failure.", exc_info=True)
            return False

    def _execute(
        self, request: WriterExecutionRequest, frozen_artifacts: list[str]
    ) -> WriterOutput | None:
        context = request.context
        project_id = request.project_id
        chapter_number = request.chapter_number
        updater = request.updater
        trace_stage_key = request.trace_stage_key
        llm_preferred_provider_kind = request.llm_preferred_provider_kind
        llm_preferred_model = request.llm_preferred_model
        max_attempts = max(1, int(self.policy.writer_attention_retries))
        last_error: Exception | None = None
        last_failure_event_id = ""
        last_failed_attempt = 0
        saw_transient_error = False
        writer_skill_layers = self.skill_prompt_layer_builder.build(
            self.skill_router.select(
                scope="writer",
                stage_key=trace_stage_key,
                task_family="write_chapter",
            )
        )
        for attempt in range(1, max_attempts + 1):
            if self._abort_requested():
                return None
            started_at = time.perf_counter()
            model_profile_id, model_name = self.telemetry.model_identity()
            try:
                self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_STARTED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 第 {attempt}/{max_attempts} 次调用开始。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="started",
                        operation_id=self.telemetry.operation_id(),
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                output = _call_compatible(
                    self.writer.write_chapter,
                    context,
                    skill_layers=writer_skill_layers,
                    trace_stage_key=trace_stage_key,
                    llm_preferred_provider_kind=llm_preferred_provider_kind,
                    llm_preferred_model=llm_preferred_model,
                )
                duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                self.telemetry.record_model_fallbacks(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    parent_stage="writing_chapter",
                    events=output.generation_meta.get("model_fallbacks") or [],
                )
                self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_SUCCEEDED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 第 {attempt}/{max_attempts} 次调用成功。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self.telemetry.operation_id(),
                        duration_ms=duration_ms,
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 调用耗时 {duration_ms}ms。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self.telemetry.operation_id(),
                        duration_ms=duration_ms,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        attempt_no=attempt,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_OUTPUT_BUILT,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer output 已生成。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self.telemetry.operation_id(),
                        duration_ms=duration_ms,
                        char_count=int(getattr(output, "char_count", 0) or 0),
                        mode=str(
                            (getattr(output, "generation_meta", {}) or {}).get("mode")
                            or ""
                        ),
                        scene_count=len(getattr(output, "scene_outputs", []) or []),
                        state_changes_count=len(
                            getattr(output, "state_changes", []) or []
                        ),
                        events_count=len(getattr(output, "new_events", []) or []),
                        delivered_payoffs_count=len(
                            getattr(output, "delivered_payoffs", []) or []
                        ),
                        thread_beats_count=len(
                            getattr(output, "thread_beats", []) or []
                        ),
                    ),
                )
                return output
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                is_transient = is_transient_llm_like(exc)
                saw_transient_error = saw_transient_error or is_transient
                duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                self.telemetry.record_model_fallbacks(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    parent_stage="writing_chapter",
                    events=list(
                        getattr(
                            self.writer.llm_client,
                            "drain_model_fallback_events",
                            list,
                        )()
                        or []
                    ),
                )
                llm_attempts = safe_prompt_trace_attempts(
                    self.telemetry.drain_attempts(),
                    fallback_attempt_no=attempt,
                    exc=exc,
                    duration_ms=duration_ms,
                )
                error_category = error_category_from_attempts(llm_attempts, exc)
                logger.warning(
                    "Writer failed for chapter %d on attempt %d/%d: %s",
                    chapter_number,
                    attempt,
                    max_attempts,
                    exc,
                )
                failure_event = self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_FAILED,
                    scope="chapter",
                    summary=f"Writer 第 {attempt}/{max_attempts} 次调用失败：{safe_error_summary(exc)}",
                    payload=event_error_payload(
                        exc,
                        stage="writing_chapter",
                        operation_id=self.telemetry.operation_id(),
                        duration_ms=duration_ms,
                        error_category=error_category,
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        is_transient=is_transient,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        attempt_count=len(llm_attempts),
                        attempt_group_ids=attempt_group_ids(llm_attempts),
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                drain_attempts = getattr(
                    self.writer.llm_client, "drain_llm_attempt_events", None
                )
                failed_attempts = drain_attempts() if callable(drain_attempts) else []
                if failed_attempts:
                    self.telemetry.save_prompt_trace(
                        session=updater.session,
                        updater=updater,
                        project_id=project_id,
                        prompt_trace={
                            "trace_scope": "writer",
                            "stage_key": trace_stage_key,
                            "template_id": "writer:failure",
                            "template_version": "v1",
                            "effective_system_prompt": "",
                            "prompt_layers": [],
                            "input_snapshot": {
                                "chapter_number": chapter_number,
                                "stage_key": trace_stage_key,
                                "failure_path": "writer_before_output",
                            },
                            "model_profile": {
                                "profile_id": model_profile_id,
                                "model": model_name,
                            },
                            "attempts": failed_attempts,
                            "output_summary": {
                                "status": "failed",
                                "chapter_number": chapter_number,
                                "error_class": exc.__class__.__name__,
                                "error_message": str(exc),
                            },
                        },
                        parent_trace_id="",
                        decision_event_id=str(getattr(failure_event, "id", "") or ""),
                    )
                last_failure_event_id = str(getattr(failure_event, "id", "") or "")
                last_failed_attempt = attempt
                self.telemetry.record_failure_trace(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    context=context,
                    stage_key=trace_stage_key,
                    template_id="writer:failure",
                    source_event_id=last_failure_event_id,
                    exc=exc,
                    duration_ms=duration_ms,
                    attempts=llm_attempts,
                    skill_layers=writer_skill_layers,
                )
                if is_timeout_like(exc):
                    logger.warning(
                        "Writer timeout detected for chapter %d; skipping extra retries.",
                        chapter_number,
                    )
                    break
                if is_transient and attempt < max_attempts:
                    delay = transient_retry_delay(attempt)
                    logger.warning(
                        "Transient LLM failure detected for chapter %d; waiting %.1f s before writer retry %d/%d.",
                        chapter_number,
                        delay,
                        attempt + 1,
                        max_attempts,
                    )
                    self.telemetry.record_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_number,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.RETRY_ATTEMPT,
                        scope="chapter",
                        summary=f"第{chapter_number}章准备进行 writer retry。",
                        payload=audit_payload(
                            stage="writing_chapter",
                            status="retry_scheduled",
                            operation_id=self.telemetry.operation_id(),
                            attempt_no=attempt + 1,
                            previous_attempt=attempt,
                            delay_seconds=delay,
                            model_profile_id=model_profile_id,
                            model=model_name,
                            preferred_provider_kind=str(
                                llm_preferred_provider_kind or ""
                            ),
                            preferred_model=str(llm_preferred_model or ""),
                        ),
                    )
                    time.sleep(delay)
        if last_error is not None:
            preview_started_at = time.perf_counter()
            preview_max_attempts = 3 if saw_transient_error else 2
            preview_timeout_seconds = self.writer.single_call_timeout_seconds
            preview_started_event = self.telemetry.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED,
                scope="chapter",
                summary=f"第{chapter_number}章 writer preview fallback 已开始。",
                parent_event_id=last_failure_event_id,
                payload=audit_payload(
                    stage="chapter_preview_fallback",
                    status="started",
                    operation_id=self.telemetry.operation_id(),
                    source_error_class=last_error.__class__.__name__,
                    source_error_message=safe_error_summary(last_error),
                    source_attempt_no=last_failed_attempt,
                    max_attempts=preview_max_attempts,
                    timeout_seconds=preview_timeout_seconds,
                ),
            )
            try:
                preview_output = _call_compatible(
                    self.writer.write_preview_chapter,
                    context,
                    skill_layers=writer_skill_layers,
                    trace_stage_key="writer_preview_fallback",
                    timeout_seconds=preview_timeout_seconds,
                    max_attempts=preview_max_attempts,
                    retry_on_timeout=False,
                )
                preview_output.generation_meta.update(
                    {
                        "fallback_from_writer_error": True,
                        "writer_fallback_error": str(last_error),
                    }
                )
                fallback_summary = prompt_trace_success_summary(preview_output)
                self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer preview fallback 成功。",
                    parent_event_id=last_failure_event_id,
                    payload=audit_payload(
                        stage="chapter_preview_fallback",
                        status="succeeded",
                        operation_id=self.telemetry.operation_id(),
                        source_error_class=last_error.__class__.__name__,
                        source_error_message=safe_error_summary(last_error),
                        source_attempt_no=last_failed_attempt,
                        fallback_attempt_no=fallback_summary.get(
                            "successful_attempt_no", 0
                        ),
                        max_attempts=preview_max_attempts,
                        timeout_seconds=preview_timeout_seconds,
                        duration_ms=max(
                            0, int((time.perf_counter() - preview_started_at) * 1000)
                        ),
                        char_count=int(getattr(preview_output, "char_count", 0) or 0),
                        **fallback_summary,
                    ),
                )
                logger.warning(
                    "Writer preview fallback succeeded for chapter %d after writer failure: %s",
                    chapter_number,
                    last_error,
                )
                return preview_output
            except Exception as preview_exc:  # noqa: BLE001
                preview_duration_ms = max(
                    0, int((time.perf_counter() - preview_started_at) * 1000)
                )
                preview_attempts = safe_prompt_trace_attempts(
                    self.telemetry.drain_attempts(),
                    fallback_attempt_no=0,
                    exc=preview_exc,
                    duration_ms=preview_duration_ms,
                )
                preview_error_category = error_category_from_attempts(
                    preview_attempts, preview_exc
                )
                preview_failure_event = self.telemetry.record_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer preview fallback 失败。",
                    parent_event_id=str(
                        getattr(preview_started_event, "id", "")
                        or last_failure_event_id
                    ),
                    payload=event_error_payload(
                        preview_exc,
                        stage="chapter_preview_fallback",
                        operation_id=self.telemetry.operation_id(),
                        duration_ms=preview_duration_ms,
                        error_category=preview_error_category,
                        source_error_class=last_error.__class__.__name__,
                        source_error_message=safe_error_summary(last_error),
                        source_attempt_no=last_failed_attempt,
                        max_attempts=preview_max_attempts,
                        timeout_seconds=preview_timeout_seconds,
                        attempt_count=len(preview_attempts),
                        attempt_group_ids=attempt_group_ids(preview_attempts),
                    ),
                )
                self.telemetry.record_failure_trace(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    context=context,
                    stage_key=trace_stage_key,
                    template_id="writer:preview_failure",
                    source_event_id=str(getattr(preview_failure_event, "id", "") or ""),
                    exc=preview_exc,
                    duration_ms=preview_duration_ms,
                    attempts=preview_attempts,
                    skill_layers=writer_skill_layers,
                    fallback_stage="chapter_preview_fallback",
                )
                logger.warning(
                    "Writer preview fallback failed for chapter %d: %s",
                    chapter_number,
                    preview_exc,
                )
                last_error = preview_exc
        if self.policy.canon.hard_floor:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "writer-failed-without-draft",
                    "chapter_number": chapter_number,
                    "project_id": project_id,
                    "error": str(last_error) if last_error else "writer failed",
                },
            )
            if frozen_path:
                frozen_artifacts.append(frozen_path)
        if last_error is not None and saw_transient_error:
            raise TransientLLMChapterFailure(
                str(last_error), cause=last_error
            ) from last_error
        raise last_error or RuntimeError("writer failed")
