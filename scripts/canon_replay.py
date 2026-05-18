#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
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
    emit_json_line(
        {
            "status": "parsed",
            "project_id": args.project_id,
            "from_chapter": args.from_chapter,
            "to_chapter": args.to_chapter,
            "mode": "persist" if args.persist else "dry_run",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
