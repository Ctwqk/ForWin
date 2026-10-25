import json

import pytest
from pydantic import ValidationError

from forwin.runtime.policy import CanonPolicy, PlanningPolicy, ReviewPolicy, RuntimePolicy, WorldPolicy, role_models


@pytest.mark.parametrize(
    "profile, expected",
    [
        ("standard", (6, 2, 8000, 6000)),
        ("pulp", (8, 1, 6000, 4000)),
    ],
)
def test_profile_defaults_for_planning_and_writer_knobs(profile, expected) -> None:
    policy = RuntimePolicy.for_profile(profile)

    assert (
        policy.planning.band_size,
        policy.planning.max_replans_per_band,
        policy.planning.digest_budget_chars,
        policy.writer.world_view_budget_chars,
    ) == expected
    assert policy.planning.band_payload_budget_chars == 60000


@pytest.mark.parametrize("band_size", [3, 13])
def test_band_size_bounds_enforced(band_size) -> None:
    stored = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    stored["planning"]["band_size"] = band_size

    with pytest.raises(ValidationError) as exc:
        RuntimePolicy.model_validate(stored)
    assert exc.value.errors()[0]["loc"] == ("planning", "band_size")
    assert exc.value.errors()[0]["type"] in {"greater_than_equal", "less_than_equal"}


@pytest.mark.parametrize(
    "section, field, value",
    [
        ("planning", "max_replans_per_band", -1),
        ("planning", "max_replans_per_band", 6),
        ("planning", "digest_budget_chars", 1999),
        ("planning", "digest_budget_chars", 20001),
        ("writer", "world_view_budget_chars", 999),
        ("writer", "world_view_budget_chars", 20001),
    ],
)
def test_planning_and_writer_budget_bounds_enforced(section, field, value) -> None:
    stored = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    stored.setdefault(section, {})[field] = value

    with pytest.raises(ValidationError) as exc:
        RuntimePolicy.model_validate(stored)
    assert exc.value.errors()[0]["loc"] == (section, field)
    assert exc.value.errors()[0]["type"] in {"greater_than_equal", "less_than_equal"}


@pytest.mark.parametrize(
    "band_size, max_replans, digest_budget, world_view_budget",
    [(4, 0, 2000, 1000), (12, 5, 20000, 20000)],
)
def test_planning_and_writer_knobs_accept_boundaries(
    band_size, max_replans, digest_budget, world_view_budget,
) -> None:
    stored = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    stored["planning"].update(
        band_size=band_size,
        max_replans_per_band=max_replans,
        digest_budget_chars=digest_budget,
    )
    stored["writer"] = {"world_view_budget_chars": world_view_budget}

    policy = RuntimePolicy.model_validate(stored)

    assert policy.planning.band_size == band_size
    assert policy.planning.max_replans_per_band == max_replans
    assert policy.planning.digest_budget_chars == digest_budget
    assert policy.writer.world_view_budget_chars == world_view_budget


@pytest.mark.parametrize(
    "profile, expected",
    [
        ("standard", (6, 2, 8000, 6000)),
        ("pulp", (8, 1, 6000, 4000)),
    ],
)
def test_stored_policy_without_new_sections_loads(profile, expected) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored.pop("writer", None)
    for name, field in PlanningPolicy.model_fields.items():
        if not field.is_required():
            stored["planning"].pop(name, None)
    stored["planning"]["context_recency_" + "window"] = 73
    original = json.dumps(stored)

    policy = RuntimePolicy.model_validate(stored)

    assert (
        policy.planning.band_size,
        policy.planning.max_replans_per_band,
        policy.planning.digest_budget_chars,
        policy.writer.world_view_budget_chars,
    ) == expected
    assert "context_recency_" + "window" not in policy.planning.model_dump()
    assert json.dumps(stored) == original
    assert RuntimePolicy.model_validate_json(policy.model_dump_json()) == policy


