from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class BandRole(StrEnum):
    opening = "opening"
    mid_arc = "mid_arc"
    final = "final"


class BandRoleClassification(BaseModel):
    role: BandRole
    reason: str = ""


def classify_band_role(
    *,
    band_index: int,
    total_bands: int,
    target_total_chapters: int,
    last_chapter_of_band: int,
) -> BandRoleClassification:
    if int(band_index or 0) == 0:
        return BandRoleClassification(role=BandRole.opening, reason="first band opens the project arc")
    target_total = int(target_total_chapters or 0)
    band_end = int(last_chapter_of_band or 0)
    if target_total and band_end == target_total:
        return BandRoleClassification(role=BandRole.final, reason="band ends at target_total_chapters")
    if not target_total:
        return BandRoleClassification(role=BandRole.mid_arc, reason="no target_total_chapters; non-opening band stays mid_arc")
    return BandRoleClassification(
        role=BandRole.mid_arc,
        reason=f"band_end={band_end} precedes target_total_chapters={target_total}",
    )


__all__ = ["BandRole", "BandRoleClassification", "classify_band_role"]
