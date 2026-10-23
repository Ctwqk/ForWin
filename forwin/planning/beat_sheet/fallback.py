"""The deterministic beat sheet shell used when ``plan.arc_beats`` fails, and the band split it shares."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forwin.book_state.runtime import BookStateRuntime
from forwin.genesis.handoff.world_seed_committer import seed_ref_id
from forwin.models.project import ArcPlanVersion
from forwin.planning.beat_sheet.types import (
    MAX_BAND_CHAPTERS,
    MIN_BAND_CHAPTERS,
    ArcBeatSheetData,
    BandSpan,
    BeatPhase,
)


def arc_bounds(arc_plan: ArcPlanVersion) -> tuple[int, int]:
    """The arc's fixed chapter range from the Genesis blueprint; a beat sheet never moves it."""
    start, end = int(arc_plan.chapter_start or 0), int(arc_plan.chapter_end or 0)
    if not 1 <= start <= end:
        raise ValueError(f"arc plan {arc_plan.id} has no chapter range: {start}-{end}")
    return start, end


def split_arc_into_bands(chapter_start: int, chapter_end: int, band_size: int) -> list[BandSpan]:
    """Cut an arc into consecutive bands of ``band_size`` chapters (Ruling P4a-R6).

    An arc shorter than MIN_BAND_CHAPTERS is one short band. A remainder of at least MIN_BAND_CHAPTERS
    chapters becomes the last band. A shorter remainder is spread evenly over the bands, the later bands
    taking the extra chapters, with one band more when spreading would push a band past MAX_BAND_CHAPTERS.
    The beat sheet validator accepts every split this returns.
    """
    length = chapter_end - chapter_start + 1
    if length < 1:
        raise ValueError(f"empty arc: {chapter_start}-{chapter_end}")
    if not MIN_BAND_CHAPTERS <= band_size <= MAX_BAND_CHAPTERS:
        raise ValueError(f"band_size {band_size} is outside {MIN_BAND_CHAPTERS}-{MAX_BAND_CHAPTERS}")
    count, remainder = divmod(length, band_size)
    if length < MIN_BAND_CHAPTERS:
        sizes = [length]
    elif remainder == 0:
        sizes = [band_size] * count
    elif remainder >= MIN_BAND_CHAPTERS:
        sizes = [band_size] * count + [remainder]
    else:
        if -(-length // count) > MAX_BAND_CHAPTERS:
            count += 1
        base, extra = divmod(length, count)
        sizes = [base] * (count - extra) + [base + 1] * extra
    bands: list[BandSpan] = []
    start = chapter_start
    for index, size in enumerate(sizes):
        bands.append(BandSpan(band_index=index, chapter_start=start, chapter_end=start + size - 1))
        start += size
    return bands


def fallback_protagonist_ref(project_id: str, pack: Mapping[str, Any], runtime: BookStateRuntime) -> str:
    """The node of the first story_engine core_cast entry that the world seed planted as a character."""
    world = pack.get("world") if isinstance(pack.get("world"), Mapping) else {}
    story_engine = world.get("story_engine") if isinstance(world.get("story_engine"), Mapping) else {}
    core_cast = story_engine.get("core_cast") if isinstance(story_engine.get("core_cast"), list) else []
    for item in core_cast:
        ref = str(item.get("id") or "").strip() if isinstance(item, Mapping) else ""
        node = runtime.world.get_node(seed_ref_id(project_id, "character", ref)) if ref else None
        if node is not None and node.is_active and str(node.node_type) == "character":
            return node.id
    raise ValueError(f"project {project_id} has no seeded core_cast character to stand as protagonist")


def build_fallback_beat_sheet(
    arc_plan: ArcPlanVersion, *, protagonist_ref: str, band_size: int,
) -> ArcBeatSheetData:
    """Even bands and one phase pursuing the arc goal; no milestone, protection or reveal target."""
    start, end = arc_bounds(arc_plan)
    return ArcBeatSheetData(
        protagonist_ref=protagonist_ref,
        phases=[BeatPhase(name=str(arc_plan.title or ""), chapter_range=(start, end), purpose=str(arc_plan.goal or ""))],
        band_breakdown=split_arc_into_bands(start, end, band_size),
    )


__all__ = ["arc_bounds", "build_fallback_beat_sheet", "fallback_protagonist_ref", "split_arc_into_bands"]
