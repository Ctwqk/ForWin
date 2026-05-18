#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_llm_client_for_replay(config, requested_profile: str = "", client_builder=None):  # noqa: ANN001, ANN201
    if client_builder is None:
        from forwin.runtime.container import ServiceContainer

        client_builder = ServiceContainer._build_llm_client
    client = client_builder(config)
    if not requested_profile:
        return client
    profiles = getattr(client, "_request_profiles", lambda: [])()
    requested = requested_profile.strip().lower()
    selected = [
        profile
        for profile in profiles
        if requested
        in {
            str(profile.get("id", "")).strip().lower(),
            str(profile.get("name", "")).strip().lower(),
        }
        and str(profile.get("api_key", "")).strip()
        and str(profile.get("base_url", "")).strip()
        and str(profile.get("model", "")).strip()
    ]
    if not selected:
        raise SystemExit(f"LLM profile not found or incomplete: {requested_profile}")
    profile = selected[0]
    client.api_key = str(profile["api_key"]).strip()
    client.base_url = str(profile["base_url"]).strip().rstrip("/")
    client.model = str(profile["model"]).strip()
    client.profile_id = str(profile.get("id", "")).strip()
    client.profile_name = str(profile.get("name", "")).strip()
    client.fallback_profiles = []
    return client


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Chapter Review Form canon over accepted chapters.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--from-chapter", type=int, required=True)
    parser.add_argument("--to-chapter", type=int, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Run LLM and produce candidate rows without DB writes.")
    mode.add_argument("--persist", action="store_true", help="Write replayed form-sourced canon rows.")
    parser.add_argument("--llm-profile", default="", help="Config LLM profile id or name. Empty means current default routing.")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-restart", action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--abort-on-error", action="store_true")
    parser.add_argument("--cost-cap-usd", type=float, default=None)
    parser.add_argument("--no-cost-cap", action="store_true")
    parser.add_argument("--diff-mode", action="store_true")
    parser.add_argument("--schema-version", default="")
    parser.add_argument("--clear-state", action="store_true")
    parser.add_argument("--confirm-clear", action="store_true")
    args = parser.parse_args(argv)
    if args.persist:
        args.dry_run = False
    return args


def emit_json_line(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resolved_to_chapter = args.to_chapter if args.to_chapter is not None else args.from_chapter

    from forwin.config import Config
    from forwin.canon_quality.chapter_review_form.replay import replay_chapter_range, replay_single_chapter
    from forwin.canon_quality.chapter_review_form.replay_state import ReplayRangeOptions
    from forwin.models.base import get_engine, get_session_factory, init_db

    config = Config.from_env()
    engine = get_engine(config.database_url)
    try:
        init_db(engine)
        session_factory = get_session_factory(engine)
        if args.estimate_only:
            from forwin.canon_quality.chapter_review_form.cost_estimator import estimate_run

            estimate = estimate_run(
                session_factory=session_factory,
                project_id=args.project_id,
                from_chapter=args.from_chapter,
                to_chapter=resolved_to_chapter,
            )
            emit_json_line({"status": "estimate", **estimate.model_dump(mode="json")})
            return 0
        if args.cost_cap_usd is None and not args.no_cost_cap:
            emit_json_line(
                {
                    "status": "error",
                    "error": "missing_cost_cap",
                    "message": "Pass --cost-cap-usd <N> or --no-cost-cap.",
                }
            )
            return 2

        llm_client = build_llm_client_for_replay(config, args.llm_profile)
        if int(resolved_to_chapter) == int(args.from_chapter):
            with session_factory() as session:
                result = replay_single_chapter(
                    session=session,
                    project_id=args.project_id,
                    chapter_number=args.from_chapter,
                    llm_client=llm_client,
                    persist=args.persist,
                    mode="primary" if args.persist else "dry_run",
                )
                if args.persist:
                    session.commit()
                else:
                    session.rollback()
            emit_json_line(result.model_dump(mode="json"))
            return 0

        options = ReplayRangeOptions(
            persist=args.persist,
            mode="primary" if args.persist else "dry_run",
            resume=args.resume,
            force_restart=args.force_restart,
            force_rerun=args.force_rerun,
            abort_on_error=args.abort_on_error,
            cost_cap_usd=args.cost_cap_usd,
            no_cost_cap=args.no_cost_cap,
        )
        results = replay_chapter_range(
            session_factory=session_factory,
            project_id=args.project_id,
            from_chapter=args.from_chapter,
            to_chapter=resolved_to_chapter,
            llm_client_factory=lambda _chapter: llm_client,
            state_root=Path(config.artifact_root),
            options=options,
        )
        for result in results:
            emit_json_line(result.model_dump(mode="json"))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
