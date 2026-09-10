from __future__ import annotations

import importlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest


def _compare(action, after):
    module = importlib.import_module("forwin.audience.effects")
    return module.observe_action_signal_change(action, after)


def _view(name, start, end, hits, day):
    return {
        "project_id": "book",
        "aggregate_id": name,
        "snapshot_created_at": f"2026-09-{day:02d}T03:00:00+00:00",
        "aggregation_version": "comment-window-v2",
        "evidence_sha256": name * 64,
        "signal_key": '["pacing","arc","pace","too_slow"]',
        "signal_type": "pacing",
        "direction": "too_slow",
        "window_type": "short",
        "source_qualified": True,
        "qualification_reasons": [],
        "total_comment_count": 10,
        "analyzed_comment_count": 10,
        "known_author_count": 3,
        "hit_comment_count": hits,
        "input_comments": [{"comment_id": f"{name}-{i}"} for i in range(10)],
        "source_scope": {
            "chapter_start": start,
            "chapter_end": end,
            "timing": {
                field: {
                    "start": f"2026-09-{day:02d}T01:00:00+00:00",
                    "end": f"2026-09-{day:02d}T02:00:00+00:00",
                    "unknown_count": 0,
                }
                for field in ("remote_created_at", "observed_at", "received_at")
            },
        },
        "publication_evidence": [
            {
                "id": "publication",
                "canon_commit_id": "canon-3",
                "chapter_plan_id": "chapter-3",
                "chapter_number": 3,
                "content_sha256": "d" * 64,
                "state": "published",
            }
        ],
    }


@pytest.fixture
def case():
    before = _view("a", 1, 2, 8, 1)
    after = _view("b", 3, 4, 3, 3)
    action = SimpleNamespace(
        id="action",
        project_id="book",
        aggregate_id="a",
        signal_key=before["signal_key"],
        signal_type="pacing",
        direction="too_slow",
        action_type="shorten_reward_gap",
        status="selected",
        source_qualified=True,
        triggered_at_chapter=2,
        target_chapter_start=3,
        target_chapter_end=7,
        cooldown_until_chapter=5,
        aggregate_evidence_json=json.dumps(before),
        action_payload_json=json.dumps({"desired_signal_change": "decrease"}),
        body_observation_json=json.dumps(
            {
                "version": 1,
                "observations": [
                    {
                        "action_id": "action",
                        "assessment": "observed",
                        "canon_commit_id": "canon-3",
                        "chapter_plan_id": "chapter-3",
                        "chapter_number": 3,
                        "content_sha256": "d" * 64,
                        "committed_at": "2026-09-02T01:00:00+00:00",
                        "prompt_input_ids": ["input-1"],
                        "quotes": [{"start": 0, "end": 4, "text": "打开信封"}],
                    }
                ],
            }
        ),
    )
    return action, after


def test_observes_prevalence_association_from_frozen_before_and_actual_body(case):
    action, after = case
    result = _compare(action, [after])
    assert result["outcome"] == "associated_desired_change"
    assert result["before_rate"] == 0.8 and result["after_rate"] == 0.3
    assert result["delta"] == -0.5
    assert result["before_aggregate_id"] == "a" and result["after_aggregate_id"] == "b"
    assert result["causal_claim"] is False


def test_positive_interest_uses_mapper_direction_not_lower_is_better(case):
    action, after = case
    action.action_payload_json = json.dumps({"desired_signal_change": "increase"})
    assert _compare(action, [after])["outcome"] == "associated_opposite_change"


def test_observation_only_action_does_not_claim_improvement(case):
    action, after = case
    action.action_payload_json = json.dumps({"desired_signal_change": "observe"})
    assert _compare(action, [after])["outcome"] == "observed_change"


@pytest.mark.parametrize("body", [{}, {"observations": [{"assessment": "unknown"}]}])
def test_prompt_or_plan_only_is_not_body_application(case, body):
    action, after = case
    action.body_observation_json = json.dumps(body)
    result = _compare(action, [after])
    assert result["outcome"] == "insufficient_data"
    assert "body_application_unobserved" in result["reasons"]


