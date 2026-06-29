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
import time
from pathlib import Path

from forwin.config import Config


def _get_config(args: argparse.Namespace) -> Config:
    """Build Config from CLI args + environment."""
    config = Config.from_env()
    kwargs: dict = {}
    database_url = getattr(args, "database_url", None)
    if database_url:
        kwargs["database_url"] = database_url
    if hasattr(args, "api_key") and args.api_key:
        kwargs["minimax_api_key"] = args.api_key
    if hasattr(args, "model") and args.model:
        kwargs["minimax_model"] = args.model
    if hasattr(args, "base_url") and args.base_url:
        kwargs["minimax_base_url"] = args.base_url
    if kwargs:
        return config.model_copy(update=kwargs)
    return config


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
    engine = get_engine(config.database_url)
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
    from forwin.models.entity import Entity
    from forwin.models.event import CanonEvent
    from forwin.models.project import ChapterPlan, Project
    from forwin.models.thread import PlotThread
    from forwin.state.query_helpers import load_latest_drafts_by_plan_id

    config = _get_config(args)
    engine = get_engine(config.database_url)
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
            draft_map = load_latest_drafts_by_plan_id(session, [plan.id for plan in plans])
            print(f"\n章节 ({len(plans)}):")
            for p in plans:
                draft = draft_map.get(p.id)
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


def cmd_llm_eval(args: argparse.Namespace) -> None:
    """Run or report ForWin LLM reliability evaluations."""
    from forwin.llm_eval.cli import report_eval_from_args, run_eval_from_args

    if args.llm_eval_command == "run":
        code = run_eval_from_args(args)
    elif args.llm_eval_command == "report":
        code = report_eval_from_args(args)
    else:
        code = 2
    if code:
        sys.exit(code)


def cmd_generation_worker(args: argparse.Namespace) -> None:
    """Run the durable generation worker."""
    from forwin.api_core import state as api_state
    from forwin.api_core.generation import _create_continue_generation_task
    from forwin.generation.worker_cli import default_worker_id, run_generation_worker_loop
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.runtime.container import RuntimeContainer

    config = _get_config(args)
    engine = get_engine(config.database_url)
    try:
        init_db(engine)
        Session = get_session_factory(engine)
        api_state._config = config
        api_state._engine = engine
        api_state._SessionFactory = Session
        api_state._runtime_container = RuntimeContainer.from_config(config, role="generation_worker")

        exit_code = run_generation_worker_loop(
            session_factory=Session,
            config=config,
            worker_id=args.worker_id or default_worker_id(),
            lease_seconds=args.lease_seconds,
            poll_interval=args.poll_interval,
            once=args.once,
            create_continue_generation_task=_create_continue_generation_task,
        )
    finally:
        engine.dispose()
    if exit_code:
        sys.exit(exit_code)


def run_publisher_worker_loop(
    backend_jobs,
    *,
    limit: int,
    once: bool,
    poll_interval: float,
    sleep=time.sleep,
) -> None:
    while True:
        handled = backend_jobs.run_pending_once(limit=limit)
        if handled:
            print("\n".join(handled))
        elif once:
            print("no publisher backend jobs")
        if once:
            return
        if not handled:
            sleep(max(float(poll_interval), 0.1))


def cmd_publisher_worker(args: argparse.Namespace) -> None:
    """Run publisher backend-owned jobs such as cover generation."""
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.runtime.container import RuntimeContainer

    config = _get_config(args)
    engine = get_engine(config.database_url)
    try:
        init_db(engine)
        Session = get_session_factory(engine)
        runtime = RuntimeContainer.from_config(config, role="publisher_worker").services().publisher_runtime
        run_publisher_worker_loop(
            runtime.backend_jobs,
            limit=args.limit,
            once=args.once,
            poll_interval=args.poll_interval,
        )
    finally:
        engine.dispose()


