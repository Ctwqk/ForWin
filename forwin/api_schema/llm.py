from __future__ import annotations

from pydantic import BaseModel


class GenerateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    premise: str
    genre: str = "玄幻"
    num_chapters: int = 3
    project_id: str | None = None


class ModelProfile(BaseModel):
    id: str
    name: str
    has_api_key: bool
    base_url: str
    model: str


__all__ = [
    'GenerateRequest',
    'ModelProfile',
]
