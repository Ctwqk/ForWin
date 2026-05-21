from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CountdownRuleProfile(BaseModel):
    key: str
    label: str = ""
    aliases: list[str] = Field(default_factory=list)
    local_window_aliases: list[str] = Field(default_factory=list)
    forbidden_stale_phrases: list[str] = Field(default_factory=list)
    resolution_phrases: list[str] = Field(default_factory=list)
    closure_requires_evidence: bool = True
    monotonic: bool = True


class CanonGlossary(BaseModel):
    countdowns: dict[str, CountdownRuleProfile] = Field(default_factory=dict)
    mechanism_terms: list[str] = Field(default_factory=list)
    final_crisis_terms: list[str] = Field(default_factory=list)


def canon_glossary_from_payload(payload: object) -> CanonGlossary:
    if isinstance(payload, CanonGlossary):
        return payload
    if isinstance(payload, dict):
        try:
            return CanonGlossary.model_validate(payload)
        except Exception:  # noqa: BLE001
            return CanonGlossary()
    return CanonGlossary()


def countdown_profiles_from_quality_context(
    quality: dict[str, Any],
) -> dict[str, CountdownRuleProfile]:
    profiles: dict[str, CountdownRuleProfile] = {}
    glossary = canon_glossary_from_payload(quality.get("canon_glossary", {}))
    profiles.update(glossary.countdowns)
    raw_profiles = quality.get("countdown_rule_profiles", {})
    if isinstance(raw_profiles, dict):
        for key, raw in raw_profiles.items():
            if isinstance(raw, CountdownRuleProfile):
                profile = raw
            elif isinstance(raw, dict):
                profile = CountdownRuleProfile.model_validate({"key": key, **raw})
            else:
                continue
            profiles[str(profile.key or key)] = profile
    return profiles


def display_countdown_label(
    *,
    key: str,
    label: str = "",
    profiles: dict[str, CountdownRuleProfile] | None = None,
) -> str:
    clean_label = str(label or "").strip()
    if clean_label:
        return clean_label
    clean_key = str(key or "").strip()
    profile = (profiles or {}).get(clean_key)
    if profile is not None and profile.label:
        return profile.label
    return f"{clean_key} 倒计时" if clean_key else "倒计时"
