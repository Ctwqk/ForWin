from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SubWorldScope = Literal["global_core", "arc_local"]
SubWorldStatus = Literal["active", "retired"]


class SubWorldSummary(BaseModel):
    id: str = ""
    name: str = ""
    purpose: str = ""
    scope: SubWorldScope = "arc_local"
    status: SubWorldStatus = "active"
    active_in_current_band: bool = False


class ChapterEntryTarget(BaseModel):
    chapter_hint: int = 0
    entity_name: str = ""
    subworld_id: str = ""
    role_hint: str = ""
