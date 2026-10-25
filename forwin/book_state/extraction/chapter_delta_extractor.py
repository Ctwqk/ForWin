from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.story_clock import (
    StoryCalendar, StoryClockError, UnknownTimeUnit, duration_to_minutes,
)
from forwin.observability.llm_trace import (
    RAW_REQUEST_KEY, RAW_RESPONSE_KEY, mark_latest_attempt_parse_failure,
    mark_latest_attempt_workflow,
    served_model_profile,
)
from forwin.models.project import ChapterPlan
from forwin.skills import serialize_prompt_layers
from forwin.utils import parse_llm_json
from forwin.writer.prompt_core.chapter_delta import (
    build_chapter_delta_messages, build_plan_fulfillment_feedback, build_unknown_ref_feedback,
)

from .chapter_delta_schema import ChapterDeltaExtraction, ExtractionIssue
from .chapter_input import ChapterExtractionInput, build_chapter_extraction_input
from .evidence import dropped_ratio, validate_evidence
from .ref_check import unknown_refs

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from forwin.protocol.writer import WriterOutput


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


def chapter_delta_is_current(writer_output: WriterOutput, *, plan_fingerprint: str) -> bool:
    """The output carries an extraction of its current body and plan."""
    current = writer_output.chapter_delta
    return (current is not None and current.body_sha256 == _sha256(writer_output.body)
            and current.plan_fingerprint == plan_fingerprint)


