from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from forwin.config import Config
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput

from .hard_floor_dict import ENDING_HOOK_MARKERS, MODEL_ARTIFACT_MARKERS


_GARBAGE_BLOCK_RE = re.compile(
    r"[^\u4e00-\u9fff，。！？；：、“”‘’（）《》…—\sA-Za-z0-9_-]{12,}"
)


class HardFloorResult(BaseModel):
    passed: bool
    fail_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def run_hard_floor(
    *,
    writer_output: WriterOutput,
    context_pack: ChapterContextPack,
    repo,
    project_id: str,
    chapter_number: int,
    config: Config,
) -> HardFloorResult:
    _ = repo
    fail_reasons: list[str] = []
    warning_reasons: list[str] = []
    checks: dict[str, bool] = {}
    body = str(writer_output.body or "")
    body_char_count = len(body)
    writer_char_count = int(writer_output.char_count or 0)
    min_chapter_chars = int(config.min_chapter_chars or 0)

    checks["chapter_length"] = body_char_count >= min_chapter_chars
    if not checks["chapter_length"]:
        fail_reasons.append("chapter_length")

    checks["char_count_consistent"] = writer_char_count == body_char_count
    if not checks["char_count_consistent"]:
        fail_reasons.append("char_count_consistent")

    checks["no_garbage"] = _no_garbage(body)
    if not checks["no_garbage"]:
        fail_reasons.append("no_garbage")

    checks["at_least_one_event"] = bool(
        writer_output.new_events
        or writer_output.state_changes
        or writer_output.thread_beats
    )
    if not checks["at_least_one_event"]:
        fail_reasons.append("at_least_one_event")

    hidden_hits = _must_not_reveal_hits(body, context_pack.must_not_reveal)
    checks["must_not_reveal"] = not hidden_hits
    if hidden_hits:
        fail_reasons.append("must_not_reveal")

    checks["ending_hook"] = _has_ending_hook(body)
    if not checks["ending_hook"]:
        warning_reasons.append("ending_hook")

    return HardFloorResult(
        passed=not fail_reasons,
        fail_reasons=fail_reasons,
        warning_reasons=warning_reasons,
        checks=checks,
        metadata={
            "project_id": project_id,
            "chapter_number": int(chapter_number or 0),
            "body_char_count": body_char_count,
            "writer_char_count": writer_char_count,
            "min_chapter_chars": min_chapter_chars,
            "must_not_reveal_hits": hidden_hits,
        },
    )


def _no_garbage(body: str) -> bool:
    if not body.strip():
        return False
    lowered = _artifact_scan_text(body)
    if any(marker.lower() in lowered for marker in MODEL_ARTIFACT_MARKERS):
        return False
    return _GARBAGE_BLOCK_RE.search(body) is None


def _artifact_scan_text(body: str) -> str:
    return body.lower().replace("：", ":")


def _must_not_reveal_hits(body: str, items: list[str]) -> list[str]:
    return [item for item in items if item and item in body]


def _has_ending_hook(body: str) -> bool:
    tail = body[-200:]
    return any(marker in tail for marker in ENDING_HOOK_MARKERS)
