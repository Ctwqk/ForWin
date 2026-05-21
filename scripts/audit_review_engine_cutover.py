#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forwin.config import Config
from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory
from forwin.models.governance import DecisionEvent
from forwin.review_engine.audit import (
    collect_legacy_compatibility_static_counts,
    summarize_legacy_compatibility_audit,
    summarize_live_cutover_audit,
)


def _payload(row: DecisionEvent) -> dict[str, Any]:
    try:
        value = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit a review-engine live cutover pilot for legacy safety-net fallback."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--expected-chapters", type=int, default=60)
    parser.add_argument("--include-legacy-compat", action="store_true")
    args = parser.parse_args()

    config = Config.from_env()
    engine = get_engine(config.database_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = (
            session.query(DecisionEvent)
            .filter(
                DecisionEvent.project_id == args.project_id,
                DecisionEvent.event_type == DecisionEventType.REVIEW_ENGINE_DECISION,
                DecisionEvent.related_object_type == "chapter_review",
            )
            .order_by(DecisionEvent.chapter_number.asc(), DecisionEvent.created_at.asc())
            .all()
        )
        legacy_compat_rows = (
            session.query(DecisionEvent)
            .filter(
                DecisionEvent.project_id == args.project_id,
                DecisionEvent.event_type == DecisionEventType.LEGACY_COMPATIBILITY_USED,
            )
            .order_by(DecisionEvent.chapter_number.asc(), DecisionEvent.created_at.asc())
            .all()
            if args.include_legacy_compat
            else []
        )

    summary = summarize_live_cutover_audit(
        [
            {
                "chapter_number": int(row.chapter_number or 0),
                "payload": _payload(row),
            }
            for row in rows
        ],
        expected_chapters=args.expected_chapters,
    )
    if args.include_legacy_compat:
        summary["legacy_compat"] = summarize_legacy_compatibility_audit(
            [
                {
                    "chapter_number": int(row.chapter_number or 0),
                    "payload": _payload(row),
                }
                for row in legacy_compat_rows
            ],
            static_counts=collect_legacy_compatibility_static_counts(),
        )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
