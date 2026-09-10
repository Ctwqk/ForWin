"""Read-only, noncausal comparisons of frozen feedback and observed BODY evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime


def _object(raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _history(raw: object) -> tuple[list[dict], bool]:
    if raw in (None, "", "{}"):
        return [], False
    value = _object(raw)
    entries = value.get("observations")
    if (
        value.get("version") != 1
        or not isinstance(entries, list)
        or not all(isinstance(entry, dict) for entry in entries)
    ):
        return [], True
    return entries, False


def _time(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw))
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
    except (ValueError, TypeError):
        return None


def _sample_reasons(view: dict, label: str) -> set[str]:
    reasons = set()
    if not (
        view.get("source_qualified") is True
        and view.get("aggregate_id")
        and view.get("aggregation_version")
        and view.get("evidence_sha256")
    ):
        reasons.add(f"{label}_source_unqualified")
    total = int(view.get("total_comment_count") or 0)
    hits = int(view.get("hit_comment_count") or 0)
    # These are minimal descriptive sample requirements, not significance tests
    # or another consensus classifier. Missing/zero signal rows stay unknown.
    if total < 3:
        reasons.add("insufficient_comments")
    if int(view.get("known_author_count") or 0) < 2:
        reasons.add("insufficient_independent_authors")
    if int(view.get("analyzed_comment_count") or 0) != total:
        reasons.add("incomplete_analysis")
    if not 0 <= hits <= total:
        reasons.add("invalid_sample_counts")
    return reasons


def _body_matches(body: dict, after: dict) -> bool:
    scope = after.get("source_scope", {})
    number = int(body.get("chapter_number") or 0)
    if (
        not int(scope.get("chapter_start") or 0)
        <= number
        <= int(scope.get("chapter_end") or 0)
    ):
        return False
    return any(
        proof.get("state") == "published"
        and all(
            proof.get(key) == body.get(key)
            for key in (
                "canon_commit_id",
                "chapter_plan_id",
                "chapter_number",
                "content_sha256",
            )
        )
        for proof in after.get("publication_evidence", [])
    )


def _comparison_reasons(before: dict, after: dict, body: dict) -> set[str]:
    reasons = _sample_reasons(after, "after")
    first, second = before.get("source_scope", {}), after.get("source_scope", {})
    if int(second.get("chapter_start") or 0) <= int(first.get("chapter_end") or 0):
        reasons.add("overlapping_chapter_windows")
    before_ids = {item.get("comment_id") for item in before.get("input_comments", [])}
    after_ids = {item.get("comment_id") for item in after.get("input_comments", [])}
    if not before_ids or not after_ids or None in before_ids | after_ids:
        reasons.add("missing_comment_identity")
    if before_ids & after_ids:
        reasons.add("overlapping_comments")
    if not _body_matches(body, after):
        reasons.add("published_body_mismatch")
    committed = _time(body.get("committed_at"))
    if committed is None:
        reasons.add("unknown_body_time")
    for field in ("remote_created_at", "observed_at", "received_at"):
        a = first.get("timing", {}).get(field, {})
        b = second.get("timing", {}).get(field, {})
        a_start, a_end, b_start, b_end = (
            _time(item)
            for item in (a.get("start"), a.get("end"), b.get("start"), b.get("end"))
        )
        if (
            a.get("unknown_count", 1)
            or b.get("unknown_count", 1)
            or None in (a_start, a_end, b_start, b_end)
        ):
            reasons.add(f"unknown_{field}")
            continue
        if a_start > a_end or b_start > b_end:
            reasons.add(f"invalid_{field}")
        if b_start <= a_end:
            reasons.add(f"overlapping_{field}")
        if committed and b_start <= committed:
            reasons.add("feedback_precedes_body")
    return reasons


def _latest_window_snapshots(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Select chronology, not UUID order; an invalid latest snapshot stays latest."""
    windows = {}
    for view in candidates:
        scope = view.get("source_scope", {})
        key = (scope.get("chapter_start"), scope.get("chapter_end"))
        windows.setdefault(key, {})[view["aggregate_id"]] = view
    latest, excluded = [], []
    for by_id in windows.values():
        views = list(by_id.values())
        if len(views) == 1:
            latest.extend(views)
            continue
        times = [_time(view.get("snapshot_created_at")) for view in views]
        if None in times or times.count(max(times)) != 1:
            excluded.extend(
                {
                    "aggregate_id": view["aggregate_id"],
                    "reasons": ["ambiguous_after_snapshot_order"],
                }
                for view in views
            )
            continue
        chosen = views[times.index(max(times))]
        latest.append(chosen)
        excluded.extend(
            {
                "aggregate_id": view["aggregate_id"],
                "reasons": ["superseded_after_snapshot"],
            }
            for view in views
            if view is not chosen
        )
    return latest, excluded


