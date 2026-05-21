from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.api_schemas import ProjectAutomationPublishSettings, ProjectAutomationSettings
from forwin.long_run_policy import LongRunPolicy, normalize_long_run_policy


MAX_DAILY_PRODUCTION_QUOTA = 20


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_quota(value: Any, *, default: int = 0, minimum: int = 0) -> int:
    normalized = _as_int(value, default)
    return min(MAX_DAILY_PRODUCTION_QUOTA, max(minimum, normalized))


class ProductionQuota(BaseModel):
    plan: int = 0
    write: int = 1
    review: int = 0
    publish: int = 0


class ProductionPolicy(BaseModel):
    enabled: bool = False
    daily_start_time: str = "09:00"
    quota: ProductionQuota = Field(default_factory=ProductionQuota)
    stop_when_review_pending: bool = True
    auto_publish: bool = False
    max_active_generation_tasks: int = 1
    max_active_upload_tasks: int = 1
    publish_bindings: list[ProjectAutomationPublishSettings] = Field(default_factory=list)
    long_run_policy: LongRunPolicy = Field(default_factory=LongRunPolicy)


def _automation_publish_bindings(
    automation: ProjectAutomationSettings,
) -> list[ProjectAutomationPublishSettings]:
    bindings: list[ProjectAutomationPublishSettings] = []
    seen_platforms: set[str] = set()
    for binding in list(getattr(automation, "publish_bindings", []) or []):
        platform = str(binding.platform or "").strip()
        if not platform or platform in seen_platforms:
            continue
        bindings.append(binding)
        seen_platforms.add(platform)
    publish = getattr(automation, "publish", None)
    platform = str(getattr(publish, "platform", "") or "").strip()
    if platform and platform not in seen_platforms:
        bindings.insert(0, publish)
    return bindings[:2]


def policy_from_automation(automation: ProjectAutomationSettings) -> ProductionPolicy:
    default_write_quota = _clamp_quota(
        getattr(automation, "daily_chapter_quota", 1),
        default=1,
        minimum=1,
    )
    daily_write_raw = _as_int(getattr(automation, "daily_write_quota", 0), 0)
    write_quota = (
        default_write_quota
        if daily_write_raw <= 0
        else _clamp_quota(daily_write_raw, default=default_write_quota, minimum=1)
    )
    auto_publish = bool(getattr(automation, "auto_publish", False))
    publish_raw = _as_int(getattr(automation, "daily_publish_quota", 0), 0)
    publish_quota = (
        1
        if auto_publish and publish_raw <= 0
        else _clamp_quota(publish_raw, default=0, minimum=0)
    )
    return ProductionPolicy(
        enabled=bool(getattr(automation, "enabled", False)),
        daily_start_time=str(getattr(automation, "daily_start_time", "") or "09:00"),
        quota=ProductionQuota(
            plan=_clamp_quota(getattr(automation, "daily_plan_quota", 0), default=0, minimum=0),
            write=write_quota,
            review=_clamp_quota(getattr(automation, "daily_review_quota", 0), default=0, minimum=0),
            publish=publish_quota,
        ),
        stop_when_review_pending=bool(getattr(automation, "stop_when_review_pending", True)),
        auto_publish=auto_publish,
        max_active_generation_tasks=_clamp_quota(
            getattr(automation, "max_active_generation_tasks", 1),
            default=1,
            minimum=1,
        ),
        max_active_upload_tasks=_clamp_quota(
            getattr(automation, "max_active_upload_tasks", 1),
            default=1,
            minimum=1,
        ),
        publish_bindings=_automation_publish_bindings(automation),
        long_run_policy=normalize_long_run_policy(
            getattr(automation, "long_run_policy", None)
        ),
    )
