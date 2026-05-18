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
) -> ChapterReviewAnswers:
    messages = _messages(form=form, chapter_text=chapter_text, prior_canon_summary=prior_canon_summary)
    output_schema = ChapterReviewAnswers.model_json_schema()
    try:
        raw = _complete_json(
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
    try:
        return ChapterReviewAnswers.model_validate(raw)
    except ValidationError as exc:
        raise ChapterReviewFormSchemaInvalid(str(exc)) from exc


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
                "Answer the chapter review form as one valid JSON object matching the schema.\n\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)}"
            ),
        },
    ]


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    parsed = parse_llm_json(str(value or ""), error_prefix="ChapterReviewForm")
    if not isinstance(parsed, dict):
        raise ChapterReviewFormSchemaInvalid("LLM response was not a JSON object.")
    return parsed
