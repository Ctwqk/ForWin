from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from forwin.llm.compat import call_chat_compat
from forwin.utils.json_repair import parse_llm_json

from .errors import ChapterReviewFormSchemaInvalid, ChapterReviewFormUnavailable
from .form_schema import ChapterReviewAnswers, ChapterReviewForm


SYSTEM_PROMPT = (
    "You are a strict canon reviewer for a long-form Chinese web novel. "
    "Read the chapter and answer the form. Every binding answer requires an exact quote "
    "from the chapter text and an explicit subject_of_quote. Do not invent facts. "
    "If uncertain, set confidence below 0.5 and explain."
)


def call_form(
    *,
    form: ChapterReviewForm,
    chapter_text: str,
    prior_canon_summary: str,
    llm_client: object,
    max_tokens: int = 4000,
    timeout_seconds: float = 60.0,
    max_schema_retries: int = 1,
) -> ChapterReviewAnswers:
    base_messages = _messages(form=form, chapter_text=chapter_text, prior_canon_summary=prior_canon_summary)
    output_schema = ChapterReviewAnswers.model_json_schema()
    max_attempts = max(0, int(max_schema_retries)) + 1
    last_error = ""
    last_raw: dict[str, Any] = {}
    for attempt_index in range(max_attempts):
        messages = (
            base_messages
            if attempt_index == 0
            else _repair_messages(base_messages=base_messages, previous_raw=last_raw, validation_error=last_error)
        )
        try:
            raw_result = _complete_json(
                llm_client=llm_client,
                messages=messages,
                output_schema=output_schema,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
        except ChapterReviewFormUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChapterReviewFormUnavailable(str(exc)) from exc

        last_raw = raw_result
        try:
            raw = _normalize_answer_payload(raw_result, form=form)
            return ChapterReviewAnswers.model_validate(raw)
        except ChapterReviewFormSchemaInvalid as exc:
            last_error = str(exc)
        except ValidationError as exc:
            last_error = str(exc)

    raise ChapterReviewFormSchemaInvalid(last_error or "LLM response did not match ChapterReviewAnswers schema.")


def _complete_json(
    *,
    llm_client: object,
    messages: list[dict[str, str]],
    output_schema: dict[str, Any],
    max_tokens: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    complete_json = getattr(llm_client, "complete_json", None)
    if callable(complete_json):
        result = complete_json(
            messages=messages,
            output_schema=output_schema,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
        return _coerce_json_object(result)
    generate_json = getattr(llm_client, "generate_json", None)
    if callable(generate_json):
        return _coerce_json_object(
            generate_json(messages=messages, output_schema=output_schema, temperature=0.0, max_tokens=max_tokens)
        )
    chat = getattr(llm_client, "chat", None)
    if callable(chat):
        raw = call_chat_compat(
            llm_client,
            messages,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            response_format={"type": "json_object"},
            output_schema=output_schema,
            task_family="chapter_review_form",
            stage_key="chapter_review_form",
        )
        return _coerce_json_object(raw)
    raise ChapterReviewFormUnavailable("No compatible structured JSON LLM client is configured.")


def _messages(*, form: ChapterReviewForm, chapter_text: str, prior_canon_summary: str) -> list[dict[str, str]]:
    payload = {
        "form": form.model_dump(mode="json"),
        "prior_canon_summary": prior_canon_summary,
        "chapter_body": chapter_text,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Answer the chapter review form as one valid JSON object matching the schema. "
                "Return only the answer object; do not echo the input payload. "
                "Include project_id, chapter_number, and form_schema_version exactly as provided in form.\n\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)}"
            ),
        },
    ]


def _normalize_answer_payload(raw: dict[str, Any], *, form: ChapterReviewForm) -> dict[str, Any]:
    if isinstance(raw.get("answers"), dict):
        payload = dict(raw["answers"])
    else:
        payload = dict(raw)

    answer_keys = {
        "characters",
        "countdowns",
        "obligations",
        "open_signals",
        "new_observations",
        "final_chapter",
        "chapter_summary",
    }
    if not any(key in payload for key in answer_keys):
        raise ChapterReviewFormSchemaInvalid("LLM response did not contain chapter review answers.")

    payload["project_id"] = form.project_id
    payload["chapter_number"] = form.chapter_number
    payload["form_schema_version"] = form.form_schema_version
    return payload


def _repair_messages(
    *,
    base_messages: list[dict[str, str]],
    previous_raw: dict[str, Any],
    validation_error: str,
) -> list[dict[str, str]]:
    previous_json = json.dumps(previous_raw, ensure_ascii=False, sort_keys=True, indent=2)
    return [
        *base_messages,
        {
            "role": "user",
            "content": (
                "The previous JSON did not match the ChapterReviewAnswers schema. "
                "Return a corrected answer object only. Do not echo the input payload, and do not omit required nested fields.\n\n"
                f"Validation error:\n{_truncate_for_prompt(validation_error)}\n\n"
                f"Previous JSON:\n{_truncate_for_prompt(previous_json)}"
            ),
        },
    ]


def _truncate_for_prompt(value: str, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    parsed = parse_llm_json(str(value or ""), error_prefix="ChapterReviewForm")
    if not isinstance(parsed, dict):
        raise ChapterReviewFormSchemaInvalid("LLM response was not a JSON object.")
    return parsed
