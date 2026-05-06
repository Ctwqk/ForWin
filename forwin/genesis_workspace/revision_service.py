from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project

from .fallbacks import initial_pack, initial_pack_dummy_merge


class StaleGenesisRevisionError(RuntimeError):
    pass


def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def ensure_revision_is_current(_session: Session, project: Project, revision: BookGenesisRevision) -> None:
    current_revision_id = str(getattr(project, "active_genesis_revision_id", "") or "")
    expected_revision_id = str(getattr(revision, "id", "") or "")
    if current_revision_id and expected_revision_id and current_revision_id != expected_revision_id:
        raise StaleGenesisRevisionError("Genesis 已被新的操作更新，请刷新后重试。")


class GenesisRevisionService:
    def active_revision(self, session: Session, project: Project) -> BookGenesisRevision | None:
        revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
        if not revision_id:
            return None
        return session.get(BookGenesisRevision, revision_id)

    def load_pack(self, revision: BookGenesisRevision | None) -> dict[str, Any]:
        return initial_pack_dummy_merge(_json_load_object(getattr(revision, "pack_json", "{}")))

    def create_initial_pack(self, project: Project, brief_seed: dict[str, Any] | None = None) -> dict[str, Any]:
        return initial_pack(project, brief_seed)