@pytest.mark.parametrize(
    "profile, expected_replans, expected_digest",
    [("standard", 2, 8000), ("pulp", 1, 6000)],
)
def test_partial_planning_defaults_preserve_stored_knobs(
    profile, expected_replans, expected_digest,
) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["planning"]["band_size"] = 10
    stored["planning"].pop("max_replans_per_band", None)
    stored["planning"].pop("digest_budget_chars", None)
    stored["writer"] = {"world_view_budget_chars": 12000}

    policy = RuntimePolicy.model_validate_json(json.dumps(stored))

    assert policy.planning.band_size == 10
    assert policy.planning.max_replans_per_band == expected_replans
    assert policy.planning.digest_budget_chars == expected_digest
    assert policy.writer.world_view_budget_chars == 12000


@pytest.mark.parametrize("profile, expected_budget", [("standard", 6000), ("pulp", 4000)])
def test_stored_empty_writer_section_uses_profile_defaults(profile, expected_budget) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["writer"] = {}

    policy = RuntimePolicy.model_validate(stored)

    assert policy.writer.world_view_budget_chars == expected_budget


@pytest.mark.parametrize("profile, actor_turns", [("standard", 6), ("pulp", 3)])
def test_world_and_models_defaults_per_profile(profile, actor_turns) -> None:
    policy = RuntimePolicy.for_profile(profile)
    assert policy.world.model_dump() == {
        "max_new_actors_per_tick": 0 if profile == "pulp" else 1,
        "drift_every_story_minutes": 0 if profile == "pulp" else 10080,
        "max_drift_variables_per_tick": 3,
        "enabled": True,
        "max_actor_turns_per_tick": actor_turns,
        "actor_turn_batch_size": 1,
        "max_new_intents_per_actor": 2,
        "max_major_events_per_tick": 1,
        "max_onstage_slips": 2,
        "max_rerolls": 2,
        "conflict_delegate": "codex",
    }
    assert policy.models.role_preferences == {"review.world_conflict": ("gpt-6-astra",)}
    assert role_models(policy, "review.world_conflict") == ("gpt-6-astra",)
    assert role_models(policy, "unknown.role") == ()


@pytest.mark.parametrize("profile, actor_turns", [("standard", 6), ("pulp", 3)])
def test_stored_policy_without_world_section_loads_defaults(profile, actor_turns) -> None:
    stored = json.loads(RuntimePolicy.for_profile(profile).model_dump_json())
    stored.pop("world", None)
    stored.pop("models", None)
    policy = RuntimePolicy.model_validate_json(json.dumps(stored))
    assert policy.schema_version == 2
    assert policy.world.conflict_delegate == "codex"
    assert policy.world.max_actor_turns_per_tick == actor_turns
    assert policy.models.role_preferences == {"review.world_conflict": ("gpt-6-astra",)}
    saved = policy.with_user_settings(gate_delegate="spark").model_dump_json()
    assert RuntimePolicy.model_validate_json(saved).world.max_actor_turns_per_tick == actor_turns


@pytest.mark.parametrize(
    "profile, next_profile", [("standard", "pulp"), ("pulp", "standard")]
)
def test_custom_role_preferences_survive_profile_change(profile, next_profile) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="python")
    preferences = {
        "review.world_conflict": ("custom-primary", "custom-fallback"),
        "world.actor_turn": ("custom-actor",),
    }
    stored["models"]["role_preferences"] = preferences
    policy = RuntimePolicy.model_validate(stored)

    updated = policy.with_user_settings(quality_profile=next_profile)

    assert updated.quality_profile == next_profile
    assert updated.models.role_preferences == preferences


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_actor_turns_per_tick", -1),
        ("actor_turn_batch_size", 0),
        ("max_new_intents_per_actor", 0),
        ("max_major_events_per_tick", 0),
        ("max_onstage_slips", -1),
        ("max_rerolls", -1),
    ],
)
def test_world_policy_rejects_counts_below_minimum(field, value) -> None:
    with pytest.raises(ValidationError) as exc:
        WorldPolicy.model_validate({field: value})
    assert exc.value.errors()[0]["loc"] == (field,)


def test_world_policy_accepts_minimum_counts() -> None:
    policy = WorldPolicy(
        max_actor_turns_per_tick=1,
        actor_turn_batch_size=1,
        max_new_intents_per_actor=1,
        max_major_events_per_tick=1,
        max_onstage_slips=0,
        max_rerolls=0,
    )
    assert policy.max_actor_turns_per_tick == 1
    assert policy.max_onstage_slips == 0
    assert policy.max_rerolls == 0


