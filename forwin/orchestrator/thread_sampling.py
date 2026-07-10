from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwin.book_state.query import BookStateQuery


@dataclass(frozen=True, slots=True)
class SampledThread:
    id: str
    name: str
    description: str
    status: str
    priority: int
    opened_at_chapter: int


@dataclass(frozen=True, slots=True)
class SampledThreadBeat:
    chapter_number: int
    beat_type: str
    description: str


@dataclass(slots=True)
class SampledThreadSet:
    threads: list[SampledThread]
    latest_beats: dict[str, SampledThreadBeat]


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
    runtime = BookStateQuery(session).runtime(
        project_id,
        as_of_chapter=max(int(chapter_number), 0),
    )
    candidates: list[SampledThread] = []
    latest_beats: dict[str, SampledThreadBeat] = {}
    for node in runtime.narrative.nodes_by_id.values():
        if str(node.node_type) != "plot_thread" or node.status != "active":
            continue
        payload = node.payload if isinstance(node.payload, dict) else {}
        beats = payload.get("beats") if isinstance(payload.get("beats"), list) else []
        beat_rows = [item for item in beats if isinstance(item, dict)]
        if beat_rows:
            latest = max(
                beat_rows,
                key=lambda item: (
                    _int(item.get("chapter_number")),
                    _int(item.get("sequence")),
                ),
            )
            latest_beats[node.id] = SampledThreadBeat(
                chapter_number=_int(latest.get("chapter_number")),
                beat_type=str(latest.get("beat_type") or ""),
                description=str(latest.get("description") or ""),
            )
        candidates.append(
            SampledThread(
                id=node.id,
                name=node.title or node.id,
                description=str(payload.get("description") or ""),
                status=node.status,
                priority=_int(payload.get("priority"), default=5),
                opened_at_chapter=_int(node.metadata.get("created_at_chapter")),
            )
        )
    candidates.sort(key=lambda item: (item.priority, item.opened_at_chapter, item.id))
    candidates = candidates[:candidate_limit]

    def last_active(thread: SampledThread) -> int:
        beat = latest_beats.get(thread.id)
        return beat.chapter_number if beat is not None else thread.opened_at_chapter

    def gap(thread: SampledThread) -> int:
        return max(0, chapter_number - last_active(thread))

    def score(thread: SampledThread) -> tuple[int, int, int, int]:
        thread_gap = gap(thread)
        return (
            1 if thread_gap >= stale_threshold else 0,
            max(0, 12 - thread.priority),
            1 if thread_gap < hot_threshold else 0,
            thread_gap,
        )

    stale = [thread for thread in candidates if gap(thread) >= stale_threshold]
    hot = [thread for thread in candidates if gap(thread) < hot_threshold]
    stale_ids = {thread.id for thread in stale}
    hot_ids = {thread.id for thread in hot}
    baseline = [
        thread
        for thread in candidates
        if thread.id not in stale_ids and thread.id not in hot_ids
    ]
    stale.sort(key=score, reverse=True)
    hot.sort(key=score, reverse=True)
    baseline.sort(key=score, reverse=True)

    selected: list[SampledThread] = []

    def take(rows: list[SampledThread], quota: int) -> None:
        for thread in rows:
            if thread in selected:
                continue
            selected.append(thread)
            if len(selected) >= quota:
                break

    if stale:
        take(stale, min(len(stale), max(1, round(active_limit * 0.4))))
    if hot and len(selected) < active_limit:
        take(
            hot,
            min(
                active_limit,
                len(selected) + min(len(hot), max(1, round(active_limit * 0.3))),
            ),
        )
    if len(selected) < active_limit:
        take(baseline, active_limit)
    if len(selected) < active_limit:
        take(sorted(candidates, key=score, reverse=True), active_limit)

    chosen = selected[:active_limit]
    return SampledThreadSet(
        threads=chosen,
        latest_beats={
            thread.id: latest_beats[thread.id]
            for thread in chosen
            if thread.id in latest_beats
        },
    )


def _int(raw: object, *, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "SampledThread",
    "SampledThreadBeat",
    "SampledThreadSet",
    "sample_active_threads",
]
