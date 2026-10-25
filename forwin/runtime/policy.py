from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QualityProfile = Literal["standard", "pulp"]
GateDelegate = Literal["human", "spark"]
WorldConflictDelegate = Literal["codex", "human"]
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
    # Share of the shortest travel time a claimed movement may undercut before it is an error.
    travel_tolerance: float = Field(default=0.2, ge=0.0, le=1.0)

    @field_validator("signals", mode="before")
    @classmethod
    def _rename_map_movement(cls, value: object) -> object:
        # Policies stored before world gate v6 name its predecessor signal.
        if not isinstance(value, (list, tuple)) or "map_movement" not in value:
            return value
        return tuple(dict.fromkeys(
            "world_consistency" if item == "map_movement" else item for item in value
        ))

    def allows_signal(self, name: str) -> bool:
        return name in self.signals

    def allows_repair_scope(self, scope: str) -> bool:
        return scope in self.repair_scopes

    def effective_rewrite_limit(self, *, has_blocking_issue: bool) -> int:
        if not has_blocking_issue:
            return self.max_rewrites
        return max(self.max_rewrites, self.blocking_rewrites)


class PlanningPolicy(FrozenPolicyModel):
    plan_health: bool
    band_size: int = Field(default=6, ge=4, le=12)
    max_replans_per_band: int = Field(default=2, ge=0, le=5)
    digest_budget_chars: int = Field(default=8000, ge=2000, le=20000)
    band_payload_budget_chars: int = Field(default=60000, ge=1)

    @classmethod
    def for_profile(cls, quality_profile: str) -> PlanningPolicy:
        if quality_profile == "pulp":
            return cls(
                plan_health=False,
                band_size=8,
                max_replans_per_band=1,
                digest_budget_chars=6000,
            )
        return cls(
            plan_health=True,
        )


_PLANNING_PROFILE_KNOBS = tuple(
    name for name, field in PlanningPolicy.model_fields.items() if not field.is_required()
)


class WriterPolicy(FrozenPolicyModel):
    world_view_budget_chars: int = Field(default=6000, ge=1000, le=20000)

    @classmethod
    def for_profile(cls, quality_profile: str) -> WriterPolicy:
        if quality_profile == "pulp":
            return cls(world_view_budget_chars=4000)
        return cls()


class CanonPolicy(FrozenPolicyModel):
    hard_floor: bool = True
    quality_gate: Literal["strict", "pulp_fatal"] = "strict"


class WorldPolicy(FrozenPolicyModel):
    max_new_actors_per_tick: int = Field(default=1, ge=0)
    drift_every_story_minutes: int = Field(default=10080, ge=0)
    max_drift_variables_per_tick: int = Field(default=3, ge=0)
    enabled: bool = True
    max_actor_turns_per_tick: int = Field(default=6, ge=1)
    actor_turn_batch_size: int = Field(default=1, ge=1)
    max_new_intents_per_actor: int = Field(default=2, ge=1)
    max_major_events_per_tick: int = Field(default=1, ge=1)
    max_onstage_slips: int = Field(default=2, ge=0)
    max_rerolls: int = Field(default=2, ge=0)
    conflict_delegate: WorldConflictDelegate = "codex"

    @classmethod
    def for_profile(cls, quality_profile: str) -> WorldPolicy:
        if quality_profile == "pulp":
            return cls(max_actor_turns_per_tick=3, drift_every_story_minutes=0, max_new_actors_per_tick=0)
        return cls()


class ModelsPolicy(FrozenPolicyModel):
    # The dict field makes RuntimePolicy unhashable despite the frozen models.
    role_preferences: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: {"review.world_conflict": ("gpt-6-astra",)}
    )


class RuntimePolicy(FrozenPolicyModel):
    schema_version: Literal[2] = 2
    quality_profile: QualityProfile = "standard"
    model_profile_id: str = ""
    chapter_length: ChapterLengthPolicy
    pause: PausePolicy
    review: ReviewPolicy
    planning: PlanningPolicy
    writer: WriterPolicy = Field(default_factory=WriterPolicy)
    canon: CanonPolicy
    world: WorldPolicy = Field(default_factory=WorldPolicy)
    models: ModelsPolicy = Field(default_factory=ModelsPolicy)
    writer_attention_retries: int = Field(ge=0, le=12)

    @model_validator(mode="before")
    @classmethod
    def _load_profile_defaults(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        profile = data.get("quality_profile", "standard")
        loaded = dict(data)
        world = data.get("world", {})
        if isinstance(world, dict):
            loaded["world"] = {**WorldPolicy.for_profile(profile).model_dump(), **world}
        if isinstance(data.get("planning"), dict):
            defaults = PlanningPolicy.for_profile(profile)
            loaded["planning"] = {
                **{
                    field: getattr(defaults, field)
                    for field in _PLANNING_PROFILE_KNOBS
                },
                **data["planning"],
            }
            loaded["planning"].pop("use_llm_simulation", None)
            loaded["planning"].pop("context_recency_" + "window", None)
            # Policies stored before narrative constraint removal carry this key; remove after deploy.
            loaded["planning"].pop("future_constraints", None)
        writer = data.get("writer", {})
        if isinstance(writer, dict):
            loaded["writer"] = {
                **WriterPolicy.for_profile(profile).model_dump(),
                **writer,
            }
        return loaded

    @classmethod
    def for_profile(cls, profile: QualityProfile, *, model_profile_id: str = "") -> Self:
        if profile == "pulp":
            return cls(
                quality_profile="pulp",
                model_profile_id=model_profile_id,
                chapter_length=ChapterLengthPolicy(min_chars=1800, target_chars=2400, max_chars=3000),
                pause=PausePolicy(manual_checkpoints=False, band_checkpoint_action="continue"),
                review=ReviewPolicy(
                    signals=("lint", "world_consistency", "publisher"),
                    repair_scopes=(),
                    max_rewrites=0,
                    blocking_rewrites=1,
                    repair_models=(),
                ),
                planning=PlanningPolicy.for_profile(profile),
                canon=CanonPolicy(quality_gate="pulp_fatal"),
                world=WorldPolicy.for_profile(profile),
                writer_attention_retries=1,
            )
        return cls(
            quality_profile="standard",
            model_profile_id=model_profile_id,
            chapter_length=ChapterLengthPolicy(min_chars=2500, target_chars=2800, max_chars=3200),
            pause=PausePolicy(),
            review=ReviewPolicy(
                signals=("experience", "lint", "world_consistency", "personality", "canon_quality", "publisher"),
                repair_scopes=("local", "chapter", "band", "arc", "book", "obligation"),
                max_rewrites=3,
                repair_models=("deepseek-reasoner", "deepseek-reasoner", "gpt-5.3-codex-spark"),
            ),
            planning=PlanningPolicy.for_profile(profile),
            canon=CanonPolicy(),
            world=WorldPolicy.for_profile(profile),
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
        world_conflict_delegate: WorldConflictDelegate | None = None,
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
        next_world = WorldPolicy.model_validate(
            {
                **base.world.model_dump(mode="python"),
                "conflict_delegate": (
                    self.world.conflict_delegate
                    if world_conflict_delegate is None
                    else world_conflict_delegate
                ),
            }
        )
        return type(self).model_validate(
            {
                **base.model_dump(mode="python"),
                "chapter_length": next_length,
                "pause": next_pause,
                "world": next_world,
                "models": self.models,
            }
        )


def role_models(policy: RuntimePolicy, role: str) -> tuple[str, ...]:
    return policy.models.role_preferences.get(role, ())
