#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import quantiles

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ArcPlanVersion


def _arc_bucket(arcs: list[ArcPlanVersion], chapter_number: int) -> str:
    for arc in arcs:
        if int(arc.chapter_start or 0) <= int(chapter_number or 0) <= int(
            arc.chapter_end or 0
        ):
            return str(arc.id or f"arc:{arc.chapter_start}-{arc.chapter_end}")
    return "arc:unknown"


def _p95(values: list[int]) -> int | float:
    if len(values) >= 20:
        return quantiles(values, n=20)[18]
    return max(values) if values else 0


def main() -> int:
    config = Config.from_env()
    engine = get_engine(config.database_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = session.query(NarrativeObligationRow).all()
        arcs = session.query(ArcPlanVersion).all()

    arcs_by_project: dict[str, list[ArcPlanVersion]] = defaultdict(list)
    for arc in arcs:
        arcs_by_project[str(arc.project_id or "")].append(arc)

    by_arc: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    by_book: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        project_id = str(row.project_id or "")
        arc_id = _arc_bucket(
            arcs_by_project.get(project_id, []),
            int(row.origin_chapter_number or 0),
        )
        priority = str(getattr(row, "priority", "") or "")
        if priority in {"P0", "P1"}:
            by_arc[(project_id, arc_id)]["p0_p1"] += 1
            by_book[project_id]["p0_p1"] += 1
        if priority in {"P1", "P2"}:
            by_arc[(project_id, arc_id)]["p1_p2"] += 1
            by_book[project_id]["p1_p2"] += 1
        if priority == "P0":
            by_book[project_id]["p0"] += 1

    arc_values = [counter["p0_p1"] for counter in by_arc.values()]
    book_values = [counter["p0_p1"] for counter in by_book.values()]
    print(f"arc_buckets={len(by_arc)} arc_p0_p1_p95={_p95(arc_values)}")
    print(f"book_buckets={len(by_book)} book_p0_p1_p95={_p95(book_values)}")
    for (project_id, arc_id), counter in sorted(by_arc.items()):
        print(
            "arc "
            f"project={project_id} arc={arc_id} "
            f"p0_p1={counter['p0_p1']} p1_p2={counter['p1_p2']}"
        )
    for project_id, counter in sorted(by_book.items()):
        print(
            "book "
            f"project={project_id} p0={counter['p0']} "
            f"p0_p1={counter['p0_p1']} p1_p2={counter['p1_p2']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
