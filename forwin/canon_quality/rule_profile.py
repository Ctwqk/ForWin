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


LEGACY_CURRENT_BOOK_COUNTDOWN_PROFILES: dict[str, CountdownRuleProfile] = {
    "memory_reset": CountdownRuleProfile(
        key="memory_reset",
        label="记忆重置周期",
        aliases=["记忆重置", "重置周期", "记忆熔铸", "熔铸倒计时"],
        local_window_aliases=["终端审计窗口", "档案清理窗口", "核心层授权窗口"],
        forbidden_stale_phrases=["五天", "七天", "三十多天", "三小时", "四十八小时", "两天"],
        resolution_phrases=[
            "记忆重置系统失效",
            "记忆重置停止",
            "记忆重置已取消",
            "记忆重置被阻止",
            "不再有记忆重置",
        ],
    ),
    "archive_cleanup": CountdownRuleProfile(
        key="archive_cleanup",
        label="终端审计/授权窗口",
        aliases=["档案清理", "档案抹除", "授权窗口", "审计窗口"],
        forbidden_stale_phrases=["四小时", "五小时", "一天"],
    ),
    "terminal_audit_window": CountdownRuleProfile(
        key="terminal_audit_window",
        label="终端审计窗口",
        aliases=["终端审计", "终端审计窗口", "终端审计倒计时"],
        forbidden_stale_phrases=["10 分钟", "三小时", "一天"],
    ),
    "core_access_window": CountdownRuleProfile(
        key="core_access_window",
        label="核心层授权窗口",
        aliases=["核心层入口", "核心层授权窗口", "入口关闭倒计时"],
    ),
    "public_countdown": CountdownRuleProfile(
        key="public_countdown",
        label="公开倒计时",
        aliases=["公开数据", "公开窗口", "对外数据"],
    ),
    "main": CountdownRuleProfile(key="main", label="主线倒计时"),
}

LEGACY_CURRENT_BOOK_GLOSSARY = CanonGlossary(
    countdowns=LEGACY_CURRENT_BOOK_COUNTDOWN_PROFILES,
    mechanism_terms=["记忆重置", "终端审计", "核心层", "档案清理", "记忆熔铸", "熔铸协议"],
    final_crisis_terms=["主线危机", "倒计时", "真相", "系统"],
)


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
    *,
    use_legacy_fallback: bool = False,
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
    if use_legacy_fallback:
        for key, profile in LEGACY_CURRENT_BOOK_COUNTDOWN_PROFILES.items():
            profiles.setdefault(key, profile)
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
