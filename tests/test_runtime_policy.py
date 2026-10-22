import json

import pytest
from pydantic import ValidationError

from forwin.runtime.policy import CanonPolicy, ReviewPolicy, RuntimePolicy, role_models


@pytest.mark.parametrize("profile, actor_turns", [("standard", 6), ("pulp", 3)])
def test_world_and_models_defaults_per_profile(profile, actor_turns) -> None:
    policy = RuntimePolicy.for_profile(profile)
    assert policy.world.model_dump() == {
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


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_stored_policy_without_world_section_loads_defaults(profile) -> None:
    stored = json.loads(RuntimePolicy.for_profile(profile).model_dump_json())
    stored.pop("world", None)
    stored.pop("models", None)
    policy = RuntimePolicy.model_validate_json(json.dumps(stored))
    assert policy.schema_version == 2
    assert policy.world.conflict_delegate == "codex"
    assert policy.world.max_actor_turns_per_tick == 6
    assert policy.models.role_preferences == {"review.world_conflict": ("gpt-6-astra",)}


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
    assert policy.planning.use_llm_simulation is False
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
