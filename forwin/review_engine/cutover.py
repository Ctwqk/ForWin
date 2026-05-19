from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.review_engine.types import Decision


@dataclass(frozen=True)
class CutoverSelection:
    live: Decision
    shadow: Decision
    live_source: str
    shadow_source: str
    engine_live: bool


def engine_live_enabled(config: Any, project_id: str) -> bool:
    if not bool(getattr(config, "review_engine_live_cutover_enabled", False)):
        return False
    allowlist = {
        str(item or "").strip()
        for item in list(
            getattr(config, "review_engine_live_cutover_project_allowlist", []) or []
        )
        if str(item or "").strip()
    }
    return not allowlist or str(project_id or "").strip() in allowlist


def select_cutover_pair(
    *,
    project_id: str,
    legacy_decision: Decision,
    engine_decision: Decision,
    config: Any,
) -> CutoverSelection:
    if engine_live_enabled(config, project_id):
        return CutoverSelection(
            live=engine_decision,
            shadow=legacy_decision,
            live_source="engine",
            shadow_source="legacy",
            engine_live=True,
        )
    return CutoverSelection(
        live=legacy_decision,
        shadow=engine_decision,
        live_source="legacy",
        shadow_source="engine",
        engine_live=False,
    )