def test_world_conflict_delegate_survives_other_settings_and_profile_changes() -> None:
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        world_conflict_delegate="human"
    )
    policy = policy.with_user_settings(gate_delegate="spark")
    assert policy.world.conflict_delegate == "human"
    policy = policy.with_user_settings(quality_profile="pulp")
    assert policy.world.conflict_delegate == "human"
    assert policy.world.max_actor_turns_per_tick == 3
    policy = policy.with_user_settings(
        quality_profile="standard", world_conflict_delegate="codex"
    )
    assert policy.world.conflict_delegate == "codex"
    assert policy.world.max_actor_turns_per_tick == 6
    with pytest.raises(ValidationError):
        policy.with_user_settings(world_conflict_delegate="spark")


def test_canon_policy_has_no_layer_switch() -> None:
    assert "book_state_layers" not in CanonPolicy.model_fields
    RuntimePolicy.for_profile("pulp")
    RuntimePolicy.for_profile("standard")


def test_standard_policy_is_strict_and_complete() -> None:
    policy = RuntimePolicy.for_profile("standard", model_profile_id="env-kimi")
    assert policy.schema_version == 2
    assert policy.quality_profile == "standard"
    assert policy.model_profile_id == "env-kimi"
    assert policy.chapter_length.model_dump() == {
        "min_chars": 2500,
        "target_chars": 2800,
        "max_chars": 3200,
    }
    assert policy.pause.gate_delegate == "human"
    assert "generation_audit_" + "interval" not in policy.pause.model_dump()
    assert "generation_audit_" + "pauses" not in policy.pause.model_dump()
    assert policy.canon.hard_floor is True
    assert policy.review.allows_signal("canon_quality")
    assert policy.review.allows_repair_scope("book")
    assert policy.review.effective_rewrite_limit(has_blocking_issue=False) == 3
    assert policy.review.effective_rewrite_limit(has_blocking_issue=True) == 3


def test_pulp_policy_is_deliberately_small_but_keeps_hard_floor() -> None:
    policy = RuntimePolicy.for_profile("pulp")
    assert policy.chapter_length.target_chars == 2400
    assert policy.pause.manual_checkpoints is False
    assert policy.pause.band_checkpoint_action == "continue"
    assert policy.review.max_rewrites == 0
    assert policy.review.blocking_rewrites == 1
    assert policy.review.repair_scopes == ()
    assert policy.review.effective_rewrite_limit(has_blocking_issue=False) == 0
    assert policy.review.effective_rewrite_limit(has_blocking_issue=True) == 1
    assert policy.canon.hard_floor is True


def test_runtime_policy_rejects_removed_values_and_invalid_lengths() -> None:
    payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    for removed in ("premium", "checkpoint", "copilot"):
        invalid = dict(payload)
        invalid["quality_profile"] = removed
        with pytest.raises(ValidationError):
            RuntimePolicy.model_validate(invalid)
    with pytest.raises(ValidationError):
        RuntimePolicy.for_profile("standard").with_user_settings(
            min_chars=3200,
            target_chars=2800,
            max_chars=3000,
        )


def test_stored_map_movement_signal_loads_as_world_consistency() -> None:
    # Policies stored before world gate v6 list its predecessor signal.
    stored = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    stored["review"]["signals"] = [
        "experience", "lint", "map_movement", "personality", "canon_quality", "publisher",
    ]
    policy = RuntimePolicy.model_validate_json(json.dumps(stored))

    assert policy.review.signals == RuntimePolicy.for_profile("standard").review.signals
    assert not policy.review.allows_signal("map_movement")
    both = {**stored["review"], "signals": ["map_movement", "lint", "world_consistency"]}
    assert ReviewPolicy.model_validate(both).signals == ("world_consistency", "lint")


def test_stored_policy_with_removed_keys_loads() -> None:
    stored = RuntimePolicy.for_profile("pulp").model_dump(mode="json")
    stored["planning"]["use_llm_simulation"] = True
    original = json.dumps(stored)
    policy = RuntimePolicy.model_validate_json(original)
    assert "use_llm_simulation" not in policy.planning.model_dump()
    assert "context_recency_" + "window" not in policy.planning.model_dump()
    assert json.dumps(stored) == original


