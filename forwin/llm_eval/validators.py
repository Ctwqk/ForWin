from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .schemas import EvalValidationResult


REQUIRED_JSON_KEYS: dict[str, list[str]] = {
    "genesis_brief": ["title", "one_line", "audience"],
    "arc_plan": ["chapters"],
    "scene_breakdown": ["scenes"],
    "state_event_extraction": ["state_changes", "new_events"],
    "thread_time_extraction": ["thread_beats"],
    "lore_timeline_notes": ["lore_candidates", "timeline_hints", "writer_notes", "entity_mentions"],
    "review_json": ["verdict", "issues"],
    "comment_analysis": ["signals"],
    "npc_intents": ["intents"],
    "world_pressure": ["pressure_level", "pressure_summary"],
}

REQUIRED_TAGS: dict[str, list[str]] = {
    "writer_preview": ["<<FORWIN_BODY>>", "<<FORWIN_SUMMARY>>"],
    "scene_generation": ["<<FORWIN_BODY>>", "<<FORWIN_SUMMARY>>", "<<FORWIN_REWARD>>"],
}


def _strip_markdown_json(text: str) -> str:
    stripped = str(text or "").strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _stable_hash(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _loads_json(text: str) -> Any:
    return json.loads(_strip_markdown_json(text))


def validate_output(
    output: str,
    *,
    expected_output_kind: str,
    schema_name: str,
) -> EvalValidationResult:
    text = str(output or "")
    if expected_output_kind == "json":
        try:
            payload = _loads_json(text)
        except Exception as exc:  # noqa: BLE001
            return EvalValidationResult(
                parse_ok=False,
                schema_ok=False,
                output_chars=len(text),
                normalized_output_hash=_stable_hash(text),
                error_message=str(exc),
            )
        required = REQUIRED_JSON_KEYS.get(schema_name, [])
        missing = [
            key for key in required
            if not isinstance(payload, dict) or key not in payload
        ]
        return EvalValidationResult(
            parse_ok=True,
            schema_ok=not missing,
            required_keys_missing=missing,
            output_chars=len(text),
            normalized_output_hash=_stable_hash(json.dumps(payload, ensure_ascii=False)),
        )

    if expected_output_kind == "tagged_prose":
        required_tags = REQUIRED_TAGS.get(schema_name, [])
        missing = [tag for tag in required_tags if tag not in text]
        return EvalValidationResult(
            parse_ok=bool(text.strip()),
            schema_ok=bool(text.strip()) and not missing,
            required_keys_missing=missing,
            output_chars=len(text),
            normalized_output_hash=_stable_hash(text),
        )

    return EvalValidationResult(
        parse_ok=bool(text.strip()),
        schema_ok=bool(text.strip()),
        output_chars=len(text),
        normalized_output_hash=_stable_hash(text),
    )
