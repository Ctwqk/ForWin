from __future__ import annotations

from dataclasses import dataclass, field

from forwin.planning.beat_sheet.types import ArcBeatSheetData, BandSpan
from forwin.protocol.experience import ArcPayoffMap, ChapterExperiencePlan, ReaderPromise


@dataclass(slots=True)
class BeatSheetExperienceInput:
    """What the band scheduler and chapter experience planner read from an arc's plan."""

    key_beats: list[str] = field(default_factory=list)
    reader_promise: ReaderPromise = field(default_factory=ReaderPromise)
    arc_payoff_map: ArcPayoffMap = field(default_factory=ArcPayoffMap)

    @classmethod
    def from_beat_sheet(cls, data: ArcBeatSheetData, band: BandSpan | None = None) -> BeatSheetExperienceInput:
        """The key beats are the purposes of the phases, then the descriptions of the milestones, that the band
        overlaps (all of them without a band), in chapter order."""

        def overlaps(chapter_range: tuple[int, int]) -> bool:
            return band is None or (chapter_range[0] <= band.chapter_end and chapter_range[1] >= band.chapter_start)

        phases = sorted((phase for phase in data.phases if overlaps(phase.chapter_range)),
                        key=lambda phase: phase.chapter_range)
        milestones = sorted((item for item in data.milestones if overlaps(item.target_range)),
                            key=lambda item: (item.target_range, item.id))
        beats = [phase.purpose for phase in phases] + [item.description for item in milestones]
        return cls(
            key_beats=[text for beat in beats if (text := beat.strip())],
            reader_promise=data.reader_promise,
            arc_payoff_map=data.arc_payoff_map,
        )


@dataclass(slots=True)
class ChapterPlanningOverlay:
    chapter_number: int
    experience_plan: ChapterExperiencePlan
