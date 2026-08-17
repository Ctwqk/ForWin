import pytest
from pydantic import ValidationError

from forwin.runtime.policy import RuntimePolicy


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
    assert policy.canon.book_state_layers == ("world", "map", "cognition", "narrative")
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
    assert policy.canon.book_state_layers == ("world",)


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