def observe_action_signal_change(action, aggregate_views: list[dict]) -> dict:
    """Consume owner snapshots; never reclassify signals or map new actions.

    BODY observations are written by their evidence owner. Selected/proposed,
    plan application and attempted model input alone do not satisfy that stage.
    The original aggregate is the baseline even when late imports create another
    snapshot of the same chapter window. A missing after row is not a zero rate.
    """
    before = _object(action.aggregate_evidence_json)
    payload = _object(action.action_payload_json)
    result = {
        "action_id": action.id,
        "signal_key": action.signal_key,
        "signal_type": action.signal_type,
        "action_type": action.action_type,
        "triggered_at_chapter": action.triggered_at_chapter,
        "cooldown_until_chapter": action.cooldown_until_chapter,
        "before_aggregate_id": before.get("aggregate_id"),
        "after_aggregate_id": None,
        "before_rate": None,
        "after_rate": None,
        "delta": None,
        "outcome": "insufficient_data",
        "reasons": [],
        "causal_claim": False,
        "excluded_snapshots": [],
    }
    reasons = _sample_reasons(before, "before")
    if not (
        action.status == "selected"
        and action.source_qualified
        and before.get("aggregate_id") == action.aggregate_id
        and before.get("project_id") == action.project_id
        and before.get("signal_key") == action.signal_key
        and before.get("direction") == action.direction
    ):
        reasons.add("action_unqualified")
    desired = payload.get("desired_signal_change")
    if desired not in ("increase", "decrease", "observe"):
        reasons.add("unknown_desired_change")
    body_history, invalid_history = _history(action.body_observation_json)
    if invalid_history:
        reasons.add("invalid_body_observation_history")
    assessments = {}
    for body in body_history:
        if body.get("action_id") == action.id and body.get("assessment") in (
            "observed",
            "not_observed",
        ):
            identity = (body.get("canon_commit_id"), body.get("content_sha256"))
            assessments.setdefault(identity, set()).add(body["assessment"])
    if any(len(values) > 1 for values in assessments.values()):
        reasons.add("body_assessment_conflict")
    bodies = [
        body
        for body in body_history
        if body.get("action_id") == action.id
        and body.get("assessment") == "observed"
        and body.get("prompt_input_ids")
        and body.get("quotes")
        and body.get("canon_commit_id")
        and body.get("chapter_plan_id")
        and body.get("content_sha256")
        and action.target_chapter_start
        <= int(body.get("chapter_number") or 0)
        <= action.target_chapter_end
    ]
    if not bodies:
        reasons.add("body_application_unobserved")
    if reasons:
        result["reasons"] = sorted(reasons)
        return result
    result["before_rate"] = round(
        before["hit_comment_count"] / before["total_comment_count"], 6
    )
    candidates = [
        view
        for view in aggregate_views
        if view.get("aggregate_id") != before.get("aggregate_id")
        and all(
            view.get(key) == before.get(key)
            for key in (
                "project_id",
                "signal_key",
                "direction",
                "window_type",
                "aggregation_version",
            )
        )
        and int(view.get("source_scope", {}).get("chapter_end") or 0)
        > int(before.get("source_scope", {}).get("chapter_end") or 0)
    ]
    candidates, excluded = _latest_window_snapshots(candidates)
    result["excluded_snapshots"] = excluded
    candidates.sort(
        key=lambda view: (
            int(view.get("source_scope", {}).get("chapter_end") or 0),
            str(view.get("aggregate_id")),
        ),
        reverse=True,
    )
    failures = {
        reason
        for item in excluded
        for reason in item["reasons"]
        if reason != "superseded_after_snapshot"
    }
    for after in candidates:
        snapshot_failures = set()
        for body in bodies:
            problems = _comparison_reasons(before, after, body)
            if problems:
                failures.update(problems)
                snapshot_failures.update(problems)
                continue
            after_rate = round(
                after["hit_comment_count"] / after["total_comment_count"], 6
            )
            delta = round(after_rate - result["before_rate"], 6)
            if delta == 0:
                outcome = "observed_no_change"
            elif desired == "observe":
                outcome = "observed_change"
            elif (delta > 0) == (desired == "increase"):
                outcome = "associated_desired_change"
            else:
                outcome = "associated_opposite_change"
            return result | {
                "after_aggregate_id": after["aggregate_id"],
                "after_rate": after_rate,
                "delta": delta,
                "outcome": outcome,
                "reasons": [],
                "body_canon_commit_id": body["canon_commit_id"],
                "desired_signal_change": desired,
                "before_comment_count": before["total_comment_count"],
                "after_comment_count": after["total_comment_count"],
            }
        result["excluded_snapshots"].append(
            {
                "aggregate_id": after["aggregate_id"],
                "reasons": sorted(snapshot_failures),
            }
        )
    result["reasons"] = sorted(failures or {"no_comparable_after_snapshot"})
    return result


def record_signal_change_observations(
    session, *, project_id: str, chapter_number: int, aggregate_views: list[dict]
) -> list[dict]:
    """Save bounded post-Canon observations in the caller's existing transaction."""
    from sqlalchemy import select

    from forwin.canon.projection_lock import lock_projection_project
    from forwin.models import FeedbackActionRecord

    with session.no_autoflush:
        for row in set(session.new) | set(session.dirty) | set(session.deleted):
            if (
                isinstance(row, FeedbackActionRecord)
                and row.project_id == project_id
                and (
                    row in session.new
                    or row in session.deleted
                    or session.is_modified(row)
                )
            ):
                raise ValueError("effect observations require flushed action inputs")
        lock_projection_project(session, project_id)
        records = session.scalars(
            select(FeedbackActionRecord)
            .where(
                FeedbackActionRecord.project_id == project_id,
                FeedbackActionRecord.status == "selected",
            )
            .order_by(FeedbackActionRecord.created_at.desc(), FeedbackActionRecord.id)
            .limit(8)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    results = []
    for record in records:
        observation = observe_action_signal_change(record, aggregate_views) | {
            "observed_at_chapter": chapter_number,
        }
        entries, invalid_history = _history(record.effect_observation_json)
        # An unreadable saved audit must remain available for repair/inspection.
        if invalid_history:
            results.append(
                {
                    "action_id": record.id,
                    "outcome": "insufficient_data",
                    "reasons": ["invalid_effect_observation_history"],
                    "causal_claim": False,
                }
            )
            continue
        if observation not in entries:
            record.effect_observation_json = json.dumps(
                {"version": 1, "observations": [*entries, observation]},
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        results.append(observation)
    session.flush()
    return results
