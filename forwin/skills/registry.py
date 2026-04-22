from __future__ import annotations

from pathlib import Path

from .loader import load_skill_manifest
from .models import SkillManifest


class SkillRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._cache: list[SkillManifest] | None = None

    def reload(self) -> list[SkillManifest]:
        manifests: list[SkillManifest] = []
        if self.root.exists():
            for path in sorted(self.root.rglob("SKILL.md")):
                manifests.append(load_skill_manifest(path, root=self.root))
        self._cache = manifests
        return list(manifests)

    def list_manifests(self) -> list[SkillManifest]:
        if self._cache is None:
            return self.reload()
        return list(self._cache)

    def get(self, name: str) -> SkillManifest | None:
        normalized = str(name or "").strip()
        if not normalized:
            return None
        for manifest in self.list_manifests():
            if manifest.name == normalized:
                return manifest
        return None
