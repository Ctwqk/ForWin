from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

QualityProfile = Literal["standard", "pulp"]
GateDelegate = Literal["human", "spark"]
BandCheckpointAction = Literal["continue", "pause_on_warn", "pause_always"]


class FrozenPolicyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChapterLengthPolicy(FrozenPolicyModel):
    min_chars: int = Field(ge=500, le=20000)
    target_chars: int = Field(ge=500, le=20000)
    max_chars: int = Field(ge=500, le=20000)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not self.min_chars <= self.target_chars <= self.max_chars:
            raise ValueError("chapter lengths must satisfy min <= target <= max")
        return self


class PausePolicy(FrozenPolicyModel):
    review_interval_chapters: int = Field(default=0, ge=0, le=500)
    manual_checkpoints: bool = True
    band_checkpoint_action: BandCheckpointAction = "pause_on_warn"
    gate_delegate: GateDelegate = "human"


class ReviewPolicy(FrozenPolicyModel):
    signals: tuple[str, ...]
    repair_scopes: tuple[str, ...]
    max_rewrites: int = Field(ge=0, le=12)
    blocking_rewrites: int = Field(default=0, ge=0, le=12)
    repair_models: tuple[str, ...]

    def allows_signal(self, name: str) -> bool:
        return name in self.signals

    def allows_repair_scope(self, scope: str) -> bool:
        return scope in self.repair_scopes

    def effective_rewrite_limit(self, *, has_blocking_issue: bool) -> int:
        if not has_blocking_issue:
            return self.max_rewrites
        return max(self.max_rewrites, self.blocking_rewrites)


class PlanningPolicy(FrozenPolicyModel):
    future_constraints: bool
    plan_health: bool
    use_llm_simulation: bool
    context_recency_window: int = Field(ge=0, le=1000)


class CanonPolicy(FrozenPolicyModel):
    hard_floor: bool = True
    quality_gate: Literal["strict", "pulp_fatal"] = "strict"
    book_state_layers: tuple[Literal["world", "map", "cognition", "narrative"], ...]


class RuntimePolicy(FrozenPolicyModel):
    schema_version: Literal[2] = 2
    quality_profile: QualityProfile = "standard"
    model_profile_id: str = ""
    chapter_length: ChapterLengthPolicy
    pause: PausePolicy
    review: ReviewPolicy
    planning: PlanningPolicy
    canon: CanonPolicy
    writer_attention_retries: int = Field(ge=0, le=12)

    @classmethod
    def for_profile(cls, profile: QualityProfile, *, model_profile_id: str = "") -> Self:
        if profile == "pulp":
            return cls(
                quality_profile="pulp",
                model_profile_id=model_profile_id,
                chapter_length=ChapterLengthPolicy(min_chars=1800, target_chars=2400, max_chars=3000),
                pause=PausePolicy(manual_checkpoints=False, band_checkpoint_action="continue"),
                review=ReviewPolicy(
                    signals=("lint", "publisher"),
                    repair_scopes=(),
                    max_rewrites=0,
                    blocking_rewrites=1,
                    repair_models=(),
                ),
                planning=PlanningPolicy(future_constraints=False, plan_health=False, use_llm_simulation=False, context_recency_window=50),
                canon=CanonPolicy(quality_gate="pulp_fatal", book_state_layers=("world",)),
                writer_attention_retries=1,
            )
        return cls(
            quality_profile="standard",
            model_profile_id=model_profile_id,
            chapter_length=ChapterLengthPolicy(min_chars=2500, target_chars=2800, max_chars=3200),
            pause=PausePolicy(),
            review=ReviewPolicy(
                signals=("experience", "lint", "map_movement", "personality", "canon_quality", "publisher"),
                repair_scopes=("local", "chapter", "band", "arc", "book", "obligation"),
                max_rewrites=3,
                repair_models=("deepseek-reasoner", "deepseek-reasoner", "gpt-5.3-codex-spark"),
            ),
            planning=PlanningPolicy(future_constraints=True, plan_health=True, use_llm_simulation=True, context_recency_window=0),
            canon=CanonPolicy(book_state_layers=("world", "map", "cognition", "narrative")),
            writer_attention_retries=3,
        )

    def with_user_settings(
        self,
        *,
        quality_profile: QualityProfile | None = None,
        model_profile_id: str | None = None,
        min_chars: int | None = None,
        target_chars: int | None = None,
        max_chars: int | None = None,
        review_interval_chapters: int | None = None,
        manual_checkpoints: bool | None = None,
        band_checkpoint_action: BandCheckpointAction | None = None,
        gate_delegate: GateDelegate | None = None,
    ) -> Self:
        selected_profile = quality_profile or self.quality_profile
        selected_model = self.model_profile_id if model_profile_id is None else model_profile_id.strip()
        base = (
            type(self).for_profile(selected_profile, model_profile_id=selected_model)
            if selected_profile != self.quality_profile
            else self.model_copy(update={"model_profile_id": selected_model})
        )
        length_updates = {
            key: value
            for key, value in {
                "min_chars": min_chars,
                "target_chars": target_chars,
                "max_chars": max_chars,
            }.items()
            if value is not None
        }
        pause_updates = {
            key: value
            for key, value in {
                "review_interval_chapters": review_interval_chapters,
                "manual_checkpoints": manual_checkpoints,
                "band_checkpoint_action": band_checkpoint_action,
                "gate_delegate": gate_delegate,
            }.items()
            if value is not None
        }
        next_length = ChapterLengthPolicy.model_validate(
            {**base.chapter_length.model_dump(mode="python"), **length_updates}
        )
        next_pause = PausePolicy.model_validate(
            {**base.pause.model_dump(mode="python"), **pause_updates}
        )
        return type(self).model_validate(
            {
                **base.model_dump(mode="python"),
                "chapter_length": next_length,
                "pause": next_pause,
            }
        )
