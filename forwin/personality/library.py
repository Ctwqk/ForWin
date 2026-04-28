from __future__ import annotations

from pathlib import Path
from typing import Any

from forwin.skills.registry import SkillRegistry

from .models import PersonalitySkillInfo


def default_personality_library_root() -> Path:
    return Path(__file__).resolve().parents[2] / "forwin_skills" / "character_personality"


class CharacterPersonalityLibrary:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_personality_library_root()
        self.registry = SkillRegistry(self.root)

    def list_skills(self) -> list[PersonalitySkillInfo]:
        skills: list[PersonalitySkillInfo] = []
        for manifest in self.registry.list_manifests():
            metadata = dict(manifest.metadata)
            scope = str(metadata.get("forwin_scope", manifest.forwin_scope) or "").strip()
            category = str(metadata.get("category", "") or "").strip()
            if scope != "character_personality" and category != "character_personality_skill":
                continue
            skills.append(
                PersonalitySkillInfo(
                    name=manifest.name,
                    version=manifest.version,
                    description=manifest.description,
                    skill_type=str(metadata.get("skill_type", "") or "").strip(),
                    path=manifest.path,
                    skill_hash=manifest.skill_hash,
                    metadata=metadata,
                    body=manifest.body,
                    incomplete=_looks_incomplete(manifest.body),
                )
            )
        return skills

    def get(self, name: str) -> PersonalitySkillInfo | None:
        normalized = str(name or "").strip()
        if not normalized:
            return None
        for skill in self.list_skills():
            if skill.name == normalized:
                return skill
        return None

    def catalog_payload(self) -> dict[str, Any]:
        return {"skills": [skill.catalog_payload() for skill in self.list_skills()]}

    def validate_skill_ids(self, skill_ids: set[str]) -> list[str]:
        available = {skill.name for skill in self.list_skills()}
        return sorted(skill_id for skill_id in skill_ids if skill_id not in available)


def _looks_incomplete(body: str) -> bool:
    normalized = str(body or "").strip().lower()
    if not normalized:
        return True
    return "todo" in normalized or "tbd" in normalized or "待填写" in normalized
