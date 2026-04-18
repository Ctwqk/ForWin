#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from forwin.config import Config
from forwin.models import ChapterPlan, GenerationTask, ProvisionalChapterLedger
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.state.repo import StateRepository
from forwin.writer.prompts import build_preview_chapter_prompt


def repo_root() -> Path:
    return ROOT


def dotenv_values() -> dict[str, str]:
    path = repo_root() / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_defaults() -> None:
    for key, value in dotenv_values().items():
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and optionally replay the exact provisional preview request."
    )
    parser.add_argument("--task-id", default="", help="Generation task id to inspect.")
    parser.add_argument("--project-id", default="", help="Project id to inspect.")
    parser.add_argument(
        "--chapter",
        type=int,
        default=0,
        help="Chapter number to probe. Defaults to latest failed provisional chapter.",
    )
    parser.add_argument(
        "--error-contains",
        default="529",
        help="When auto-selecting, prefer the latest provisional ledger whose error contains this text.",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Override FORWIN_DB_PATH before loading config.",
    )
    parser.add_argument(
        "--call",
        action="store_true",
        help="Actually send the request to the configured Minimax endpoint.",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Directory to write payload/response files to. Defaults to /tmp/<timestamp>.",
    )
    return parser.parse_args()


def pick_latest_provisional_failure(
    session, error_contains: str, db_path: str
) -> tuple[str, int, str]:
    stmt = select(ProvisionalChapterLedger).order_by(
        ProvisionalChapterLedger.created_at.desc(),
        ProvisionalChapterLedger.chapter_number.desc(),
    )
    ledgers = session.execute(stmt).scalars().all()
    for ledger in ledgers:
        error_text = str(ledger.error_text or "")
        if error_contains and error_contains.lower() not in error_text.lower():
            continue
        if error_text.strip():
            return ledger.project_id, int(ledger.chapter_number), error_text
    for ledger in ledgers:
        error_text = str(ledger.error_text or "")
        if error_text.strip():
            return ledger.project_id, int(ledger.chapter_number), error_text
    raise SystemExit(f"No provisional failure ledger found in db: {db_path}")


def pick_target_from_task(session, task_id: str) -> tuple[str, int]:
    task = session.execute(
        select(GenerationTask).where(GenerationTask.id == task_id)
    ).scalar_one_or_none()
    if task is None:
        raise SystemExit(f"Generation task not found: {task_id}")
    project_id = str(task.project_id or "").strip()
    chapter = int(task.current_chapter or 0)
    if not project_id:
        raise SystemExit(f"Generation task has no project_id: {task_id}")
    if chapter > 0:
        return project_id, chapter
    plan = session.execute(
        select(ChapterPlan)
        .where(ChapterPlan.project_id == project_id)
        .order_by(ChapterPlan.chapter_number.asc())
        .limit(1)
    ).scalar_one_or_none()
    if plan is None:
        raise SystemExit(f"No chapter plan found for project: {project_id}")
    return project_id, int(plan.chapter_number)


def resolve_target(session, args: argparse.Namespace) -> tuple[str, int, str]:
    if args.task_id:
        project_id, chapter = pick_target_from_task(session, args.task_id)
        return project_id, chapter, ""
    if args.project_id and args.chapter > 0:
        return args.project_id, int(args.chapter), ""
    project_id, chapter, error_text = pick_latest_provisional_failure(
        session, args.error_contains, os.environ.get("FORWIN_DB_PATH", "")
    )
    return project_id, chapter, error_text


def make_out_dir(raw: str) -> Path:
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = Path("/tmp") / f"forwin-provisional-probe-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompt_params(orchestrator: WritingOrchestrator) -> dict[str, float | int]:
    writer = orchestrator.provisional_writer
    target_chars = max(
        writer.min_chapter_chars,
        min(writer.target_chapter_chars, writer.max_chapter_chars),
    )
    max_output_tokens = min(
        writer.max_tokens,
        max(1800, int(target_chars * 1.8)),
    )
    return {
        "temperature": min(writer.temperature, 0.7),
        "target_chars": target_chars,
        "min_chars": writer.min_chapter_chars,
        "max_chars": writer.max_chapter_chars,
        "max_tokens": max_output_tokens,
        "timeout_seconds": writer.single_call_timeout_seconds,
    }


