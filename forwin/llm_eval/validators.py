from __future__ import annotations

import hashlib

from forwin.utils.json_repair import parse_llm_json

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


def _stable_hash(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def validate_output(
    output: str,
    *,
    expected_output_kind: str,
    schema_name: str,
) -> EvalValidationResult:
    text = str(output or "")
    if expected_output_kind == "json":
        try:
            payload = parse_llm_json(text, error_prefix="LLM eval JSON parser")
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
            normalized_output_hash=_stable_hash(str(payload)),
        )

    if expected_output_kind == "tagged_prose":
        if schema_name == "writer_preview":
            from forwin.writer.chapter_writer import ChapterWriter

            parsed = ChapterWriter._parse_preview_text(text, fallback_title="")
            body = str(parsed.get("body") or "").strip()
            return EvalValidationResult(
                parse_ok=bool(text.strip()),
                schema_ok=bool(body),
                required_keys_missing=[] if body else ["body"],
                output_chars=len(text),
                normalized_output_hash=_stable_hash(text),
            )
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
