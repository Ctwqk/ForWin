from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FormAnswer(BaseModel):
    value: str
    evidence_quote: str = ""
    subject_of_quote: str = ""
    confidence: float = 0.0
    explanation: str = ""

    def is_bindable(self, min_confidence: float) -> bool:
        return self.confidence >= float(min_confidence) and bool(self.evidence_quote.strip())


class CharacterReviewAsk(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    descriptive_aliases: list[str] = Field(default_factory=list)
    prior_life_state: Literal["alive", "wounded", "dead", "unknown"]
    prior_custody_state: Literal["free", "captured", "unknown"]
    last_seen_chapter: int
    must_track: bool = False


class BridgeEvent(BaseModel):
    event_kind: Literal["capture", "release", "wound", "death", "resurrection_or_correction"]
    evidence_quote: str
    subject_of_quote: str
    confidence: float


class CharacterReviewAnswer(BaseModel):
    name: str
    appears_in_chapter: bool
    life_state: FormAnswer
    custody_state: FormAnswer
    participation: FormAnswer
    bridge_events: list[BridgeEvent] = Field(default_factory=list)


class CountdownReviewAsk(BaseModel):
    key: str
    label: str
    prior_value_minutes: int | None
    prior_status: Literal["active", "paused", "closed", "fulfilled", "reopened", "consistent", "warning", "conflict", "resolved"]
    last_updated_chapter: int
    aliases: list[str] = Field(default_factory=list)


class CountdownReviewAnswer(BaseModel):
    key: str
    mentioned_in_chapter: bool
    status_in_this_chapter: FormAnswer
    new_value_minutes: int | None = None
    new_value_evidence: FormAnswer | None = None
    consistent_with_prior: FormAnswer
    inconsistency_kind: Literal[
        "regression",
        "magnitude_mismatch",
        "reopened_after_close",
        "other",
        "none",
    ] = "none"


class ObligationReviewAsk(BaseModel):
    id: str
    summary: str
    deadline_chapter: int
    must_resolve_now: bool
    payoff_test: str


class ObligationReviewAnswer(BaseModel):
    id: str
    addressed: FormAnswer
    payoff_evidence: FormAnswer | None = None


class OpenSignalReviewAsk(BaseModel):
    id: str
    description: str
    severity: str
    age_chapters: int


class OpenSignalReviewAnswer(BaseModel):
    id: str
    status: FormAnswer
    resolution_evidence: FormAnswer | None = None


class NewCharacterObservation(BaseModel):
    name: str
    first_appearance_quote: str
    role_hint: str = ""


class NewCountdownObservation(BaseModel):
    description: str
    initial_value_minutes: int | None
    first_mention_quote: str


class NewWorldFact(BaseModel):
    fact: str
    evidence_quote: str
    category: Literal["setting", "rule", "identity", "relationship", "other"]


class NewObservations(BaseModel):
    new_characters: list[NewCharacterObservation] = Field(default_factory=list)
    new_countdowns: list[NewCountdownObservation] = Field(default_factory=list)
    new_world_facts: list[NewWorldFact] = Field(default_factory=list)


class FinalChapterAsk(BaseModel):
    main_crisis_descriptors: list[str] = Field(default_factory=list)
    expected_closure_kinds: list[str] = Field(default_factory=list)


class FinalChapterAnswer(BaseModel):
    main_crisis_status: FormAnswer
    closure_evidence: FormAnswer | None = None
    unresolved_promises: list[str] = Field(default_factory=list)


class ChapterReviewForm(BaseModel):
    project_id: str
    chapter_number: int
    form_schema_version: str
    characters: list[CharacterReviewAsk] = Field(default_factory=list)
    countdowns: list[CountdownReviewAsk] = Field(default_factory=list)
    obligations: list[ObligationReviewAsk] = Field(default_factory=list)
    open_signals: list[OpenSignalReviewAsk] = Field(default_factory=list)
    final_chapter: FinalChapterAsk | None = None


class ChapterReviewAnswers(BaseModel):
    project_id: str
    chapter_number: int
    form_schema_version: str
    characters: list[CharacterReviewAnswer] = Field(default_factory=list)
    countdowns: list[CountdownReviewAnswer] = Field(default_factory=list)
    obligations: list[ObligationReviewAnswer] = Field(default_factory=list)
    open_signals: list[OpenSignalReviewAnswer] = Field(default_factory=list)
    new_observations: NewObservations = Field(default_factory=NewObservations)
    final_chapter: FinalChapterAnswer | None = None
    chapter_summary: str = ""
