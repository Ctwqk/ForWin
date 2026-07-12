from __future__ import annotations

from pydantic import BaseModel


class ModelProfile(BaseModel):
    id: str
    name: str
    has_api_key: bool
    base_url: str
    model: str


__all__ = [
    'ModelProfile',
]
