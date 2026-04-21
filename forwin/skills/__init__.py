from __future__ import annotations

from pathlib import Path

from .models import SkillCapability, SkillLayer, SkillManifest, SkillSelection
from .prompt_layer import (
    SkillPromptLayerBuilder,
    inject_skill_layers,
    serialize_prompt_layers,
    summarize_selected_skills,
)
from .registry import SkillRegistry
from .router import SkillRouter


def build_skill_runtime_components(
    *,
    root: str | Path,
    enabled: bool = True,
    strictness: str = "normal",
    enabled_skill_groups: list[str] | tuple[str, ...] | None = None,
    disabled_skill_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[SkillRegistry, SkillRouter, SkillPromptLayerBuilder]:
    registry = SkillRegistry(root)
    router = SkillRouter(
        registry,
        enabled=enabled,
        enabled_skill_groups=enabled_skill_groups,
        disabled_skill_ids=disabled_skill_ids,
    )
    builder = SkillPromptLayerBuilder(strictness=strictness)
    return registry, router, builder


__all__ = [
    "SkillCapability",
    "SkillLayer",
    "SkillManifest",
    "SkillPromptLayerBuilder",
    "SkillRegistry",
    "SkillRouter",
    "SkillSelection",
    "build_skill_runtime_components",
    "inject_skill_layers",
    "serialize_prompt_layers",
    "summarize_selected_skills",
]
