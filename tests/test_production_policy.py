from __future__ import annotations

from forwin.api_project_payloads import normalize_project_automation
from forwin.production.policy import policy_from_automation


def test_policy_maps_legacy_daily_chapter_quota_to_write_quota() -> None:
    automation = normalize_project_automation(
        {
            "enabled": True,
            "daily_start_time": "7:5",
            "daily_chapter_quota": 3,
            "publish": {
                "platform": "fanqie",
                "book_name": "旧绑定",
                "create_if_missing": True,
            },
        }
    )

    policy = policy_from_automation(automation)

    assert policy.enabled is True
    assert policy.daily_start_time == "07:05"
    assert policy.quota.write == 3
    assert policy.quota.plan == 0
    assert policy.quota.review == 0
    assert policy.quota.publish == 0
    assert [binding.platform for binding in policy.publish_bindings] == ["fanqie"]


def test_policy_prefers_new_quota_fields_and_clamps_bounds() -> None:
    automation = normalize_project_automation(
        {
            "enabled": True,
            "daily_chapter_quota": 4,
            "daily_plan_quota": 2,
            "daily_write_quota": 25,
            "daily_review_quota": 3,
            "daily_publish_quota": 30,
            "stop_when_review_pending": False,
            "auto_publish": True,
            "publish_bindings": [
                {"platform": "qidian", "book_name": "起点版"},
                {"platform": "fanqie", "book_name": "番茄版"},
            ],
        }
    )

    policy = policy_from_automation(automation)

    assert policy.quota.plan == 2
    assert policy.quota.write == 20
    assert policy.quota.review == 3
    assert policy.quota.publish == 20
    assert policy.stop_when_review_pending is False
    assert policy.auto_publish is True
    assert [binding.platform for binding in policy.publish_bindings] == ["qidian", "fanqie"]


def test_policy_auto_publish_backfills_publish_quota_when_missing() -> None:
    automation = normalize_project_automation(
        {
            "daily_chapter_quota": 2,
            "daily_publish_quota": 0,
            "auto_publish": True,
            "publish": {"platform": "fanqie", "book_name": "番茄版"},
        }
    )

    policy = policy_from_automation(automation)

    assert policy.quota.write == 2
    assert policy.quota.publish == 1


def test_policy_carries_long_run_policy() -> None:
    automation = normalize_project_automation(
        {
            "long_run_policy": {
                "mode": "factory_batch",
                "batch_size": 12,
                "defer_observation_failures": True,
            },
        }
    )

    policy = policy_from_automation(automation)

    assert policy.long_run_policy.mode == "factory_batch"
    assert policy.long_run_policy.batch_size == 12
    assert policy.long_run_policy.defer_observation_failures is True
