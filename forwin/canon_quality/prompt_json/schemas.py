from __future__ import annotations

from typing import Literal


PromptJsonMode = Literal["deterministic", "hybrid", "prompt_json", "shadow"]

VALID_PROMPT_JSON_MODES = {"deterministic", "hybrid", "prompt_json", "shadow"}
VALID_VERDICTS = {"pass", "warn", "fail", "uncertain"}
VALID_SEVERITIES = {"info", "minor", "major", "critical"}
VALID_EVIDENCE_SOURCES = {
    "writer_output",
    "canon",
    "plan",
    "ledger",
    "prior_state",
    "derived",
    "identity_registry",
    "future_plan",
    "old_plan",
    "proposed_patch",
    "locked_constraints",
    "band_definition",
    "chapter_plan",
    "obligation_ledger",
}

COMMON_SYSTEM_PROMPT = """You are a strict but conservative narrative continuity analyzer for a serialized fiction system.

Your task is to identify only evidence-backed continuity, planning, canon, or obligation problems in the provided materials.

Rules:
- Use only the provided input.
- Do not infer hidden facts unless the input directly supports them.
- If the evidence is ambiguous, return "uncertain" or "warn", not "fail".
- A blocking issue must include at least one direct quote from the input.
- Do not mark stylistic differences as canon violations.
- Do not mark an issue as critical unless it would cause canon admission, future planning, or reader-facing continuity to become materially wrong.
- heuristic_hints are suggestions only. They may be wrong. Do not trust them unless supported by writer_output and canon_context.
- Output valid JSON only.
"""


def normalize_prompt_json_mode(value: str | None, *, default: PromptJsonMode = "hybrid") -> PromptJsonMode:
    normalized = str(value or default).strip().lower()
    if normalized in VALID_PROMPT_JSON_MODES:
        return normalized  # type: ignore[return-value]
    return default


def common_output_schema(*, analyzer: str, extra_required: list[str] | None = None) -> dict[str, object]:
    required = [
        "analyzer",
        "version",
        "verdict",
        "blocking",
        "confidence",
        "summary",
        "issues",
        "uncertainties",
        "metadata",
    ]
    for field in extra_required or []:
        if field not in required:
            required.append(field)
    return {
        "type": "object",
        "analyzer": analyzer,
        "required": required,
        "properties": {
            "analyzer": {"type": "string"},
            "version": {"type": "string"},
            "verdict": {"enum": sorted(VALID_VERDICTS)},
            "blocking": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "summary": {"type": "string"},
            "issues": {"type": "array"},
            "accepted_facts": {"type": "array"},
            "uncertainties": {"type": "array"},
            "metadata": {"type": "object"},
        },
    }
