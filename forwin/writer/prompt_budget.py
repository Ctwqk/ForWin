from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_prompt_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        normalized.append(
            {
                "role": str(message.get("role", "") or ""),
                "content": str(message.get("content", "") or ""),
            }
        )
    return normalized


def prompt_message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(item["role"]) + len(item["content"])
        for item in normalize_prompt_messages(messages)
    )


def prompt_revision_hash(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        normalize_prompt_messages(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def prompt_budget_warning(
    messages: list[dict[str, Any]],
    *,
    max_chars: int,
) -> dict[str, object]:
    char_count = prompt_message_chars(messages)
    budget = max(0, int(max_chars or 0))
    return {
        "char_count": char_count,
        "max_chars": budget,
        "over_budget": bool(budget and char_count > budget),
    }
