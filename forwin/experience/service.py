from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audience_metrics import derive_audience_trends
from forwin.models.publisher import SignalWindowAggregate


@dataclass(slots=True)
class AudienceCalibrationProfile:
    boost_reward_density: bool = False
    clarify_rule_legibility: bool = False
    protect_character_heat: bool = False
    hold_managed_ambiguity: bool = False
    favor_visible_payoff: bool = False
    reduce_setup_ratio: bool = False
    boost_status_payoff: bool = False
    avoid_trope_categories: list[str] | None = None
    progression_blocked_template_ids: list[str] | None = None
    progression_blocked_categories: list[str] | None = None
    recent_template_ids: list[str] | None = None
    recent_trope_categories: list[str] | None = None


def load_long_window_audience_trends(
    session: Session,
    project_id: str,
    *,
    limit: int = 3,
) -> list[str]:
    rows = session.execute(
        select(SignalWindowAggregate)
        .where(
            SignalWindowAggregate.project_id == project_id,
            SignalWindowAggregate.window_type == "long",
            SignalWindowAggregate.signal_level.in_(("confirmed", "watchlist")),
        )
        .order_by(
            SignalWindowAggregate.window_chapter_end.desc(),
            SignalWindowAggregate.unique_user_count.desc(),
            SignalWindowAggregate.max_severity.desc(),
        )
        .limit(limit)
    ).scalars().all()
    if not rows:
        return []
    trend_views = derive_audience_trends(rows, window_type="long", limit=limit)
    if trend_views:
        return [
            f"{row.target_name or '整体'}:{row.signal_type}:{row.current_level}"
            for row in trend_views
        ]
    return [
        f"{row.target_name or '整体'}:{row.signal_type}:{row.signal_level}"
        for row in rows
    ]


def load_long_window_audience_trend_views(
    session: Session,
    project_id: str,
    *,
    limit: int = 6,
):
    rows = session.execute(
        select(SignalWindowAggregate)
        .where(
            SignalWindowAggregate.project_id == project_id,
            SignalWindowAggregate.window_type == "long",
            SignalWindowAggregate.signal_level.in_(("confirmed", "watchlist", "candidate")),
        )
        .order_by(
            SignalWindowAggregate.window_chapter_end.desc(),
            SignalWindowAggregate.unique_user_count.desc(),
            SignalWindowAggregate.max_severity.desc(),
        )
    ).scalars().all()
    return derive_audience_trends(rows, window_type="long", limit=limit)


class ExperiencePlanningService:
    def build_audience_calibration_profile(
        self,
        *,
        session: Session,
        project_id: str,
    ) -> AudienceCalibrationProfile:
        trends = load_long_window_audience_trend_views(session, project_id)
        profile = AudienceCalibrationProfile()
        for trend in trends:
            strong_signal = trend.current_level in {"confirmed", "watchlist"} or trend.current_score >= 0.28
            if trend.signal_type == "pacing" and strong_signal and trend.trend_type != "falling":
                profile.boost_reward_density = True
                profile.favor_visible_payoff = True
                profile.reduce_setup_ratio = True
            elif trend.signal_type in {"confusion", "risk"} and strong_signal:
                profile.clarify_rule_legibility = True
                category = str(getattr(trend, "target_name", "") or "").strip()
                if category and category != "整体":
                    profile.avoid_trope_categories = _append_unique(
                        profile.avoid_trope_categories,
                        category,
                    )
            elif trend.signal_type in {"character_heat", "relationship_interest"} and strong_signal and trend.trend_type != "falling":
                profile.protect_character_heat = True
            elif trend.signal_type == "prediction" and strong_signal:
                profile.hold_managed_ambiguity = True
            elif trend.signal_type in {"status", "scale", "growth"} and strong_signal:
                profile.boost_status_payoff = True
        return profile


def _append_unique(values: list[str] | None, value: str) -> list[str]:
    result = [item for item in values or [] if str(item).strip()]
    if value and value not in result:
        result.append(value)
    return result
