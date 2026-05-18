from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .replay import ReplayTokenUsage, load_accepted_draft_ref


class CostEstimate(BaseModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_usd: float = 0.0
    chapters: dict[str, dict[str, float | int]] = Field(default_factory=dict)


class CostCapDecision(BaseModel):
    abort: bool = False
    reason: str = ""
    projected_total_usd: float = 0.0


def estimate_tokens_for_text(text: str) -> int:
    value = str(text or "")
    chars = len(value)
    non_ascii = sum(1 for char in value if ord(char) > 127)
    ascii_chars = chars - non_ascii
    return max(1, int(non_ascii * 0.5 + ascii_chars * 0.25))


def estimate_chapter_cost(
    *,
    chapter_number: int,
    body: str,
    input_price_per_million: float = 0.0,
    output_price_per_million: float = 0.0,
) -> CostEstimate:
    input_tokens = estimate_tokens_for_text(body) + 3500
    output_tokens = 3000
    total_usd = (input_tokens / 1_000_000 * input_price_per_million) + (
        output_tokens / 1_000_000 * output_price_per_million
    )
    return CostEstimate(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_usd=total_usd,
        chapters={
            str(chapter_number): {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usd": total_usd,
            }
        },
    )


def estimate_run(
    *,
    session_factory,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
    input_price_per_million: float = 0.0,
    output_price_per_million: float = 0.0,
) -> CostEstimate:
    estimate = CostEstimate()
    with session_factory() as session:
        for chapter_number in range(int(from_chapter), int(to_chapter) + 1):
            accepted = load_accepted_draft_ref(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
            )
            chapter = estimate_chapter_cost(
                chapter_number=chapter_number,
                body=accepted.body,
                input_price_per_million=input_price_per_million,
                output_price_per_million=output_price_per_million,
            )
            estimate.total_input_tokens += chapter.total_input_tokens
            estimate.total_output_tokens += chapter.total_output_tokens
            estimate.total_usd += chapter.total_usd
            estimate.chapters.update(chapter.chapters)
    return estimate


def usage_from_llm_client(llm_client: object) -> ReplayTokenUsage:
    attempts = list(getattr(llm_client, "llm_attempt_events", []) or [])
    successes = [item for item in attempts if str(item.get("status", "")).lower() == "succeeded"]
    if not successes:
        return ReplayTokenUsage(estimated=True)
    last = successes[-1]
    raw_input_tokens = last.get("input_tokens")
    raw_output_tokens = last.get("output_tokens")
    input_tokens = raw_input_tokens
    output_tokens = raw_output_tokens
    if input_tokens is None:
        input_tokens = _estimate_attempt_tokens(
            last,
            text_key="input_text",
            raw_key="_raw_request_payload",
            chars_key="input_chars",
        )
    if output_tokens is None:
        output_tokens = _estimate_attempt_tokens(
            last,
            text_key="output_text",
            raw_key="_raw_response_text",
            chars_key="output_chars",
        )
    return ReplayTokenUsage(
        input_tokens=max(0, int(input_tokens or 0)),
        output_tokens=max(0, int(output_tokens or 0)),
        estimated=raw_input_tokens is None or raw_output_tokens is None,
    )


def _estimate_attempt_tokens(
    attempt: dict,
    *,
    text_key: str,
    raw_key: str,
    chars_key: str,
) -> int:
    text = str(attempt.get(text_key) or "")
    if text:
        return estimate_tokens_for_text(text)
    raw = attempt.get(raw_key)
    if raw:
        raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)
        return estimate_tokens_for_text(raw_text)
    return int(float(attempt.get(chars_key) or 0) * 0.5)


def should_abort_for_cost_cap(
    *,
    current_cost: CostEstimate,
    next_chapter_estimate: CostEstimate,
    cap_usd: float | None,
) -> CostCapDecision:
    projected = current_cost.total_usd + next_chapter_estimate.total_usd
    if cap_usd is None:
        return CostCapDecision(abort=False, projected_total_usd=projected)
    abort = projected > float(cap_usd)
    return CostCapDecision(
        abort=abort,
        reason="cost_cap" if abort else "",
        projected_total_usd=projected,
    )
