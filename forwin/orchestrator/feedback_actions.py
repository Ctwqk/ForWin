"""Phase C of the Audience Feedback Layer.

ActionMapper: translates confirmed signals into concrete system actions.
AudienceHintPack builder: assembles the minimal hint set that Writer sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Sequence

from sqlalchemy.orm import Session

from forwin.models import SignalWindowAggregate
from forwin.orchestrator.feedback_aggregator import FeedbackCooldown

logger = logging.getLogger(__name__)

# ── Response windows (chapters ahead) per signal type ────────────────

_RESPONSE_WINDOWS: dict[str, int] = {
    "risk": 3,
    "pacing": 5,
    "confusion": 10,
    "character_heat": 10,
}

# ── Action types per signal type + level ─────────────────────────────

_ACTION_MAP: dict[str, dict[str, str]] = {
    "risk": {
        "watchlist": "monitor_risk",
        "confirmed": "patch_current_band",
    },
    "pacing": {
        "confirmed": "reband_candidate",
    },
    "confusion": {
        "confirmed": "clarification_backlog",
    },
    "character_heat": {
        "confirmed": "boost_future_band",
    },
}


@dataclass(slots=True)
class FeedbackAction:
    """A concrete action recommendation derived from an audience signal."""
    signal_key: str
    signal_type: str
    target_name: str
    action_type: str
    severity: int
    level: str
    response_window: int
    description: str


@dataclass(slots=True)
class AudienceHintPack:
    """Minimal, Writer-safe hint set distilled from audience signals.

    Writer sees ONLY this — never raw comments, never full signal details.
    """
    pacing_hints: list[str] = field(default_factory=list)
    clarity_hints: list[str] = field(default_factory=list)
    character_heat_changes: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


class ActionMapper:
    """Maps confirmed audience signals to system actions and Writer hints."""

    def map_actions(
        self,
        actionable: Sequence[SignalWindowAggregate],
    ) -> list[FeedbackAction]:
        """Convert actionable aggregates into FeedbackAction recommendations."""
        actions: list[FeedbackAction] = []
        seen_keys: set[str] = set()

        # Sort by severity desc so strongest signals come first
        sorted_aggs = sorted(actionable, key=lambda a: (-a.max_severity, a.signal_key))

        for agg in sorted_aggs:
            if agg.signal_key in seen_keys:
                continue
            seen_keys.add(agg.signal_key)

            type_map = _ACTION_MAP.get(agg.signal_type, {})
            action_type = type_map.get(agg.signal_level)
            if not action_type:
                continue

            response_window = _RESPONSE_WINDOWS.get(agg.signal_type, 5)
            description = _build_action_description(agg, action_type)

            actions.append(FeedbackAction(
                signal_key=agg.signal_key,
                signal_type=agg.signal_type,
                target_name=agg.target_name,
                action_type=action_type,
                severity=agg.max_severity,
                level=agg.signal_level,
                response_window=response_window,
                description=description,
            ))

        return actions

    def build_hint_pack(
        self,
        actions: Sequence[FeedbackAction],
    ) -> AudienceHintPack:
        """Distill actions into the minimal hint set that Writer sees."""
        pack = AudienceHintPack()

        for action in actions:
            hint = action.description
            if action.signal_type == "pacing":
                pack.pacing_hints.append(hint)
            elif action.signal_type == "confusion":
                pack.clarity_hints.append(hint)
            elif action.signal_type == "character_heat":
                pack.character_heat_changes.append(hint)
            elif action.signal_type == "risk":
                pack.risk_flags.append(hint)

        # Cap each category to prevent prompt bloat
        pack.pacing_hints = pack.pacing_hints[:3]
        pack.clarity_hints = pack.clarity_hints[:3]
        pack.character_heat_changes = pack.character_heat_changes[:3]
        pack.risk_flags = pack.risk_flags[:3]

        return pack

    def record_actions(
        self,
        session: Session,
        *,
        project_id: str,
        chapter_number: int,
        actions: Sequence[FeedbackAction],
        cooldown: FeedbackCooldown,
    ) -> None:
        """Record that actions were taken, starting cooldowns."""
        for action in actions:
            cooldown.record_action(
                session,
                project_id=project_id,
                signal_key=action.signal_key,
                signal_type=action.signal_type,
                action_type=action.action_type,
                chapter_number=chapter_number,
                notes=action.description,
            )


def _build_action_description(agg: SignalWindowAggregate, action_type: str) -> str:
    """Build a human-readable Chinese description for Writer hints."""
    target = agg.target_name or "整体"
    window = f"{agg.window_chapter_start}-{agg.window_chapter_end}章"
    users = agg.unique_user_count

    if agg.signal_type == "risk":
        if action_type == "patch_current_band":
            return f"读者指出[{target}]存在风险({window}, {users}人), 建议在近1-3章自然修补"
        return f"[{target}]收到风险预警({window}), 持续关注"

    if agg.signal_type == "pacing":
        if action_type == "reband_candidate":
            return f"读者反馈节奏问题({window}, {users}人), 建议调整近端计划"
        return f"节奏信号待观察({window})"

    if agg.signal_type == "confusion":
        return f"[{target}]有待放清({window}), 可在后续情节中自然补充"

    if agg.signal_type == "character_heat":
        if action_type == "boost_future_band":
            return f"[{target}]持续受关注({window}, {users}人), 可适当增加后续出场"
        return f"[{target}]热度上升({window}), 保持关注"

    return f"{agg.signal_type}:{target}({window})"


def build_audience_hint_pack_from_aggregates(
    session: Session,
    project_id: str,
    chapter_number: int,
    *,
    actionable: Sequence[SignalWindowAggregate],
    cooldown: FeedbackCooldown,
) -> AudienceHintPack:
    """Full Phase C pipeline: map → hints → record cooldowns.

    Returns the AudienceHintPack for Writer consumption.
    """
    mapper = ActionMapper()
    actions = mapper.map_actions(actionable)
    pack = mapper.build_hint_pack(actions)

    if actions:
        mapper.record_actions(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            actions=actions,
            cooldown=cooldown,
        )
        logger.info(
            "Phase C: %d actions mapped, hints: pacing=%d clarity=%d heat=%d risk=%d",
            len(actions),
            len(pack.pacing_hints),
            len(pack.clarity_hints),
            len(pack.character_heat_changes),
            len(pack.risk_flags),
        )

    return pack
