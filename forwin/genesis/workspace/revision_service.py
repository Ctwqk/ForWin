from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project
from forwin.genesis.helpers import _initial_pack
from forwin.genesis.names_paths import _initial_pack_dummy_merge

def _json_load_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

class GenesisRevisionService:
    def active_revision(self, session: Session, project: Project) -> BookGenesisRevision | None:
        revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
        if not revision_id:
            return None
        return session.get(BookGenesisRevision, revision_id)

    def load_pack(self, revision: BookGenesisRevision | None) -> dict[str, Any]:
        return _initial_pack_dummy_merge(_json_load_object(getattr(revision, "pack_json", "{}")))

    def create_initial_pack(self, project: Project, brief_seed: dict[str, Any] | None = None) -> dict[str, Any]:
        return _initial_pack(project, brief_seed)
