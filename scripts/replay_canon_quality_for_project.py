#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.config import Config
from forwin.models import ChapterDraft, ChapterPlan, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.writer import WriterOutput


REPO_ROOT = Path(__file__).resolve().parents[1]


def replay_project(
    *,
    session_factory,
    project_id: str,
    from_chapter: int,
    to_chapter: int,
    output_path: Path,
    persist: bool = False,
) -> int:
    rows: list[tuple[int, str, list[str]]] = []
    signal_count = 0
    with session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= int(from_chapter),
                ChapterPlan.chapter_number <= int(to_chapter),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        for plan in plans:
            draft = session.execute(
                select(ChapterDraft)
                .where(ChapterDraft.chapter_plan_id == plan.id)
                .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if draft is None:
                continue
            writer_output = WriterOutput(
                project_id=project_id,
                chapter_number=int(plan.chapter_number or 0),
                title=str(plan.title or f"第{plan.chapter_number}章"),
                body=str(draft.body_text or ""),
                char_count=int(draft.char_count or len(str(draft.body_text or ""))),
                end_of_chapter_summary=str(draft.summary or ""),
            )
            result = analyze_writer_output_quality(
                session=session,
                project_id=project_id,
                chapter_number=int(plan.chapter_number or 0),
                writer_output=writer_output,
                draft_id=str(draft.id or ""),
                persist=persist,
            )
            signal_count += len(result.signals)
            rows.append(
                (
                    int(plan.chapter_number or 0),
                    str(plan.title or ""),
                    [
                        f"- `{signal.severity}` `{signal.signal_type}` {signal.description}"
                        for signal in result.signals
                    ],
                )
            )
        if persist:
            session.commit()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_report(project_id=project_id, rows=rows, signal_count=signal_count), encoding="utf-8")
    return signal_count


def _render_report(*, project_id: str, rows: list[tuple[int, str, list[str]]], signal_count: int) -> str:
    lines = [
        f"# Canon Quality Replay: {project_id}",
        "",
        f"Signal count: {signal_count}",
        "",
    ]
    for chapter_number, title, signals in rows:
        lines.append(f"## Chapter {chapter_number}: {title}")
        lines.extend(signals or ["- no signals"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay deterministic canon quality analyzers for an existing project.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--from-chapter", type=int, default=1)
    parser.add_argument("--to-chapter", type=int, default=10_000)
    parser.add_argument("--mode", choices=("report", "persist"), default="report")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    config = Config.from_env()
    engine = get_engine(config.database_url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    output = Path(args.output) if args.output else REPO_ROOT / "reports" / f"canon-quality-replay-{args.project_id}.md"
    try:
        count = replay_project(
            session_factory=session_factory,
            project_id=args.project_id,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            output_path=output,
            persist=args.mode == "persist",
        )
    finally:
        engine.dispose()
    print(f"wrote {output} with {count} signals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
