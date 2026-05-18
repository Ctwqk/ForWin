from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable

from sqlalchemy import select

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CharacterStateTransitionRow, CountdownLedgerRow

SUPERSEDED_BY = "chapter_review_form_migration"


def is_form_sourced(payload: dict[str, Any]) -> bool:
    return str(payload.get("source") or "") == "chapter_review_form"


def mark_payload_superseded(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if not is_form_sourced(result):
        result.setdefault("superseded_by", SUPERSEDED_BY)
    return result


def summarize_rows(rows: Iterable[Any]) -> dict[str, int]:
    summary = {
        "form_sourced": 0,
        "legacy_sourced": 0,
        "already_superseded": 0,
        "total": 0,
    }
    for row in rows:
        payload = _row_payload(row)
        summary["total"] += 1
        if is_form_sourced(payload):
            summary["form_sourced"] += 1
        else:
            summary["legacy_sourced"] += 1
        if payload.get("superseded_by"):
            summary["already_superseded"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark legacy canon-quality rows as superseded by Chapter Review Form.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing. This is the default unless --apply is set.")
    parser.add_argument("--project-id", default="", help="Limit migration to one project id.")
    parser.add_argument("--apply", action="store_true", help="Persist supersede markers.")
    args = parser.parse_args(argv)

    config = Config.from_env()
    engine = get_engine(config.database_url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            character_rows = _load_rows(session, CharacterStateTransitionRow, project_id=args.project_id)
            countdown_rows = _load_rows(session, CountdownLedgerRow, project_id=args.project_id)
            before = {
                "character_state_transitions": summarize_rows(character_rows),
                "countdown_ledgers": summarize_rows(countdown_rows),
            }
            changed = {
                "character_state_transitions": _mark_rows(character_rows),
                "countdown_ledgers": _mark_rows(countdown_rows),
            }
            after = {
                "character_state_transitions": summarize_rows(character_rows),
                "countdown_ledgers": summarize_rows(countdown_rows),
            }
            print(
                json.dumps(
                    {
                        "mode": "apply" if args.apply else "dry-run",
                        "project_id": args.project_id,
                        "before": before,
                        "changed": changed,
                        "after": after,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            if args.apply:
                session.commit()
            else:
                session.rollback()
    finally:
        engine.dispose()
    return 0


def _load_rows(session: Any, model: Any, *, project_id: str) -> list[Any]:
    query = select(model)
    if project_id:
        query = query.where(model.project_id == project_id)
    return list(session.execute(query.order_by(model.project_id.asc(), model.chapter_number.asc())).scalars().all())


def _mark_rows(rows: Iterable[Any]) -> int:
    changed = 0
    for row in rows:
        payload = _row_payload(row)
        updated = mark_payload_superseded(payload)
        if updated != payload:
            row.payload_json = json.dumps(updated, ensure_ascii=False)
            changed += 1
    return changed


def _row_payload(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = row.get("payload", row)
        return dict(payload) if isinstance(payload, dict) else {}
    raw = getattr(row, "payload_json", "{}") or "{}"
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    sys.exit(main())
