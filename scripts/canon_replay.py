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


def clear_state_if_requested(*, clear_state: bool, confirm_clear: bool, state_path: Path) -> dict[str, str]:
    if not clear_state:
        return {"status": "not_requested"}
    if not confirm_clear:
        return {
            "status": "error",
            "error": "confirm_clear_required",
            "message": "Pass --confirm-clear to delete replay state.",
        }
    if state_path.exists():
        state_path.unlink()
    return {"status": "cleared", "state_file": str(state_path)}


def validate_cost_cap_args(*, cost_cap_usd: float | None, no_cost_cap: bool) -> dict[str, str]:
    if cost_cap_usd is None and not no_cost_cap:
        return {
            "status": "error",
            "error": "missing_cost_cap",
            "message": "Pass --cost-cap-usd <N> or --no-cost-cap.",
        }
    return {"status": "ok"}


def schema_version_warning(*, requested_schema_version: str, current_schema_version: str) -> dict[str, str]:
    requested = str(requested_schema_version or "").strip()
    current = str(current_schema_version or "").strip()
    if not requested or requested == current:
        return {}
    return {
        "status": "warning",
        "warning": "schema_version_mismatch",
        "requested_schema_version": requested,
        "current_schema_version": current,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from forwin.config import Config
    from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
    from forwin.canon_quality.chapter_review_form.replay import (
        latest_accepted_chapter,
        replay_chapter_range,
        replay_single_chapter,
        replay_single_chapter_diff,
        summarize_replay_results,
    )
    from forwin.canon_quality.chapter_review_form.replay_state import ReplayRangeOptions, state_file_path
    from forwin.models.base import get_engine, get_session_factory, init_db

    config = Config.from_env()
    engine = get_engine(config.database_url)
    try:
        init_db(engine)
        session_factory = get_session_factory(engine)
        if args.to_chapter is None:
            with session_factory() as session:
                resolved_to_chapter = latest_accepted_chapter(
                    session=session,
                    project_id=args.project_id,
                )
        else:
            resolved_to_chapter = args.to_chapter

        state_path = state_file_path(
            root=Path(config.artifact_root),
            project_id=args.project_id,
            from_chapter=args.from_chapter,
            to_chapter=resolved_to_chapter,
        )
        clear_result = clear_state_if_requested(
            clear_state=args.clear_state,
            confirm_clear=args.confirm_clear,
            state_path=state_path,
        )
        if clear_result["status"] == "error":
            emit_json_line(clear_result)
            return 2
        if clear_result["status"] == "cleared":
            emit_json_line(clear_result)

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
        cost_cap_validation = validate_cost_cap_args(
            cost_cap_usd=args.cost_cap_usd,
            no_cost_cap=args.no_cost_cap,
        )
        if cost_cap_validation["status"] == "error":
            emit_json_line(cost_cap_validation)
            return 2

        schema_warning = schema_version_warning(
            requested_schema_version=args.schema_version,
            current_schema_version=FORM_SCHEMA_VERSION,
        )
        if schema_warning:
            print(json.dumps(schema_warning, ensure_ascii=False, sort_keys=True), file=sys.stderr)

        llm_client = build_llm_client_for_replay(config, args.llm_profile)
        if args.diff_mode:
            for chapter_number in range(int(args.from_chapter), int(resolved_to_chapter) + 1):
                with session_factory() as session:
                    differences = replay_single_chapter_diff(
                        session=session,
                        project_id=args.project_id,
                        chapter_number=chapter_number,
                        llm_client=llm_client,
                    )
                    session.rollback()
                emit_json_line(
                    {
                        "chapter_number": chapter_number,
                        "status": "diff_completed",
                        "differences": differences,
                    }
                )
            if schema_warning:
                emit_json_line({"status": "summary", "schema_warning": schema_warning})
            return 0

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
            if schema_warning:
                emit_json_line({"status": "summary", "schema_warning": schema_warning})
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
        summary = summarize_replay_results(results)
        if schema_warning:
            summary["schema_warning"] = schema_warning
        emit_json_line({"status": "summary", **summary})
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