def cmd_outbox_worker(args: argparse.Namespace) -> None:
    """Run eventually consistent outbox side effects."""
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.outbox.handlers import build_default_outbox_handlers
    from forwin.outbox.worker import run_outbox_worker_loop

    config = _get_config(args)
    engine = get_engine(config.database_url)
    try:
        init_db(engine)
        Session = get_session_factory(engine)
        exit_code = run_outbox_worker_loop(
            session_factory=Session,
            worker_id=args.worker_id,
            handlers=build_default_outbox_handlers(
                session_factory=Session,
                config=config,
            ),
            poll_interval=args.poll_interval,
            once=args.once,
            max_attempts=args.max_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    finally:
        engine.dispose()
    if exit_code:
        sys.exit(exit_code)


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forwin",
        description="ForWin – 长篇中文网文生成系统 (Phase 0.5)",
    )
    parser.add_argument("--database-url", default=None, help="PostgreSQL SQLAlchemy URL")
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

    eval_parser = sub.add_parser("llm-eval", help="评估 LLM / Codex CLI 在 ForWin 场景下的可靠性")
    eval_sub = eval_parser.add_subparsers(dest="llm_eval_command", help="LLM eval 子命令")

    eval_run = eval_sub.add_parser("run", help="运行 LLM 可靠性测试")
    eval_run.add_argument("--suite", default="medium", choices=["smoke", "medium"], help="测试套件")
    eval_run.add_argument("--profiles", default="", help="逗号分隔的 profile id，例如 minimax,kimi,codex-spark")
    eval_run.add_argument("--manifest", default="", help="独立 eval profile manifest JSON")
    eval_run.add_argument("--runtime-settings-path", default="", help="runtime settings JSON 路径")
    eval_run.add_argument("--artifact-root", default="", help="输出 artifact root")
    eval_run.add_argument("--run-id", default="", help="指定 run id；默认自动生成")
    eval_run.add_argument("--rounds", type=int, default=0, help="每个 profile 的 direct probe 轮数；medium 默认 20，smoke 默认 1")
    eval_run.add_argument("--dry-run", action="store_true", help="只列出将运行的 profiles/cases，不调用 LLM")
    eval_run.add_argument("--skip-mini-real-run", action="store_true", help="只跑 direct stage probes")
    eval_run.add_argument("--base-url", default="", help="可选：生产 ForWin base URL，用于后续 live 集成")
    eval_run.add_argument("--allow-production-data", action="store_true", help="允许压测已部署实例或生产数据")

    eval_report = eval_sub.add_parser("report", help="读取并打印 LLM eval 报告")
    eval_report.add_argument("--run-id", required=True, help="run id")
    eval_report.add_argument("--artifact-root", default="", help="artifact root")

    worker = sub.add_parser("generation-worker", help="运行 durable generation worker")
    worker.add_argument("--worker-id", default="", help="Worker id；默认 hostname:pid")
    worker.add_argument("--lease-seconds", type=int, default=300, help="任务 lease 秒数")
    worker.add_argument("--poll-interval", type=float, default=2.0, help="无任务时轮询间隔秒数")
    worker.add_argument("--once", action="store_true", help="只 claim 一次后退出")

    publisher_worker = sub.add_parser("publisher-worker", help="运行 publisher 后端任务 worker")
    publisher_worker.add_argument("--once", action="store_true", help="只执行一轮后退出")
    publisher_worker.add_argument("--limit", type=int, default=1, help="单轮处理任务数")
    publisher_worker.add_argument("--poll-interval", type=float, default=2.0, help="无任务时轮询间隔秒数")

    outbox_worker = sub.add_parser("outbox-worker", help="运行 outbox side-effect worker")
    outbox_worker.add_argument("--worker-id", default="", help="Worker id")
    outbox_worker.add_argument("--poll-interval", type=float, default=2.0, help="无事件时轮询间隔秒数")
    outbox_worker.add_argument("--once", action="store_true", help="只 claim 一次后退出")
    outbox_worker.add_argument("--max-attempts", type=int, default=3, help="单个事件最大尝试次数")
    outbox_worker.add_argument("--retry-delay-seconds", type=int, default=30, help="失败重试等待秒数")

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
    elif args.command == "llm-eval":
        cmd_llm_eval(args)
    elif args.command == "generation-worker":
        cmd_generation_worker(args)
    elif args.command == "publisher-worker":
        cmd_publisher_worker(args)
    elif args.command == "outbox-worker":
        cmd_outbox_worker(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
