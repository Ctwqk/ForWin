"""An arc's beat sheet: its phases, milestones, protections, reveal targets, exit state and bands."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


ProtectionKind = Literal["must_exist", "must_be_alive", "must_hold_state", "must_not_know"]
PROTECTION_KINDS: tuple[str, ...] = get_args(ProtectionKind)
# A reveal target's observer when the reveal is to the reader rather than to an actor.
READER_OBSERVER = "reader"
# A band's chapter count; an arc shorter than the minimum is one short band.
MIN_BAND_CHAPTERS = 4
MAX_BAND_CHAPTERS = 12
BEAT_SHEET_SOURCES = ("llm", "fallback")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class BeatPhase(_Frozen):
    name: str
    chapter_range: tuple[int, int]
    purpose: str


class Milestone(_Frozen):
    id: str
    description: str
    target_range: tuple[int, int]
    required_refs: list[str] = Field(default_factory=list)
    hard: bool = False


class ProtectionSpec(_Frozen):
    """A world state the arc relies on; ``until_chapter`` ends with the milestone that needs it."""

    ref: str
    kind: ProtectionKind
    until_chapter: int
    # must_not_know: the observer who may not learn the fact before ``until_chapter``.
    observer_ref: str = ""
    fact_ref: str = ""
    # must_hold_state: the state field and the value it keeps.
    field: str = ""
    value: Any = None


class RevealTarget(_Frozen):
    fact_ref: str
    observer: str
    target_state: str
    range: tuple[int, int]


class ExitStateItem(_Frozen):
    ref: str
    field: str
    value: Any = None
    note: str = ""


class BandSpan(_Frozen):
    band_index: int
    chapter_start: int
    chapter_end: int


class ArcBeatSheetData(_Frozen):
    protagonist_ref: str
    phases: list[BeatPhase]
    milestones: list[Milestone] = Field(default_factory=list)
    protections: list[ProtectionSpec] = Field(default_factory=list)
    reveal_targets: list[RevealTarget] = Field(default_factory=list)
    exit_state: list[ExitStateItem] = Field(default_factory=list)
    band_breakdown: list[BandSpan]
    reader_promise: ReaderPromise = Field(default_factory=ReaderPromise)
    arc_payoff_map: ArcPayoffMap = Field(default_factory=ArcPayoffMap)


__all__ = [
    "BEAT_SHEET_SOURCES",
    "MAX_BAND_CHAPTERS",
    "MIN_BAND_CHAPTERS",
    "PROTECTION_KINDS",
    "READER_OBSERVER",
    "ArcBeatSheetData",
    "BandSpan",
    "BeatPhase",
    "ExitStateItem",
    "Milestone",
    "ProtectionKind",
    "ProtectionSpec",
    "RevealTarget",
]
