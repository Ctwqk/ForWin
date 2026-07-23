"""ForWin CLI – 长篇中文网文生成系统.

Usage:
    forwin read --project-id <id> [--chapter 1]
    forwin status --project-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time

from forwin.config import InfrastructureConfig


def _get_config(args: argparse.Namespace) -> InfrastructureConfig:
    """Build InfrastructureConfig from CLI args + environment."""
    config = InfrastructureConfig.from_env()
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


def _api_client(args: argparse.Namespace):
    from forwin.mcp.client import ForWinAPIClient

    return ForWinAPIClient(
        base_url=str(args.api_base_url).rstrip("/"),
        timeout=float(args.api_timeout),
    )


def cmd_generate(args: argparse.Namespace) -> None:
    """Create a Genesis project and hand it off to durable generation."""
    from forwin.mcp.models import STAGE_KEY_ORDER

    async def run() -> None:
        client = _api_client(args)
        created = await client.project_create(
            title=args.title,
            premise=args.premise,
            genre=args.genre,
            setting_summary=args.setting_summary,
            target_total_chapters=args.chapters,
        )
        if created.project is None:
            raise RuntimeError("ForWin API did not return the created project")
        project_id = created.project.id
        for stage_key in STAGE_KEY_ORDER:
            print(f"Genesis {stage_key}: generate")
            await client.genesis_stage_generate(
                project_id=project_id,
                stage_key=stage_key,
            )
            print(f"Genesis {stage_key}: lock")
            await client.genesis_stage_lock(
                project_id=project_id,
                stage_key=stage_key,
            )
        active = await client.task_active_generation_check(project_id=project_id)
        if active.has_active_generation_task:
            raise RuntimeError("project already has an active generation task")
        started = await client.project_start_writing(
            project_id=project_id,
            auto_continue=True,
            run_until_chapter=args.chapters,
            max_chapters=args.chapters,
        )
        task_id = started.task.task_id if started.task is not None else ""
        print(f"project_id={project_id}")
        print(f"task_id={task_id}")
        print(started.message)

    asyncio.run(run())


def cmd_read(args: argparse.Namespace) -> None:
    """Read a chapter through the deployed HTTP API."""
    chapter = asyncio.run(
        _api_client(args).chapter_get(
            project_id=args.project_id,
            chapter_number=args.chapter,
        )
    )
    print(f"\n{'=' * 60}")
    print(f"第{chapter.chapter_number}章  {chapter.title}")
    print(f"{'=' * 60}")
    print(f"字数: {chapter.char_count}  |  版本: v{chapter.version}")
    print(f"摘要: {chapter.summary}")
    print(f"{'─' * 60}\n")
    print(chapter.body)
    print(f"\n{'─' * 60}")


def cmd_status(args: argparse.Namespace) -> None:
    """Show project status through the deployed HTTP API."""
    project = asyncio.run(_api_client(args).project_get(args.project_id))
    print(f"\n{'=' * 60}")
    print(f"项目: {project.title}")
    print(f"ID: {project.id}")
    print(f"类型: {project.genre}")
    print(f"生命周期: {project.creation_status}")
    print(f"当前阶段: {project.latest_stage or '-'}")
    print(f"下一门禁: {project.next_gate or '-'}")
    print(f"接受章节: {project.accepted_chapter_count}/{project.target_total_chapters}")
    if project.blocking_reason.message:
        print(f"阻断: {project.blocking_reason.message}")
    if project.chapters:
        print("\n章节:")
        for chapter in project.chapters:
            print(
                f"  第{chapter.chapter_number}章 《{chapter.title}》 "
                f"[{chapter.status}] {chapter.char_count}字"
            )
    print(f"{'=' * 60}\n")


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
    from forwin.generation.worker_cli import (
        default_worker_id,
        run_generation_worker_loop,
    )
    from forwin.runtime.workers import build_generation_worker_runtime

    config = _get_config(args)
    runtime = build_generation_worker_runtime(config)
    try:
        exit_code = run_generation_worker_loop(
            application_service=runtime.application_service,
            worker_id=args.worker_id or default_worker_id(),
            lease_seconds=args.lease_seconds,
            poll_interval=args.poll_interval,
            once=args.once,
        )
    finally:
        runtime.close()
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
    with backend_jobs.singleton_worker_lock():
        backend_jobs.recover_interrupted_cover_jobs()
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
    from forwin.runtime.workers import build_publisher_worker_runtime

    config = _get_config(args)
    runtime = build_publisher_worker_runtime(config)
    try:
        run_publisher_worker_loop(
            runtime.publisher_runtime.backend_jobs,
            limit=args.limit,
            once=args.once,
            poll_interval=args.poll_interval,
        )
    finally:
        runtime.close()


def cmd_outbox_worker(args: argparse.Namespace) -> None:
    """Run eventually consistent outbox side effects."""
    from forwin.outbox.worker import run_outbox_worker_loop
    from forwin.runtime.workers import build_outbox_worker_runtime

    config = _get_config(args)
    runtime = build_outbox_worker_runtime(config)
    try:
        exit_code = run_outbox_worker_loop(
            session_factory=runtime.session_factory,
            worker_id=args.worker_id,
            handlers=runtime.handlers,
            poll_interval=args.poll_interval,
            once=args.once,
            lease_seconds=args.lease_seconds,
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            base_delay_seconds=args.base_delay_seconds,
            max_delay_seconds=args.max_delay_seconds,
        )
    finally:
        runtime.close()
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
    parser.add_argument(
        "--database-url", default=None, help="PostgreSQL SQLAlchemy URL"
    )
    parser.add_argument("--api-key", default=None, help="LLM API Key")
    parser.add_argument("--model", default=None, help="LLM 模型名称")
    parser.add_argument("--base-url", default=None, help="LLM API Base URL")
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("FORWIN_API_BASE_URL", "http://127.0.0.1:8899"),
        help="ForWin HTTP API base URL",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=900.0,
        help="HTTP timeout 秒数",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="启用详细日志")

    sub = parser.add_subparsers(dest="command", help="子命令")

    generate = sub.add_parser("generate", help="创建 Genesis 项目并启动写作")
    generate.add_argument("--title", required=True, help="书名")
    generate.add_argument("--premise", required=True, help="小说设定/前提")
    generate.add_argument("--genre", default="玄幻", help="类型")
    generate.add_argument("--setting-summary", default="", help="世界设定摘要")
    generate.add_argument("--chapters", type=int, default=3, help="目标章节数")

    # read
    read = sub.add_parser("read", help="阅读已生成的章节")
    read.add_argument("--project-id", required=True, help="项目ID")
    read.add_argument("--chapter", type=int, default=1, help="章节号 (默认: 1)")

    # status
    stat = sub.add_parser("status", help="查看项目状态")
    stat.add_argument("--project-id", required=True, help="项目ID")

    eval_parser = sub.add_parser(
        "llm-eval", help="评估 LLM / Codex CLI 在 ForWin 场景下的可靠性"
    )
    eval_sub = eval_parser.add_subparsers(
        dest="llm_eval_command", help="LLM eval 子命令"
    )

    eval_run = eval_sub.add_parser("run", help="运行 LLM 可靠性测试")
    eval_run.add_argument(
        "--suite", default="medium", choices=["smoke", "medium"], help="测试套件"
    )
    eval_run.add_argument(
        "--profiles",
        default="",
        help="逗号分隔的 profile id，例如 minimax,kimi,codex-spark",
    )
    eval_run.add_argument(
        "--manifest", default="", help="独立 eval profile manifest JSON"
    )
    eval_run.add_argument("--artifact-root", default="", help="输出 artifact root")
    eval_run.add_argument("--run-id", default="", help="指定 run id；默认自动生成")
    eval_run.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="每个 profile 的 direct probe 轮数；medium 默认 20，smoke 默认 1",
    )
    eval_run.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出将运行的 profiles/cases，不调用 LLM",
    )
    eval_run.add_argument(
        "--skip-mini-real-run", action="store_true", help="只跑 direct stage probes"
    )
    eval_report = eval_sub.add_parser("report", help="读取并打印 LLM eval 报告")
    eval_report.add_argument("--run-id", required=True, help="run id")
    eval_report.add_argument("--artifact-root", default="", help="artifact root")

    worker = sub.add_parser("generation-worker", help="运行 durable generation worker")
    worker.add_argument("--worker-id", default="", help="Worker id；默认 hostname:pid")
    worker.add_argument(
        "--lease-seconds", type=int, default=300, help="任务 lease 秒数"
    )
    worker.add_argument(
        "--poll-interval", type=float, default=2.0, help="无任务时轮询间隔秒数"
    )
    worker.add_argument("--once", action="store_true", help="只 claim 一次后退出")

    publisher_worker = sub.add_parser(
        "publisher-worker", help="运行 publisher 后端任务 worker"
    )
    publisher_worker.add_argument(
        "--once", action="store_true", help="只执行一轮后退出"
    )
    publisher_worker.add_argument("--limit", type=int, default=1, help="单轮处理任务数")
    publisher_worker.add_argument(
        "--poll-interval", type=float, default=2.0, help="无任务时轮询间隔秒数"
    )

    outbox_worker = sub.add_parser(
        "outbox-worker", help="运行 outbox side-effect worker"
    )
    outbox_worker.add_argument(
        "--worker-id",
        default=f"{socket.gethostname()}:{os.getpid()}",
        help="Worker id",
    )
    outbox_worker.add_argument(
        "--poll-interval", type=float, default=2.0, help="无事件时轮询间隔秒数"
    )
    outbox_worker.add_argument(
        "--once", action="store_true", help="只 claim 一次后退出"
    )
    outbox_worker.add_argument(
        "--lease-seconds", type=float, default=60.0, help="事件 lease 秒数"
    )
    outbox_worker.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        default=15.0,
        help="lease 心跳间隔秒数",
    )
    outbox_worker.add_argument(
        "--base-delay-seconds",
        type=float,
        default=30.0,
        help="失败重试基础退避秒数",
    )
    outbox_worker.add_argument(
        "--max-delay-seconds",
        type=float,
        default=900.0,
        help="失败重试最大退避秒数",
    )

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
