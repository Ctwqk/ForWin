from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ReplayState(BaseModel):
    schema_version: str = "canon_replay.v1"
    project_id: str
    from_chapter: int
    to_chapter: int
    started_at: str = Field(default_factory=_now)
    last_updated_at: str = Field(default_factory=_now)
    chapters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=lambda: {"completed": 0, "errors": 0, "skipped": 0})
    summary: dict[str, Any] = Field(default_factory=dict)

    def mark_completed(self, chapter_number: int, result_summary: dict[str, Any]) -> ReplayState:
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "completed",
            "result_summary": result_summary,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def mark_error(self, chapter_number: int, error_message: str) -> ReplayState:
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "error",
            "error_message": error_message,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def mark_skipped(self, chapter_number: int, reason: str) -> ReplayState:
        data = self.model_copy(deep=True)
        data.chapters[str(chapter_number)] = {
            "status": "skipped_due_to_cap" if reason == "cost_cap" else "skipped",
            "reason": reason,
            "last_updated_at": _now(),
        }
        data._recount()
        return data

    def should_skip_completed(self, chapter_number: int, *, force_rerun: bool) -> bool:
        return not force_rerun and self.chapters.get(str(chapter_number), {}).get("status") == "completed"

    def _recount(self) -> None:
        completed = sum(1 for item in self.chapters.values() if item.get("status") == "completed")
        errors = sum(1 for item in self.chapters.values() if item.get("status") == "error")
        skipped = sum(1 for item in self.chapters.values() if str(item.get("status", "")).startswith("skipped"))
        self.totals = {"completed": completed, "errors": errors, "skipped": skipped}
        self.last_updated_at = _now()

    @staticmethod
    def prepare_existing_state(
        *,
        path: Path,
        project_id: str,
        from_chapter: int,
        to_chapter: int,
        resume: bool,
        force_restart: bool,
    ) -> ReplayState:
        if path.exists() and not resume and not force_restart:
            raise RuntimeError(f"state file already exists: {path}")
        if force_restart:
            return ReplayState(project_id=project_id, from_chapter=from_chapter, to_chapter=to_chapter)
        if resume:
            if not path.exists():
                raise RuntimeError(f"cannot resume missing state file: {path}")
            return ReplayState.model_validate_json(path.read_text(encoding="utf-8"))
        return ReplayState(project_id=project_id, from_chapter=from_chapter, to_chapter=to_chapter)


class ReplayRangeOptions(BaseModel):
    persist: bool = False
    mode: str = "dry_run"
    resume: bool = False
    force_restart: bool = False
    force_rerun: bool = False
    abort_on_error: bool = True
    cost_cap_usd: float | None = None
    no_cost_cap: bool = False


def state_file_path(*, root: Path, project_id: str, from_chapter: int, to_chapter: int) -> Path:
    return root / "canon_replay" / project_id / f"{from_chapter}-{to_chapter}.state.json"


def write_state_atomic(path: Path, state: ReplayState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
