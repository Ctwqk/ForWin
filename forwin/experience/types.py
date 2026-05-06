from __future__ import annotations

from dataclasses import dataclass

from forwin.protocol.experience import ArcPayoffMap, ChapterExperiencePlan, ReaderPromise


@dataclass(slots=True)
class ArcExperienceBundle:
    reader_promise: ReaderPromise
    arc_payoff_map: ArcPayoffMap


@dataclass(slots=True)
class ChapterPlanningOverlay:
    chapter_number: int
    experience_plan: ChapterExperiencePlan
