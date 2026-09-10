from __future__ import annotations

from typing import Any

from forwin.canon_names import is_plausible_person_name
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput


def apply_canon_name_drift_autofix(
    writer_output: WriterOutput,
    review: ReviewVerdict,
) -> WriterOutput | None:
    replacements: dict[str, str] = {}
    for issue in review.issues:
        if str(issue.rule_name or "") != "canon_name_drift":
            continue
        if str(issue.severity or "") != "error":
            continue
        entity_names = list(issue.entity_names or [])
        if len(entity_names) < 2:
            continue
        observed = str(entity_names[0] or "").strip()
        canonical = str(entity_names[1] or "").strip()
        if not observed or not canonical or observed == canonical:
            continue
        if observed.startswith(canonical):
            continue
        if not is_plausible_person_name(observed) or not is_plausible_person_name(
            canonical
        ):
            continue
        replacements[observed] = canonical

    if not replacements:
        return None

    payload = replace_canon_name_strings(
        writer_output.model_dump(mode="python"),
        replacements,
    )
    payload["char_count"] = len(str(payload.get("body") or ""))
    generation_meta = dict(payload.get("generation_meta") or {})
    previous_autofix = generation_meta.get("canon_name_autofix")
    if isinstance(previous_autofix, dict):
        autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
        autofix_meta.update(replacements)
    else:
        autofix_meta = replacements
    generation_meta["canon_name_autofix"] = autofix_meta
    payload["generation_meta"] = generation_meta
    return WriterOutput.model_validate(payload)


def replace_canon_name_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for observed, canonical in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            result = result.replace(observed, canonical)
        return result
    if isinstance(value, list):
        return [replace_canon_name_strings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_canon_name_strings(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            (
                replace_canon_name_strings(key, replacements)
                if isinstance(key, str)
                else key
            ): replace_canon_name_strings(item, replacements)
            for key, item in value.items()
        }
    return value
