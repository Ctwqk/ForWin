from __future__ import annotations

from forwin.long_run_policy import LongRunMode, LongRunPolicy, normalize_long_run_policy


def test_normalize_defaults_to_daily_serial() -> None:
    policy = normalize_long_run_policy({})

    assert policy == LongRunPolicy()
    assert policy.mode == LongRunMode.daily_serial
    assert policy.batch_size == 1
    assert policy.stop_on_chapter_failure is True
    assert policy.defer_observation_failures is False
    assert policy.payoff_gap_limit == 2
    assert policy.resume_policy == "manual_after_failed_chapter"


def test_normalize_factory_batch_clamps_batch_size() -> None:
    policy = normalize_long_run_policy(
        {
            "mode": "factory_batch",
            "batch_size": 999,
            "defer_observation_failures": True,
            "payoff_gap_limit": 5,
            "resume_policy": "auto_after_infrastructure_failure",
        }
    )

    assert policy.mode == LongRunMode.factory_batch
    assert policy.batch_size == 50
    assert policy.defer_observation_failures is True
    assert policy.payoff_gap_limit == 5
    assert policy.resume_policy == "auto_after_infrastructure_failure"
