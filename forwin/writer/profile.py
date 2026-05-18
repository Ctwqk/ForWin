from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class WriterProfile(BaseModel):
    temperature: float = 0.85
    max_tokens: int = 16384
    default_scene_count: int = 3
    max_scene_count: int = 4
    min_chapter_chars: int = 2500
    target_chapter_chars: int = 2800
    max_chapter_chars: int = 3200
    prompt_budget_chars: int = 12000

    @classmethod
    def from_values(cls, **values: Any) -> "WriterProfile":
        raw = {key: value for key, value in values.items() if value is not None}
        profile = cls(**raw)
        min_chars = max(300, int(profile.min_chapter_chars))
        max_chars = max(min_chars, int(profile.max_chapter_chars))
        target_chars = max(min_chars, min(int(profile.target_chapter_chars), max_chars))
        default_scene_count = max(1, int(profile.default_scene_count))
        max_scene_count = max(default_scene_count, int(profile.max_scene_count))
        return profile.model_copy(
            update={
                "temperature": float(profile.temperature),
                "max_tokens": max(1, int(profile.max_tokens)),
                "default_scene_count": default_scene_count,
                "max_scene_count": max_scene_count,
                "min_chapter_chars": min_chars,
                "target_chapter_chars": target_chars,
                "max_chapter_chars": max_chars,
                "prompt_budget_chars": max(1000, int(profile.prompt_budget_chars)),
            }
        )


def writer_profile_from_config(config: object) -> WriterProfile:
    return WriterProfile.from_values(
        temperature=getattr(config, "temperature", 0.85),
        max_tokens=getattr(config, "max_tokens", 16384),
        default_scene_count=getattr(config, "default_scene_count", 3),
        max_scene_count=getattr(config, "max_scene_count", 4),
        min_chapter_chars=getattr(config, "min_chapter_chars", 2500),
        target_chapter_chars=getattr(config, "target_chapter_chars", 2800),
        max_chapter_chars=getattr(config, "max_chapter_chars", 3200),
        prompt_budget_chars=getattr(config, "prompt_budget_chars", 12000),
    )
