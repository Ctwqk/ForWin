from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from typing import Any

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.story_clock import (
    StoryCalendar, StoryClockError, UnknownTimeUnit, duration_to_minutes,
)
from forwin.observability.llm_trace import (
    RAW_REQUEST_KEY, RAW_RESPONSE_KEY, mark_latest_attempt_parse_failure,
    mark_latest_attempt_workflow,
)
from forwin.skills import serialize_prompt_layers
from forwin.utils import parse_llm_json
from forwin.writer.prompt_core.chapter_delta import build_chapter_delta_messages

from .chapter_delta_schema import ChapterDeltaExtraction, ExtractionIssue
from .chapter_input import ChapterExtractionInput
from .evidence import dropped_ratio, validate_evidence


_STAGE_KEY = "chapter_delta_extraction"


@dataclass(frozen=True)
class ChapterDeltaExtractionResult:
    extraction: ChapterDeltaExtraction | None
    issues: tuple[ExtractionIssue, ...]
    attempts: int
    prompt_trace: dict[str, Any]


def elapsed_minutes(
    extraction: ChapterDeltaExtraction,
    calendar: StoryCalendar | None,
    lexicon: BookLexicon,
) -> int | None:
    elapsed = extraction.time.elapsed
    if elapsed is None:
        return None
    return duration_to_minutes(
        calendar if calendar is not None else StoryCalendar("", 0, ()),
        lexicon, amount=elapsed.amount, unit_name=elapsed.unit,
    )


