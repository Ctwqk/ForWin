from __future__ import annotations

from dataclasses import dataclass

from forwin.models.project import ChapterPlan


@dataclass(slots=True)
class BandWindow:
    band_id: str
    chapter_start: int
    chapter_end: int
    active_band: list[ChapterPlan]


class BandWindowResolver:
    def resolve(
        self,
        *,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        detailed_band_size: int,
    ) -> BandWindow:
        ordered = sorted(chapter_plans, key=lambda plan: int(plan.chapter_number or 0))
        if not ordered:
            return BandWindow(
                band_id="band:0:0",
                chapter_start=0,
                chapter_end=0,
                active_band=[],
            )
        band_size = max(1, int(detailed_band_size or 1))
        min_chapter = min(int(plan.chapter_number or 0) for plan in ordered)
        max_chapter = max(int(plan.chapter_number or 0) for plan in ordered)
        anchor = max(min_chapter, min(max_chapter, int(activation_chapter or min_chapter)))
        offset = max(0, anchor - min_chapter)
        chapter_start = min_chapter + (offset // band_size) * band_size
        chapter_end = min(max_chapter, chapter_start + band_size - 1)
        active_band = [
            plan
            for plan in ordered
            if chapter_start <= int(plan.chapter_number or 0) <= chapter_end
        ]
        return BandWindow(
            band_id=f"band:{chapter_start}:{chapter_end}",
            chapter_start=chapter_start,
            chapter_end=chapter_end,
            active_band=active_band,
        )