def write_payload_files(
    out_dir: Path,
    *,
    payload: dict[str, object],
    meta: dict[str, object],
) -> None:
    (out_dir / "request.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prompt_text = []
    for index, message in enumerate(payload.get("messages", []), start=1):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "unknown"))
        content = str(message.get("content", ""))
        prompt_text.append(f"[{index}] role={role}\n{content}")
    (out_dir / "prompt.txt").write_text("\n\n".join(prompt_text), encoding="utf-8")


def main() -> int:
    load_env_defaults()
    args = parse_args()
    if args.db_path:
        os.environ["FORWIN_DB_PATH"] = str(args.db_path)
    config = Config.from_env()
    orchestrator = WritingOrchestrator(config)
    try:
        session = orchestrator._SessionFactory()
        try:
            project_id, chapter_number, source_error = resolve_target(session, args)
            plan = session.execute(
                select(ChapterPlan).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == chapter_number,
                )
            ).scalar_one_or_none()
            if plan is None:
                raise SystemExit(
                    f"Chapter plan not found for project={project_id} chapter={chapter_number}"
                )

            repo = StateRepository(session)
            context = orchestrator.retrieval_broker.build_chapter_context(
                repo, project_id, plan
            )
            params = prompt_params(orchestrator)
            messages = build_preview_chapter_prompt(
                context,
                target_chars=int(params["target_chars"]),
                min_chars=int(params["min_chars"]),
                max_chars=int(params["max_chars"]),
            )
            payload = {
                "model": orchestrator.llm_client.model,
                "messages": messages,
                "temperature": params["temperature"],
                "max_tokens": params["max_tokens"],
            }
            out_dir = make_out_dir(args.out_dir)
            meta = {
                "project_id": project_id,
                "chapter_number": chapter_number,
                "chapter_title": plan.title,
                "task_id": args.task_id or None,
                "base_url": orchestrator.llm_client.base_url,
                "call_enabled": bool(args.call),
                "source_error": source_error,
                "request_summary": {
                    "model": orchestrator.llm_client.model,
                    "message_count": len(messages),
                    "temperature": params["temperature"],
                    "max_tokens": params["max_tokens"],
                    "timeout_seconds": params["timeout_seconds"],
                },
            }
            write_payload_files(out_dir, payload=payload, meta=meta)

            print(f"output_dir={out_dir}")
            print(f"project_id={project_id}")
            print(f"chapter_number={chapter_number}")
            print(f"model={orchestrator.llm_client.model}")
            print(f"base_url={orchestrator.llm_client.base_url}")
            print(f"messages={len(messages)}")
            print(f"max_tokens={params['max_tokens']}")
            if source_error:
                print(f"source_error={source_error}")
            print(f"request_json={out_dir / 'request.json'}")
            print(f"prompt_txt={out_dir / 'prompt.txt'}")

            if not args.call:
                print("call_skipped=true")
                return 0

            if not str(config.minimax_api_key or "").strip():
                raise SystemExit("MINIMAX_API_KEY is empty; cannot send request.")

            try:
                raw = orchestrator.llm_client.chat(
                    messages,
                    temperature=float(params["temperature"]),
                    max_tokens=int(params["max_tokens"]),
                    timeout_seconds=float(params["timeout_seconds"]),
                    retry_on_timeout=True,
                )
            except Exception as exc:  # noqa: BLE001
                (out_dir / "error.txt").write_text(str(exc), encoding="utf-8")
                print(f"call_error={exc}")
                print(f"error_txt={out_dir / 'error.txt'}")
                return 2

            (out_dir / "response.txt").write_text(raw, encoding="utf-8")
            print(f"call_ok=true")
            print(f"response_txt={out_dir / 'response.txt'}")
            print(f"response_chars={len(raw)}")
            return 0
        finally:
            session.close()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