class ChapterDeltaExtractor:
    def __init__(self, llm_client, *, max_attempts: int = 3):
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        self._chat_parameters = inspect.signature(llm_client.chat).parameters

    def extract(
        self,
        *,
        extraction_input: ChapterExtractionInput,
        chapter_title: str,
        chapter_body: str,
        chapter_summary: str,
        calendar: StoryCalendar | None,
        lexicon: BookLexicon,
    ) -> ChapterDeltaExtractionResult:
        feedback: list[str] = []
        attempts: list[dict[str, Any]] = []
        business_retries: list[dict[str, Any]] = []
        evidence_retried = False
        elapsed_retried = False
        extraction = None
        issues: list[ExtractionIssue] = []
        body_hash = hashlib.sha256(chapter_body.encode("utf-8")).hexdigest()
        for attempt_no in range(1, self.max_attempts + 1):
            messages = build_chapter_delta_messages(
                extraction_input=extraction_input, chapter_title=chapter_title,
                chapter_body=chapter_body, chapter_summary=chapter_summary, feedback=feedback,
            )
            feedback = []
            raw = ""
            call_error: Exception | None = None
            try:
                raw = self._call_chat(messages)
                try:
                    parsed = ChapterDeltaExtraction.model_validate(parse_llm_json(
                        raw, error_prefix="ChapterDeltaExtractor JSON parser",
                    ))
                except Exception as exc:  # noqa: BLE001
                    mark_latest_attempt_parse_failure(
                        self.llm_client, parser_name="ChapterDeltaExtractor JSON parser",
                        stage_key=_STAGE_KEY, schema_name="ChapterDeltaExtraction",
                        raw_output=raw, error=exc,
                    )
                    raise
            except Exception as exc:  # noqa: BLE001
                call_error = exc
                extraction = None
                issues = []
                feedback = [str(exc)]
            finally:
                mark_latest_attempt_workflow(self.llm_client, attempt_no=attempt_no, stage_key=_STAGE_KEY)
                drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
                drained = drain() if callable(drain) else []
                current_attempts = drained if isinstance(drained, list) and drained else [{
                    "attempt_no": 1, "stage_key": _STAGE_KEY, "task_family": "writer",
                }]
                for event in current_attempts:
                    event.setdefault("workflow_attempt_no", attempt_no)
                    event.setdefault("workflow_retry", attempt_no > 1)
                    event.setdefault(RAW_REQUEST_KEY, {"messages": messages})
                    event.setdefault(RAW_RESPONSE_KEY, raw)
                    if call_error is not None:
                        event.setdefault("error_class", type(call_error).__name__)
                        event.setdefault("error_message", str(call_error))
                attempts.extend(current_attempts)

            if call_error is None:
                parsed.body_sha256 = body_hash
                extraction, issues = validate_evidence(parsed, chapter_body)
                if dropped_ratio(issues, parsed) > 0.5 and not evidence_retried and attempt_no < self.max_attempts:
                    evidence_retried = True
                    feedback.extend(f"{issue.code} at {issue.path}: {issue.message}" for issue in issues)
                try:
                    elapsed_minutes(extraction, calendar, lexicon)
                except (UnknownTimeUnit, StoryClockError) as exc:
                    if not elapsed_retried and attempt_no < self.max_attempts:
                        elapsed_retried = True
                        feedback.append(f"elapsed_unknown at time.elapsed: {exc}")
                    else:
                        extraction.time.elapsed = None
                        issues.append(ExtractionIssue(
                            severity="warning", code="elapsed_unknown", path="time.elapsed", message=str(exc),
                        ))

            if feedback and attempt_no < self.max_attempts:
                business_retries.append({
                    "stage": _STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "ExtractionValidation",
                    "reason": "\n".join(feedback)[:500], "retry_kind": "business_validation",
                })
                continue
            break

        if extraction is None:
            issues.append(ExtractionIssue(
                severity="error", code="extraction_failed", path="",
                message=f"Chapter delta extraction failed after {attempt_no} attempts: {'; '.join(feedback)}",
            ))
        prompt_trace = self._build_prompt_trace(
            messages=messages, extraction_input=extraction_input, chapter_title=chapter_title,
            body_hash=body_hash, attempts=attempts, business_retries=business_retries,
            extraction=extraction, issues=issues,
        )
        return ChapterDeltaExtractionResult(extraction, tuple(issues), attempt_no, prompt_trace)

    def _call_chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.2, "max_tokens": 8000,
            "response_format": {"type": "json_object"}, "task_family": "writer",
            "stage_key": _STAGE_KEY, "output_schema": ChapterDeltaExtraction.model_json_schema(),
        }
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in self._chat_parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in self._chat_parameters}
        return self.llm_client.chat(messages, **kwargs)

    def _build_prompt_trace(
        self, *, messages: list[dict], extraction_input: ChapterExtractionInput,
        chapter_title: str, body_hash: str, attempts: list[dict[str, Any]],
        business_retries: list[dict[str, Any]], extraction: ChapterDeltaExtraction | None,
        issues: list[ExtractionIssue],
    ) -> dict[str, Any]:
        last_call_result = getattr(self.llm_client, "last_call_result", None)
        trace = getattr(last_call_result, "trace", {}) or {}
        layers = serialize_prompt_layers(messages, [])
        return {
            "trace_scope": "writer", "stage_key": _STAGE_KEY,
            "backend": str(trace.get("backend") or getattr(last_call_result, "backend", "") or ""),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False)),
            "template_id": "extraction:chapter_delta", "template_version": "v1",
            "effective_system_prompt": "\n\n".join(str(item["content"]).strip() for item in layers if item["role"] == "system"),
            "prompt_layers": layers,
            "input_snapshot": {
                "project_id": extraction_input.project_id, "chapter_number": extraction_input.chapter_number,
                "chapter_title": chapter_title, "body_sha256": body_hash,
                "stage_key": _STAGE_KEY, "selected_skills": [],
            },
            "model_profile": {key: getattr(self.llm_client, key, "") for key in (
                "profile_id", "profile_name", "model", "base_url",
            )},
            "attempts": attempts,
            "output_summary": {
                "extraction_ok": extraction is not None, "issues": [issue.model_dump() for issue in issues],
                "skill_summary": [], "business_retry_events": business_retries,
            },
        }
