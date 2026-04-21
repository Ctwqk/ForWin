from __future__ import annotations


ALLOWED_SKILL_MODES = {"instruction_only"}
ALLOWED_SKILL_STRICTNESS = {"light", "normal", "strict"}


def ensure_skill_mode(mode: object) -> str:
    normalized = str(mode or "").strip() or "instruction_only"
    if normalized not in ALLOWED_SKILL_MODES:
        raise ValueError(f"Unsupported ForWin skill mode: {normalized}")
    return normalized


def normalize_skill_strictness(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_SKILL_STRICTNESS:
        return normalized
    return "normal"
