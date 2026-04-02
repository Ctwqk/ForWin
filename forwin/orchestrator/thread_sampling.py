from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import PlotThread, PlotThreadBeat
from forwin.state.query_helpers import load_latest_thread_beats


@dataclass(slots=True)
class SampledThreadSet:
    threads: list[PlotThread]
    latest_beats: dict[str, PlotThreadBeat]


def sample_active_threads(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    limit: int,
    stale_window: int,
    recent_window: int = 2,
) -> SampledThreadSet:
    active_limit = max(1, int(limit))
    candidate_limit = max(active_limit * 3, active_limit + 6)
    stale_threshold = max(1, int(stale_window))
    hot_threshold = max(1, int(recent_window))

    candidates = session.execute(
        select(PlotThread)
        .where(
            PlotThread.project_id == project_id,
            PlotThread.status == "active",
        )
        .order_by(PlotThread.priority.asc(), PlotThread.opened_at_chapter.asc())
        .limit(candidate_limit)
    ).scalars().all()
    latest_beats = load_latest_thread_beats(
        session,
        [thread.id for thread in candidates],
    )

    def _last_active(thread: PlotThread) -> int:
        beat = latest_beats.get(thread.id)
        return beat.chapter_number if beat is not None else thread.opened_at_chapter

    def _gap(thread: PlotThread) -> int:
        return max(0, chapter_number - _last_active(thread))

    def _score(thread: PlotThread) -> tuple[int, int, int, int]:
        gap = _gap(thread)
        stale_flag = 1 if gap >= stale_threshold else 0
        hot_flag = 1 if gap < hot_threshold else 0
        priority_weight = max(0, 12 - int(thread.priority))
        return (
            stale_flag,
            priority_weight,
            hot_flag,
            gap,
        )

    stale = [
        thread for thread in candidates
        if _gap(thread) >= stale_threshold
    ]
    hot = [
        thread for thread in candidates
        if _gap(thread) < hot_threshold
    ]
    baseline = [
        thread for thread in candidates
        if thread not in stale and thread not in hot
    ]

    stale.sort(key=_score, reverse=True)
    hot.sort(key=_score, reverse=True)
    baseline.sort(key=_score, reverse=True)

    selected: list[PlotThread] = []

    def _take(rows: list[PlotThread], quota: int) -> None:
        for thread in rows:
            if thread in selected:
                continue
            selected.append(thread)
            if len(selected) >= quota:
                break

    if stale:
        _take(stale, min(len(stale), max(1, round(active_limit * 0.4))))
    if hot and len(selected) < active_limit:
        current_len = len(selected)
        target_len = min(
            active_limit,
            current_len + min(len(hot), max(1, round(active_limit * 0.3))),
        )
        _take(hot, target_len)
    if len(selected) < active_limit:
        _take(baseline, active_limit)
    if len(selected) < active_limit:
        merged = sorted(candidates, key=_score, reverse=True)
        _take(merged, active_limit)

    chosen = selected[:active_limit]
    return SampledThreadSet(
        threads=chosen,
        latest_beats={thread.id: latest_beats[thread.id] for thread in chosen if thread.id in latest_beats},
    )