def ensure_chapter_delta(
    session: Session,
    *,
    llm_client,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
) -> tuple[WriterOutput, ChapterDeltaExtractionResult | None]:
    """The output with an extraction of its current body; None when the attached one still matches.

    A changed body or plan is re-extracted. Without a usable LLM client the output comes back
    with no chapter_delta and an error issue, so a stale extraction never survives a change.
    """
    from forwin.book_state.plan_fingerprint import chapter_plan_fingerprint

    plan = session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
    ))
    fingerprint = chapter_plan_fingerprint(plan)
    if chapter_delta_is_current(writer_output, plan_fingerprint=fingerprint):
        return writer_output, None
    if not callable(getattr(llm_client, "chat", None)):
        result = ChapterDeltaExtractionResult(
            extraction=None,
            issues=(ExtractionIssue(
                severity="error", code="extraction_unavailable", path="",
                message="No LLM client is available to extract the chapter delta.",
            ),),
            attempts=0,
            prompt_trace={},
        )
    else:
        result = ChapterDeltaExtractor(llm_client).extract(
            extraction_input=build_chapter_extraction_input(
                session, project_id=project_id, chapter_number=chapter_number,
                scene_location_hints=writer_output.scene_location_hints,
            ),
            chapter_title=writer_output.title,
            chapter_body=writer_output.body,
            chapter_summary=writer_output.end_of_chapter_summary,
            calendar=StoryCalendarRepository(session).get(project_id),
            lexicon=BookLexiconRepository(session).get(project_id),
            plan_fingerprint=fingerprint,
        )
    generation_meta = {
        **(writer_output.generation_meta or {}),
        "chapter_delta_issues": [issue.model_dump(mode="json") for issue in result.issues],
    }
    return writer_output.model_copy(
        update={"chapter_delta": result.extraction, "generation_meta": generation_meta}
    ), result


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ChapterDeltaExtractor:
    def __init__(self, llm_client, *, max_attempts: int = 3, max_tokens: int = 8000):
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max_tokens
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
        plan_fingerprint: str = "",
    ) -> ChapterDeltaExtractionResult:
        feedback: list[str] = []
        attempts: list[dict[str, Any]] = []
        business_retries: list[dict[str, Any]] = []
        evidence_retried = False
        elapsed_retried = False
        fulfillment_retried = False
        refs_retried = False
        required_targets = {
            target.target_id for target in extraction_input.fulfillment_targets
            if target.target_id.startswith(("goal:", "obligation:", "bind:"))
        }
        # Keep the best validated candidate if a later retry fails, names more unknown refs
        # or omits more targets.
        best_key: tuple[int, int] | None = None
        extraction = None
        issues: list[ExtractionIssue] = []
        body_hash = _sha256(chapter_body)
        for attempt_no in range(1, self.max_attempts + 1):
            messages = build_chapter_delta_messages(
                extraction_input=extraction_input, chapter_title=chapter_title,
                chapter_body=chapter_body, chapter_summary=chapter_summary, feedback=feedback,
            )
            feedback = []
            raw = ""
            call_error: Exception | None = None
            failure_reason = ""
            try:
                raw = self._call_chat(messages)
                try:
                    parsed = ChapterDeltaExtraction.model_validate(parse_llm_json(
                        raw, error_prefix="ChapterDeltaExtractor JSON parser",
                    ))
                except Exception as exc:  # noqa: BLE001
                    feedback = [str(exc)]
                    mark_latest_attempt_parse_failure(
                        self.llm_client, parser_name="ChapterDeltaExtractor JSON parser",
                        stage_key=_STAGE_KEY, schema_name="ChapterDeltaExtraction",
                        raw_output=raw, error=exc,
                    )
                    raise
            except Exception as exc:  # noqa: BLE001
                call_error = exc
                failure_reason = "\n".join(feedback) if feedback else type(exc).__name__
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
                        if feedback:
                            event.setdefault("error_message", failure_reason)
                        else:
                            event["error_message"] = failure_reason
                attempts.extend(current_attempts)

            if call_error is None:
                parsed.body_sha256 = body_hash
                parsed.plan_fingerprint = plan_fingerprint
                candidate, candidate_issues = validate_evidence(parsed, chapter_body)
                absent_targets = required_targets - {item.target_id for item in candidate.plan_fulfillment}
                if absent_targets and not fulfillment_retried and attempt_no < self.max_attempts:
                    fulfillment_retried = True
                    feedback.append(build_plan_fulfillment_feedback(sorted(absent_targets)))
                unknown = unknown_refs(candidate, extraction_input)
                if unknown and not refs_retried and attempt_no < self.max_attempts:
                    refs_retried = True
                    feedback.extend(build_unknown_ref_feedback(unknown))
                if dropped_ratio(candidate_issues, parsed) > 0.5 and not evidence_retried and attempt_no < self.max_attempts:
                    evidence_retried = True
                    feedback.extend(f"{issue.code} at {issue.path}: {issue.message}" for issue in candidate_issues)
                try:
                    elapsed_minutes(candidate, calendar, lexicon)
                except (UnknownTimeUnit, StoryClockError) as exc:
                    if not elapsed_retried and attempt_no < self.max_attempts:
                        elapsed_retried = True
                        feedback.append(f"elapsed_unknown at time.elapsed: {exc}")
                    candidate.time.elapsed = None
                    candidate_issues.append(ExtractionIssue(
                        severity="warning", code="elapsed_unknown", path="time.elapsed", message=str(exc),
                    ))
                # An unknown ref fails translation, so it outweighs an absent target.
                key = (len(unknown), len(absent_targets))
                if best_key is None or key <= best_key:
                    extraction, issues = candidate, candidate_issues
                    best_key = key

            if (feedback or call_error is not None) and attempt_no < self.max_attempts:
                business_retries.append({
                    "stage": _STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "ExtractionValidation",
                    "reason": ("\n".join(feedback) or failure_reason)[:500], "retry_kind": "business_validation",
                })
                continue
            break

        if extraction is None:
            issues.append(ExtractionIssue(
                severity="error", code="extraction_failed", path="",
                message=f"Chapter delta extraction failed after {attempt_no} attempts: {failure_reason}",
            ))
        prompt_trace = self._build_prompt_trace(
            messages=messages, extraction_input=extraction_input, chapter_title=chapter_title,
            body_hash=body_hash, attempts=attempts, business_retries=business_retries,
            extraction=extraction, issues=issues, plan_fingerprint=plan_fingerprint,
        )
        return ChapterDeltaExtractionResult(extraction, tuple(issues), attempt_no, prompt_trace)

    def _call_chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.2, "max_tokens": self.max_tokens,
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
        issues: list[ExtractionIssue], plan_fingerprint: str,
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
                "plan_fingerprint": plan_fingerprint,
                "stage_key": _STAGE_KEY, "selected_skills": [],
            },
            "model_profile": served_model_profile(attempts, llm_client=self.llm_client),
            "attempts": attempts,
            "output_summary": {
                "extraction_ok": extraction is not None, "issues": [issue.model_dump() for issue in issues],
                "skill_summary": [], "business_retry_events": business_retries,
            },
        }
