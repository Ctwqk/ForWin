"""ForWin CLI – 长篇中文网文生成系统.

Usage:
    forwin generate --premise "在一个灵气复苏的末世..." [--genre 玄幻] [--chapters 3]
    forwin read --project-id <id> [--chapter 1]
    forwin status --project-id <id>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from forwin.config import Config


def _get_config(args: argparse.Namespace) -> Config:
    """Build Config from CLI args + environment."""
    kwargs: dict = {}
    if hasattr(args, "db") and args.db:
        kwargs["db_path"] = args.db
    if hasattr(args, "api_key") and args.api_key:
        kwargs["minimax_api_key"] = args.api_key
    elif os.environ.get("MINIMAX_API_KEY"):
        kwargs["minimax_api_key"] = os.environ["MINIMAX_API_KEY"]
    if hasattr(args, "model") and args.model:
        kwargs["minimax_model"] = args.model
    if hasattr(args, "base_url") and args.base_url:
        kwargs["minimax_base_url"] = args.base_url
    return Config(**kwargs)


# ------------------------------------------------------------------
# Subcommands
# ------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate chapters from a premise."""
    from forwin.orchestrator.loop import WritingOrchestrator

    config = _get_config(args)
    if not config.minimax_api_key:
        print("错误: 未设置 API Key。请通过 --api-key 或 MINIMAX_API_KEY 环境变量设置。")
        sys.exit(1)

    orchestrator = WritingOrchestrator(config)
    result = orchestrator.run(
        premise=args.premise,
        genre=args.genre,
        num_chapters=args.chapters,
    )
    project_id = result.project_id
    if result.failed_chapters:
        failed_str = ", ".join(str(chapter) for chapter in result.failed_chapters)
        print(
            f"\n警告: 成功生成 {len(result.completed_chapters)} 章，"
            f"失败章节: {failed_str}"
        )
    print(f"\n使用以下命令阅读生成的章节:")
    print(f"  forwin read --project-id {project_id} --chapter 1")
    print(f"  forwin status --project-id {project_id}")


def cmd_read(args: argparse.Namespace) -> None:
    """Read a chapter from the database."""
    from sqlalchemy import select

    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.draft import ChapterDraft
    from forwin.models.project import ChapterPlan

    config = _get_config(args)
    engine = get_engine(config.db_path)
    init_db(engine)
    Session = get_session_factory(engine)
    session = Session()

    try:
        # Find the chapter plan.
        stmt = (
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == args.project_id,
                ChapterPlan.chapter_number == args.chapter,
            )
        )
        plan = session.execute(stmt).scalar_one_or_none()
        if plan is None:
            print(f"未找到项目 {args.project_id} 的第 {args.chapter} 章计划。")
            return

        # Find the latest draft for this chapter.
        draft_stmt = (
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == plan.id)
            .order_by(ChapterDraft.version.desc())
            .limit(1)
        )
        draft = session.execute(draft_stmt).scalar_one_or_none()
        if draft is None:
            print(f"第 {args.chapter} 章尚未生成。")
            return

        print(f"\n{'='*60}")
        print(f"第{args.chapter}章  {plan.title}")
        print(f"{'='*60}")
        print(f"字数: {draft.char_count}  |  版本: v{draft.version}")
        print(f"摘要: {draft.summary}")
        print(f"{'─'*60}\n")
        print(draft.body_text)
        print(f"\n{'─'*60}")
    finally:
        session.close()
        engine.dispose()


def cmd_status(args: argparse.Namespace) -> None:
    """Show project status."""
    from sqlalchemy import func, select

    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.draft import ChapterDraft
    from forwin.models.entity import Entity
    from forwin.models.event import CanonEvent
    from forwin.models.project import ChapterPlan, Project
    from forwin.models.thread import PlotThread

    config = _get_config(args)
    engine = get_engine(config.db_path)
    init_db(engine)
    Session = get_session_factory(engine)
    session = Session()

    try:
        project = session.get(Project, args.project_id)
        if project is None:
            print(f"未找到项目: {args.project_id}")
            return

        print(f"\n{'='*60}")
        print(f"项目: {project.title}")
        print(f"ID: {project.id}")
        print(f"类型: {project.genre}")
        print(f"{'─'*60}")

        # Premise (truncated)
        premise_display = project.premise[:100] + "..." if len(project.premise) > 100 else project.premise
        print(f"设定: {premise_display}")

        # Entities
        entities = session.execute(
            select(Entity).where(Entity.project_id == args.project_id, Entity.is_active == True)
        ).scalars().all()
        chars = [e for e in entities if e.kind == "character"]
        locs = [e for e in entities if e.kind == "location"]
        facs = [e for e in entities if e.kind == "faction"]
        print(f"\n角色 ({len(chars)}): {', '.join(c.name for c in chars)}")
        if locs:
            print(f"地点 ({len(locs)}): {', '.join(l.name for l in locs)}")
        if facs:
            print(f"势力 ({len(facs)}): {', '.join(f.name for f in facs)}")

        # Plot threads
        threads = session.execute(
            select(PlotThread).where(PlotThread.project_id == args.project_id)
        ).scalars().all()
        if threads:
            print(f"\n情节线 ({len(threads)}):")
            for t in threads:
                print(f"  [{t.status}] {t.name} (优先级{t.priority})")

        # Chapters
        plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == args.project_id)
            .order_by(ChapterPlan.chapter_number)
        ).scalars().all()
        if plans:
            print(f"\n章节 ({len(plans)}):")
            for p in plans:
                # Get draft info
                draft = session.execute(
                    select(ChapterDraft)
                    .where(ChapterDraft.chapter_plan_id == p.id)
                    .order_by(ChapterDraft.version.desc())
                    .limit(1)
                ).scalar_one_or_none()
                char_info = f" ({draft.char_count}字)" if draft else ""
                print(f"  第{p.chapter_number}章 《{p.title}》 [{p.status}]{char_info}")

        # Events
        event_count = session.execute(
            select(func.count(CanonEvent.id)).where(CanonEvent.project_id == args.project_id)
        ).scalar_one()
        print(f"\n已记录事件: {event_count}")

        print(f"{'='*60}\n")
    finally:
        session.close()
        engine.dispose()


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forwin",
        description="ForWin – 长篇中文网文生成系统 (Phase 0.5)",
    )
    parser.add_argument("--db", default=None, help="数据库路径 (默认: data/novel.db)")
    parser.add_argument("--api-key", default=None, help="LLM API Key")
    parser.add_argument("--model", default=None, help="LLM 模型名称")
    parser.add_argument("--base-url", default=None, help="LLM API Base URL")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="启用详细日志"
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    # generate
    gen = sub.add_parser("generate", help="生成小说章节")
    gen.add_argument("--premise", required=True, help="小说设定/前提")
    gen.add_argument("--genre", default="玄幻", help="类型 (默认: 玄幻)")
    gen.add_argument("--chapters", type=int, default=3, help="生成章节数 (默认: 3)")

    # read
    read = sub.add_parser("read", help="阅读已生成的章节")
    read.add_argument("--project-id", required=True, help="项目ID")
    read.add_argument("--chapter", type=int, default=1, help="章节号 (默认: 1)")

    # status
    stat = sub.add_parser("status", help="查看项目状态")
    stat.add_argument("--project-id", required=True, help="项目ID")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s: %(message)s",
        )

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "read":
        cmd_read(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