@pytest.mark.parametrize("profile, future_constraints", [("standard", True), ("pulp", False)])
def test_stored_policy_with_future_constraints_loads(profile, future_constraints) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["planning"]["future_constraints"] = future_constraints
    stored["planning"]["band_size"] = 10

    policy = RuntimePolicy.model_validate_json(json.dumps(stored))

    assert "future_constraints" not in policy.planning.model_dump()
    assert policy.planning.band_size == 10


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_with_user_settings_resets_planning_writer_knobs_on_profile_switch(profile) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["planning"].update(band_size=11, max_replans_per_band=4, digest_budget_chars=12345)
    stored["writer"]["world_view_budget_chars"] = 12345
    policy = RuntimePolicy.model_validate(stored)
    target = "pulp" if profile == "standard" else "standard"
    updated = policy.with_user_settings(quality_profile=target)
    defaults = RuntimePolicy.for_profile(target)
    assert updated.planning == defaults.planning
    assert updated.writer == defaults.writer


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_with_user_settings_keeps_custom_planning_writer_knobs_on_same_profile(profile) -> None:
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["planning"].update(band_size=11, max_replans_per_band=4, digest_budget_chars=12345)
    stored["writer"]["world_view_budget_chars"] = 12345
    policy = RuntimePolicy.model_validate(stored)
    updated = policy.with_user_settings(quality_profile=profile, review_interval_chapters=7)
    assert updated.planning == policy.planning
    assert updated.writer == policy.writer
    assert updated.pause.review_interval_chapters == 7


def test_unknown_writer_key_is_rejected() -> None:
    stored = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    stored["writer"]["unknown"] = True
    with pytest.raises(ValidationError) as exc:
        RuntimePolicy.model_validate(stored)
    assert exc.value.errors()[0]["loc"] == ("writer", "unknown")
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_band_payload_budget_loads_defaults_and_preserves_override(profile):
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    del stored["planning"]["band_payload_budget_chars"]
    assert RuntimePolicy.model_validate(stored).planning.band_payload_budget_chars == 60000
    stored["planning"]["band_payload_budget_chars"] = 9000
    assert RuntimePolicy.model_validate(stored).planning.band_payload_budget_chars == 9000
    stored["planning"]["band_payload_budget_chars"] = 0
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate(stored)


@pytest.mark.parametrize("profile,every", [("standard", 10080), ("pulp", 0)])
def test_world_policy_drift_defaults_and_merge(profile, every):
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    stored["world"] = {"conflict_delegate": "human"}
    original = json.dumps(stored)
    loaded = RuntimePolicy.model_validate(stored)
    assert loaded.world.drift_every_story_minutes == every
    assert loaded.world.max_drift_variables_per_tick == 3
    assert loaded.world.max_actor_turns_per_tick == (3 if profile == "pulp" else 6)
    assert loaded.world.conflict_delegate == "human"
    assert json.dumps(stored) == original
    stored["world"].update(drift_every_story_minutes=17, max_drift_variables_per_tick=0)
    custom = RuntimePolicy.model_validate(stored)
    assert custom.world.drift_every_story_minutes == 17
    assert custom.world.max_drift_variables_per_tick == 0
    for field in ("drift_every_story_minutes", "max_drift_variables_per_tick"):
        with pytest.raises(ValidationError):
            WorldPolicy.model_validate({field: -1})


@pytest.mark.parametrize("profile,limit", [("standard", 1), ("pulp", 0)])
def test_new_actor_budget_profile_defaults_and_override(profile, limit):
    stored = RuntimePolicy.for_profile(profile).model_dump(mode="json")
    del stored["world"]["max_new_actors_per_tick"]
    assert RuntimePolicy.model_validate(stored).world.max_new_actors_per_tick == limit
    stored["world"]["max_new_actors_per_tick"] = 2
    assert RuntimePolicy.model_validate(stored).world.max_new_actors_per_tick == 2
    with pytest.raises(ValidationError):
        WorldPolicy(max_new_actors_per_tick=-1)
