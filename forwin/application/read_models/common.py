from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session



DisplayDatetime = Callable[[datetime | None], str]
_GENESIS_STAGE_ORDER = (
    "brief",
    "world",
    "map",
    "story_engine",
    "book_blueprint",
    "bootstrap",
)
_PROJECT_DETAIL_CHAPTER_PREVIEW_LIMIT = 60
_PROJECT_SUMMARY_CHAPTER_PREVIEW_LIMIT = 3


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalized_project_ids(project_ids: list[str]) -> list[str]:
    return [
        str(project_id or "").strip()
        for project_id in project_ids
        if str(project_id or "").strip()
    ]


def _recent_rows_by_project(
    session: Session,
    model,
    project_column,
    project_ids: list[str],
    *,
    order_by: tuple[Any, ...],
    limit: int,
) -> dict[str, list[Any]]:
    ids = _normalized_project_ids(project_ids)
    normalized_limit = max(1, int(limit or 1))
    if not ids:
        return {}
    ranked = (
        select(
            model.id.label("row_id"),
            project_column.label("project_id"),
            func.row_number()
            .over(partition_by=project_column, order_by=order_by)
            .label("rn"),
        )
        .where(project_column.in_(ids))
        .subquery()
    )
    rows = session.execute(
        select(model, ranked.c.rn)
        .join(ranked, model.id == ranked.c.row_id)
        .where(ranked.c.rn <= normalized_limit)
        .order_by(ranked.c.project_id.asc(), ranked.c.rn.asc())
    ).all()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row, _rn in rows:
        grouped[str(getattr(row, project_column.key) or "")].append(row)
    return dict(grouped)


def _latest_rows_by_project(
    session: Session,
    model,
    project_column,
    project_ids: list[str],
    *,
    order_by: tuple[Any, ...],
) -> dict[str, Any]:
    grouped = _recent_rows_by_project(
        session,
        model,
        project_column,
        project_ids,
        order_by=order_by,
        limit=1,
    )
    return {project_id: rows[0] for project_id, rows in grouped.items() if rows}


def _json_list_strings(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in payload if item is not None]


def _json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_list(raw: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw or "[]") or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in payload if isinstance(item, dict)]


__all__ = [
    "_deep_merge_dict",
    "_normalized_project_ids",
    "_recent_rows_by_project",
    "_latest_rows_by_project",
    "_json_list_strings",
    "_json_object",
    "_load_json_list",
]
