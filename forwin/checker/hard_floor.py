from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy

from .hard_floor_dict import ENDING_HOOK_MARKERS, MODEL_ARTIFACT_MARKERS
from .pulp_beat import pulp_track_for_genre, verify_pulp_beats


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
    policy: RuntimePolicy,
) -> HardFloorResult:
    _ = repo
    fail_reasons: list[str] = []
    warning_reasons: list[str] = []
    checks: dict[str, bool] = {}
    body = str(writer_output.body or "")
    body_char_count = len(body)
    writer_char_count = int(writer_output.char_count or 0)
    min_chapter_chars = int(policy.chapter_length.min_chars)

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

    pulp_beat_track = pulp_track_for_genre(context_pack.genre)
    experience_plan = context_pack.chapter_experience_plan
    planned_reward_tags = tuple(
        str(tag)
        for tag in (
            experience_plan.planned_reward_tags if experience_plan is not None else ()
        )
    )
    pulp_beats = verify_pulp_beats(
        body,
        track=pulp_beat_track,
        reward_tags=planned_reward_tags,
    )
    checks["pulp_visible_payoff"] = pulp_beats.visible_payoff_present
    if (
        policy.quality_profile == "pulp"
        and not pulp_beats.visible_payoff_present
    ):
        warning_reasons.append("pulp_visible_payoff")

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
            "pulp_beat_track": pulp_beat_track or "body_inferred",
            "planned_reward_tags": list(planned_reward_tags),
            "pulp_beat": pulp_beats.model_dump(mode="json"),
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
