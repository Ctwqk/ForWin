"""Shapes of the POV world view; every field holds text the writer may see."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnstageEntity(_ViewModel):
    """What anyone in the scene can perceive of one onstage entity."""

    ref: str
    name: str = ""
    location_name: str = ""
    alive_status: str = ""
    # The restraint state token; empty when free.
    restraint: str = ""
    health: str = ""
    # Names of the items and resources it holds; concealed ones only for the POV itself.
    held_items: list[str] = Field(default_factory=list)
    public_identity: str = ""


class KnownFact(_ViewModel):
    fact_ref: str
    proposition: str
    # The POV's recorded knowledge state.
    state: str
    # The POV believes or knows a fact whose truth_value is false.
    pov_believes_false: bool = False
    # A secret the POV holds that the reader may not learn in this chapter.
    pov_only_secret: bool = False


class AllowedReveal(_ViewModel):
    """A reveal directive of the chapter with the truth it permits."""

    directive_id: str
    fact_ref: str
    proposition: str
    # "reader" or the actor ref that comes to the target state.
    observer: str
    # The actor observer's name; empty for the reader.
    observer_name: str = ""
    target_state: str
    # The fact's subjects as a forbidden-topic phrase, without its proposition; empty when none is nameable.
    topic: str = ""


class ViewEntity(_ViewModel):
    """An entity the POV knows of, by name only."""

    ref: str
    name: str
    aliases: list[str] = Field(default_factory=list)


class ViewHint(_ViewModel):
    directive_id: str
    hint_text: str


class ForbiddenTopic(_ViewModel):
    """A secret the writer must not settle, named by its subjects without its proposition."""

    fact_ref: str
    topic: str


class ViewLocation(_ViewModel):
    id: str
    name: str = ""
    # The POV's travel minutes over writer-visible routes; None without such a route.
    minutes_from_pov: int | None = None


class ViewClock(_ViewModel):
    label: str
    # Empty when the clock has no due time.
    remaining_label: str = ""
    # A secret clock the POV knows as an insider: not for the reader.
    secret: bool = False


class ChapterWorldView(_ViewModel):
    pov_ref: str = ""
    pov_name: str = ""
    onstage: list[OnstageEntity] = Field(default_factory=list)
    # Other entities the POV knows of: names the writer may use besides the onstage ones.
    known_entities: list[ViewEntity] = Field(default_factory=list)
    pov_knowledge: list[KnownFact] = Field(default_factory=list)
    # "<fact_ref>: <proposition>" of recorded reader knowledge about onstage refs and chapter locations.
    reader_known: list[str] = Field(default_factory=list)
    allowed_reveals: list[AllowedReveal] = Field(default_factory=list)
    hints: list[ViewHint] = Field(default_factory=list)
    forbidden_topics: list[ForbiddenTopic] = Field(default_factory=list)
    locations: list[ViewLocation] = Field(default_factory=list)
    pov_location: ViewLocation | None = None
    now_label: str = ""
    expected_span_label: str = ""
    pov_clocks: list[ViewClock] = Field(default_factory=list)
    # Keys of the trimmed parts, in trimming order.
    trimmed: list[str] = Field(default_factory=list)


__all__ = [
    "AllowedReveal", "ChapterWorldView", "ForbiddenTopic", "KnownFact", "OnstageEntity", "ViewClock", "ViewEntity",
    "ViewHint", "ViewLocation",
]
