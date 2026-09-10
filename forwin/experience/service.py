from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audience.feedback import derive_audience_trends
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
        return AudienceCalibrationProfile()
