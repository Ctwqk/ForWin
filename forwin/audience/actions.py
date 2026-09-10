"""Qualified aggregate decisions and finite, independently evidenced action stages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audience.feedback import FeedbackCooldown
from forwin.canon.projection_lock import lock_projection_project
from forwin.models import FeedbackActionRecord, SignalWindowAggregate
from forwin.protocol.context import AudienceHintItem, AudienceHintView

_RESPONSE_WINDOWS = {
    "risk": 3,
    "pacing": 5,
    "confusion": 10,
    "character_heat": 10,
    "relationship_interest": 10,
    "prediction": 6,
}
_HINT_VALID_CHAPTERS = 3
_OPPOSITES = {
    "pacing": frozenset({"too_slow", "too_fast"}),
    "confusion": frozenset({"unclear", "clear"}),
    "character_heat": frozenset({"positive", "negative"}),
    "relationship_interest": frozenset({"want_more", "want_less"}),
}
_CATEGORY = {
    "pacing": "pacing",
    "confusion": "clarity",
    "character_heat": "character_heat",
    "relationship_interest": "character_heat",
    "risk": "risk",
    "prediction": "prediction",
}
# Action type, bounded Writer suggestion, optional future-plan field. Prediction
# stays an observation of reader expectations, never an instruction to fulfil it.
_DECISIONS = {
    ("pacing", "too_slow"): (
        "shorten_reward_gap",
        "适当推进线索或兑现既定回报，避免重复铺垫",
        "progress_markers",
        "decrease",
    ),
    ("pacing", "too_fast"): (
        "allow_consequence_space",
        "给关键行动的后果、因果与情绪反应留出叙述空间",
        "immersion_anchors",
        "decrease",
    ),
    ("pacing", "balanced"): (
        "observe_balanced_pacing",
        "节奏反馈平衡，沿既定计划保持当前推进方式",
        "",
        "observe",
    ),
    ("confusion", "unclear"): (
        "clarify_rule_legibility",
        "在既定情节中补足规则边界和关键因果的可理解线索",
        "rule_anchors",
        "decrease",
    ),
    ("confusion", "clear"): (
        "observe_clear_rules",
        "可理解性反馈正向，保持既定信息边界",
        "",
        "observe",
    ),
    ("character_heat", "positive"): (
        "preserve_character_interest",
        "保留角色已建立的动机与行动连贯性",
        "immersion_anchors",
        "increase",
    ),
    ("character_heat", "negative"): (
        "clarify_character_motivation",
        "核对角色动机与行为后果的表达，不将负面反馈当成热度奖励",
        "immersion_anchors",
        "decrease",
    ),
    ("relationship_interest", "want_more"): (
        "develop_planned_relationship",
        "在既定关系走向内呈现有意义的互动，不改写根设定",
        "progress_markers",
        "observe",
    ),
    ("relationship_interest", "want_less"): (
        "rebalance_relationship_space",
        "避免重复关系铺陈，为本章既定目标保留空间",
        "progress_markers",
        "observe",
    ),
    ("risk", "concern"): (
        "repair_immersion",
        "核对既定规则、因果与代价，避免沉浸感被无依据的变化打断",
        "rule_anchors",
        "decrease",
    ),
    ("prediction", "predicts"): (
        "observe_reader_prediction",
        "这是读者预测的观察，不是创作命令；遵循既定因果，勿为迎合或反转猜测硬改剧情",
        "",
        "observe",
    ),
}


@dataclass(frozen=True, slots=True)
class FeedbackAction:
    signal_key: str
    signal_type: str
    target_name: str
    action_type: str
    severity: int
    level: str
    response_window: int
    description: str
    direction: str
    aggregate_id: str
    aggregate_evidence: dict
    plan_field: str = ""
    desired_signal_change: str = "observe"
    decision_reason: str = ""
    conflicting_aggregates: tuple[dict, ...] = ()


def _view(aggregate: SignalWindowAggregate | dict) -> dict:
    if isinstance(aggregate, dict):
        return deepcopy(aggregate)
    from forwin.audience.aggregation import aggregate_view

    return aggregate_view(aggregate)


def action_is_qualified(record: FeedbackActionRecord) -> bool:
    """Check the frozen owner decision, without re-aggregating its evidence."""
    if not record.source_qualified:
        return False
    try:
        evidence = json.loads(record.aggregate_evidence_json or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(evidence, dict):
        return False
    return bool(
        evidence.get("source_qualified") is True
        and evidence.get("aggregate_id") == record.aggregate_id
        and evidence.get("project_id") == record.project_id
        and evidence.get("signal_key") == record.signal_key
        and evidence.get("signal_type") == record.signal_type
        and evidence.get("direction") == record.direction
        and evidence.get("aggregation_version")
        and evidence.get("evidence_sha256")
    )


def _conflicting_aggregates(view: dict, views: list[dict]) -> tuple[dict, ...]:
    """Opposite qualified decisions with overlapping source chapters stay observable."""
    directions = _OPPOSITES.get(view.get("signal_type"), frozenset())
    if (
        view.get("direction") not in directions
        or view.get("signal_level", view.get("level")) != "confirmed"
    ):
        return ()
    scope = view.get("source_scope", {})
    peers = []
    for other in views:
        if (
            other.get("source_qualified") is not True
            or other.get("signal_level", other.get("level")) != "confirmed"
            or other.get("direction") not in directions
            or other.get("direction") == view.get("direction")
            or any(
                other.get(key) != view.get(key)
                for key in ("project_id", "signal_type", "target_type", "target_name")
            )
        ):
            continue
        other_scope = other.get("source_scope", {})
        if max(
            scope.get("chapter_start", 0), other_scope.get("chapter_start", 0)
        ) <= min(scope.get("chapter_end", -1), other_scope.get("chapter_end", -1)):
            peers.append(other)
    if not peers:
        return ()
    unique = {item["aggregate_id"]: item for item in [view, *peers]}
    return tuple(
        {
            key: item[key]
            for key in ("aggregate_id", "aggregation_version", "evidence_sha256")
        }
        for _id, item in sorted(unique.items())
    )


def action_hint(record: FeedbackActionRecord) -> AudienceHintItem:
    payload = json.loads(record.action_payload_json or "{}")
    return AudienceHintItem(action_id=record.id, **payload["hint"])


def action_hint_available(record: FeedbackActionRecord, chapter_number: int) -> bool:
    return bool(
        record.status == "selected"
        and action_is_qualified(record)
        and record.selected_at_chapter is not None
        and record.selected_at_chapter < chapter_number
        and record.hint_valid_from_chapter
        <= chapter_number
        <= record.hint_expires_at_chapter
        and record.target_chapter_start <= chapter_number <= record.target_chapter_end
    )


class ActionMapper:
    """The only direction-to-action mapping; selection is distinct from proposal."""

    def map_actions(
        self, actionable: Sequence[SignalWindowAggregate | dict]
    ) -> list[FeedbackAction]:
        actions = []
        seen = set()
        views = [_view(item) for item in actionable]
        # Ranking consumes the owner's counters/level, never recalculates consensus.
        views.sort(
            key=lambda view: (
                -int(view.get("max_severity", 0)),
                -int(view.get("hit_comment_count", 0)),
                str(view.get("signal_key", "")),
            )
        )
        for view in views:
            signal_type = str(view.get("signal_type", ""))
            direction = str(view.get("direction", "unknown"))
            level = str(view.get("signal_level", view.get("level", "noise")))
            key = str(view.get("signal_key", ""))
            decision = _DECISIONS.get((signal_type, direction))
            if not (
                view.get("source_qualified") is True
                and view.get("project_id")
                and view.get("aggregate_id")
                and view.get("aggregation_version")
                and view.get("evidence_sha256")
                and key
                and decision
            ):
                continue
            if level != "confirmed" and not (
                signal_type == "risk" and level == "watchlist"
            ):
                continue
            if key in seen:
                continue
            seen.add(key)
            action_type, instruction, field, desired_change = decision
            decision_reason = ""
            if signal_type == "risk" and level == "watchlist":
                action_type = "observe_risk_watchlist"
                instruction = "风险信号尚未形成多作者共识；仅保留风险观察，沿既定计划继续，不据此发起修复或改纲"
                field, desired_change = "", "observe"
                decision_reason = "watchlist_observation"
            conflicts = _conflicting_aggregates(view, views)
            if conflicts:
                action_type = "observe_conflicting_directions"
                instruction = "来源章窗重叠的读者反馈方向相反；仅记录分歧，保留既定计划并观察后续变化"
                field, desired_change = "", "observe"
                decision_reason = "conflicting_directions"
            target = str(view.get("target_name", view.get("target", "")) or "整体")[
                :120
            ]
            description = f"关于「{target}」：{instruction}。"
            actions.append(
                FeedbackAction(
                    key,
                    signal_type,
                    target,
                    action_type,
                    int(view.get("max_severity", 0)),
                    level,
                    _RESPONSE_WINDOWS[signal_type],
                    description,
                    direction,
                    str(view["aggregate_id"]),
                    deepcopy(view),
                    field,
                    desired_change,
                    decision_reason,
                    conflicts,
                )
            )
        return actions

    def record_actions(
        self,
        session: Session,
        *,
        project_id: str,
        chapter_number: int,
        actions: Sequence[FeedbackAction],
        cooldown: FeedbackCooldown,
    ) -> list[FeedbackActionRecord]:
        """Persist idempotent proposals; no selection or cooldown is implied."""
        for action in actions:
            evidence = action.aggregate_evidence
            if any(
                evidence.get(key) != value
                for key, value in {
                    "project_id": project_id,
                    "signal_key": action.signal_key,
                    "signal_type": action.signal_type,
                    "direction": action.direction,
                    "aggregate_id": action.aggregate_id,
                }.items()
            ):
                raise ValueError("feedback aggregate action identity mismatch")
        lock_projection_project(session, project_id)
        records = []
        for action in actions:
            record_id = uuid5(
                NAMESPACE_URL,
                f"forwin:feedback-action:{project_id}:{action.aggregate_id}:{action.action_type}",
            ).hex
            record = session.get(FeedbackActionRecord, record_id)
            if record is None:
                payload = {
                    "hint": {
                        "category": _CATEGORY[action.signal_type],
                        "text": action.description,
                    },
                    "desired_signal_change": action.desired_signal_change,
                }
                if action.plan_field:
                    payload["plan_hint"] = {
                        "field": action.plan_field,
                        "text": action.description,
                    }
                if action.decision_reason:
                    payload["decision_reason"] = action.decision_reason
                if action.conflicting_aggregates:
                    payload["conflicting_aggregates"] = list(
                        action.conflicting_aggregates
                    )
                record = FeedbackActionRecord(
                    id=record_id,
                    project_id=project_id,
                    signal_key=action.signal_key,
                    signal_type=action.signal_type,
                    action_type=action.action_type,
                    direction=action.direction,
                    aggregate_id=action.aggregate_id,
                    aggregate_evidence_json=json.dumps(
                        action.aggregate_evidence, ensure_ascii=False, sort_keys=True
                    ),
                    source_qualified=True,
                    status="proposed",
                    triggered_at_chapter=chapter_number,
                    cooldown_until_chapter=0,
                    target_chapter_start=chapter_number + 1,
                    target_chapter_end=chapter_number + action.response_window,
                    hint_valid_from_chapter=chapter_number + 1,
                    hint_expires_at_chapter=chapter_number + _HINT_VALID_CHAPTERS,
                    notes=action.description,
                    action_payload_json=json.dumps(payload, ensure_ascii=False),
                )
                session.add(record)
            records.append(record)
        session.flush()
        return records

    def select_actions(
        self,
        session: Session,
        *,
        project_id: str,
        chapter_number: int,
        action_ids: Sequence[str],
        cooldown: FeedbackCooldown,
    ) -> AudienceHintView:
        """Select a bounded hint set under the project lock, then start cooldown."""
        lock_projection_project(session, project_id)
        records = list(
            session.scalars(
                select(FeedbackActionRecord)
                .where(
                    FeedbackActionRecord.project_id == project_id,
                    FeedbackActionRecord.id.in_(action_ids),
                )
                .order_by(FeedbackActionRecord.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        by_id = {row.id: row for row in records}
        eligible = []
        selected_keys = set()
        for action_id in dict.fromkeys(action_ids):
            record = by_id.get(action_id)
            if record is None or not action_is_qualified(record):
                continue
            if record.signal_key in selected_keys:
                continue
            if not (
                record.hint_valid_from_chapter
                <= chapter_number + 1
                <= record.hint_expires_at_chapter
                and record.target_chapter_start
                <= chapter_number + 1
                <= record.target_chapter_end
            ):
                continue
            if record.status == "selected" or (
                record.status == "proposed"
                and cooldown.is_cooled(
                    session, project_id, record.signal_key, chapter_number
                )
            ):
                eligible.append(action_hint(record))
                selected_keys.add(record.signal_key)
        pack = AudienceHintView(items=eligible).clipped()
        for hint in pack.items:
            record = by_id[hint.action_id]
            if record.status == "selected":
                continue
            record.status = "selected"
            record.selected_at_chapter = chapter_number
            record.selected_at = datetime.now(UTC).replace(tzinfo=None)
            record.cooldown_until_chapter = chapter_number + cooldown.cooldown_chapters
        session.flush()
        return pack


def build_audience_hint_pack_from_aggregates(
    session: Session,
    project_id: str,
    chapter_number: int,
    *,
    actionable: Sequence[SignalWindowAggregate | dict],
    cooldown: FeedbackCooldown,
) -> AudienceHintView:
    mapper = ActionMapper()
    records = mapper.record_actions(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        actions=mapper.map_actions(actionable),
        cooldown=cooldown,
    )
    return mapper.select_actions(
        session,
        project_id=project_id,
        chapter_number=chapter_number,
        action_ids=[row.id for row in records],
        cooldown=cooldown,
    )


def record_prompt_inclusions(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
    prompt_trace_id: str,
    feedback_inputs: list[dict],
) -> None:
    """Link actual adapter-input evidence to selected actions in the trace transaction.

    This records an attempted input, including adapter failures. Neither a returned
    adapter call nor a prompt inclusion proves provider exactly-once or body effect.
    """
    from forwin.writer.feedback_input import text_sha256

    if not feedback_inputs:
        return
    lock_projection_project(session, project_id)
    action_ids = {
        hint["action_id"]
        for event in feedback_inputs
        for hint in event.get("hints", [])
    }
    rows = session.scalars(
        select(FeedbackActionRecord)
        .where(
            FeedbackActionRecord.project_id == project_id,
            FeedbackActionRecord.id.in_(sorted(action_ids)),
        )
        .order_by(FeedbackActionRecord.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for row in rows:
        if not action_hint_available(row, chapter_number):
            continue
        digest = text_sha256(action_hint(row).text)
        existing = json.loads(row.prompt_inclusions_json or "[]")
        seen = {item.get("input_id") for item in existing}
        for event in feedback_inputs:
            if (
                event.get("input_status") != "attempted_input"
                or not event.get("input_id")
                or event["input_id"] in seen
            ):
                continue
            if not any(
                hint.get("action_id") == row.id and hint.get("hint_sha256") == digest
                for hint in event.get("hints", [])
            ):
                continue
            existing.append(
                {
                    key: event[key]
                    for key in (
                        "input_id",
                        "input_status",
                        "adapter_outcome",
                        "messages_sha256",
                        "stage_key",
                        "exception_type",
                    )
                    if key in event
                }
                | {
                    "hint_sha256": digest,
                    "prompt_trace_id": prompt_trace_id,
                    "chapter_number": chapter_number,
                }
            )
            seen.add(event["input_id"])
        row.prompt_inclusions_json = json.dumps(existing, ensure_ascii=False)
    session.flush()
