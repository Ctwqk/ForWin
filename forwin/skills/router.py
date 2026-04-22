from __future__ import annotations

from .models import SkillSelection
from .registry import SkillRegistry


class SkillRouter:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        enabled: bool = True,
        enabled_skill_groups: list[str] | tuple[str, ...] | None = None,
        disabled_skill_ids: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.registry = registry
        self.enabled = bool(enabled)
        self.enabled_skill_groups = {
            str(item).strip()
            for item in (enabled_skill_groups or [])
            if str(item).strip()
        }
        self.disabled_skill_ids = {
            str(item).strip()
            for item in (disabled_skill_ids or [])
            if str(item).strip()
        }

    def select(
        self,
        *,
        scope: str,
        stage_key: str = "",
        task_family: str = "",
    ) -> list[SkillSelection]:
        if not self.enabled:
            return []
        resolved_scope = str(scope or "").strip()
        resolved_stage_key = str(stage_key or "").strip()
        resolved_task_family = str(task_family or "").strip()
        selections: list[SkillSelection] = []
        for manifest in self.registry.list_manifests():
            if manifest.name in self.disabled_skill_ids:
                continue
            if self.enabled_skill_groups and manifest.group not in self.enabled_skill_groups:
                continue
            if manifest.forwin_scope != resolved_scope:
                continue
            if manifest.stage_keys and resolved_stage_key not in manifest.stage_keys:
                continue
            if manifest.task_families and resolved_task_family not in manifest.task_families:
                continue
            reasons = [f"scope={resolved_scope}"]
            if resolved_stage_key:
                reasons.append(f"stage_key={resolved_stage_key}")
            if resolved_task_family:
                reasons.append(f"task_family={resolved_task_family}")
            selections.append(
                SkillSelection(
                    manifest=manifest,
                    activation_reason=", ".join(reasons),
                )
            )
        return selections