def test_no_after_signal_is_unknown_not_zero_or_improved(case):
    action, _ = case
    result = _compare(action, [])
    assert result["outcome"] == "insufficient_data"
    assert result["after_rate"] is None


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda v: v.update(source_qualified=False), "after_source_unqualified"),
        (lambda v: v.update(known_author_count=1), "insufficient_independent_authors"),
        (lambda v: v.update(analyzed_comment_count=9), "incomplete_analysis"),
        (
            lambda v: v.update(total_comment_count=2, hit_comment_count=2),
            "insufficient_comments",
        ),
        (
            lambda v: v["source_scope"].update(chapter_start=2),
            "overlapping_chapter_windows",
        ),
        (
            lambda v: v["input_comments"].append({"comment_id": "a-0"}),
            "overlapping_comments",
        ),
        (
            lambda v: v["publication_evidence"][0].update(canon_commit_id="other"),
            "published_body_mismatch",
        ),
        (
            lambda v: v["publication_evidence"][0].update(content_sha256="x" * 64),
            "published_body_mismatch",
        ),
        (
            lambda v: v["source_scope"]["timing"]["remote_created_at"].update(
                start="2026-09-01T01:00:00+00:00"
            ),
            "overlapping_remote_created_at",
        ),
        (
            lambda v: v["source_scope"]["timing"]["observed_at"].update(
                unknown_count=1
            ),
            "unknown_observed_at",
        ),
    ],
)
def test_ambiguous_windows_have_explicit_noncausal_reason(case, mutation, reason):
    action, after = case
    mutation(after)
    result = _compare(action, [after])
    assert result["outcome"] == "insufficient_data"
    assert reason in result["reasons"]


def test_late_before_backfill_cannot_move_baseline_to_latest_snapshot(case):
    action, after = case
    late_before = _view("c", 1, 2, 1, 4)
    result = _compare(action, [late_before, after])
    assert result["before_aggregate_id"] == "a"
    assert result["before_rate"] == 0.8


def test_later_snapshot_does_not_hide_incompatible_observed_publication(case):
    action, after = case
    newer = deepcopy(after)
    newer["aggregate_id"] = "c"
    newer["source_scope"].update(chapter_start=5, chapter_end=6)
    newer["publication_evidence"][0]["canon_commit_id"] = "another-canon"
    result = _compare(action, [after, newer])
    assert result["after_aggregate_id"] == "b"


def test_unqualified_legacy_record_never_gains_effect_from_new_aggregates(case):
    action, after = case
    action.source_qualified = False
    result = _compare(action, [after])
    assert result["outcome"] == "insufficient_data"
    assert "action_unqualified" in result["reasons"]


def test_conflicting_body_assessments_cannot_cherry_pick_observed(case):
    action, after = case
    history = json.loads(action.body_observation_json)
    denied = deepcopy(history["observations"][0])
    denied["assessment"] = "not_observed"
    history["observations"].append(denied)
    action.body_observation_json = json.dumps(history)
    result = _compare(action, [after])
    assert result["outcome"] == "insufficient_data"
    assert "body_assessment_conflict" in result["reasons"]


def test_newest_after_snapshot_wins_over_lexicographically_larger_old_id(case):
    action, after = case
    older = deepcopy(after)
    older["aggregate_id"] = "zz-old"
    older["hit_comment_count"] = 9
    older["snapshot_created_at"] = "2026-09-03T02:30:00+00:00"
    result = _compare(action, [after, older])
    assert result["after_aggregate_id"] == "b"
    assert result["after_rate"] == 0.3


def test_unqualified_newest_after_cannot_fall_back_to_stale_good_snapshot(case):
    action, after = case
    latest = deepcopy(after)
    latest["aggregate_id"] = "zz-latest"
    latest["snapshot_created_at"] = "2026-09-04T00:00:00+00:00"
    latest["source_qualified"] = False
    result = _compare(action, [after, latest])
    assert result["outcome"] == "insufficient_data"
    assert "after_source_unqualified" in result["reasons"]


@pytest.mark.parametrize("timestamp", [None, "2026-09-03T03:00:00+00:00"])
def test_unknown_or_tied_after_snapshot_order_is_explicit(case, timestamp):
    action, after = case
    other = deepcopy(after)
    other["aggregate_id"] = "other"
    other["snapshot_created_at"] = timestamp
    other["hit_comment_count"] = 9
    result = _compare(action, [after, other])
    assert result["outcome"] == "insufficient_data"
    assert "ambiguous_after_snapshot_order" in result["reasons"]
