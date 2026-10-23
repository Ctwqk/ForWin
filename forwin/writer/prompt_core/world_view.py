"""Writer-facing templates of the POV world view; story content comes only from the view's fields.

Only ``forwin.world_view.types`` is imported from the view package: its builder imports this
module to measure its budget on the text the writer reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from forwin.protocol.book_state import ObserverType
from forwin.world_view.types import (
    AllowedReveal, ChapterWorldView, KnownFact, OnstageEntity, ViewClock, ViewLocation,
)


# A forbidden topic names an unrevealed secret's subjects, never its proposition.
FORBIDDEN_TOPIC_TEMPLATE = "涉及{subjects}的未揭示真相"
FORBIDDEN_TOPIC_SUBJECT_SEPARATOR = "、"
# The <<FORWIN_LOCATION>> instruction; the location table lists the ids it accepts.
SCENE_LOCATION_INSTRUCTION = "填地点表中的 id，新地点写 new:<名称>"
# Knowledge states (book_state.knowledge.KNOWLEDGE_STATES) as the writer reads them.
KNOWLEDGE_STATE_LABELS = {
    "heard": "听说",
    "suspects": "怀疑",
    "believes": "相信",
    "knows": "确知",
    "disbelieves": "不信",
    "misled": "被误导",
}
# Reader target states a reveal may state outright; the others are reached by hints only.
_STATED_READER_STATES = frozenset({"believes", "knows"})
_READER = ObserverType.READER.value
_NOT_FOR_READER = "POV 知道；本章不得向读者明示"


def forbidden_topic_text(subjects: Sequence[str]) -> str:
    """The topic of a secret the writer must not settle, built from its subjects' names."""
    return FORBIDDEN_TOPIC_TEMPLATE.format(subjects=FORBIDDEN_TOPIC_SUBJECT_SEPARATOR.join(subjects))


def view_location_rows(view: ChapterWorldView) -> list[ViewLocation]:
    """The location table: the POV's location first, even when trimming dropped it, then the rest."""
    rows = list(view.locations)
    if view.pov_location is not None and all(row.id != view.pov_location.id for row in rows):
        rows.insert(0, view.pov_location)
    return rows


def render_world_view(view: ChapterWorldView) -> str:
    """The view as the writer reads it; the view's builder trims to fit this text in its budget."""
    pov = view.pov_name or view.pov_ref
    location = view.pov_location
    lines = [
        "【POV 世界视图】",
        f"  · 视角人物：{pov}（按此人的视角叙述）" if pov else "  · 视角人物：未指定",
        f"  · POV 所在：{location.name or location.id}（{location.id}）" if location is not None else "  · POV 所在：未知",
        f"  · 当前故事时间：{view.now_label}",
    ]
    if view.expected_span_label:
        lines.append(f"  · 本章预期时长：{view.expected_span_label}")
    _extend(lines, "在场（只写场上任何人都能感知的状态）", [_onstage_row(entity) for entity in view.onstage])
    _extend(lines, "POV 的认知", [_knowledge_row(fact) for fact in view.pov_knowledge])
    # Entries are "<fact_ref>: <proposition>"; the writer needs only the proposition.
    _extend(lines, "读者已知", [entry.partition(": ")[2] or entry for entry in view.reader_known])
    _extend(lines, "本章允许揭示", [_reveal_row(view, reveal) for reveal in view.allowed_reveals])
    _extend(lines, "本章暗示（只埋线索，不要点破）", [hint.hint_text for hint in view.hints])
    _extend(
        lines, "禁止定论的话题（不要给出确定答案，也不要编造真相）",
        [topic.topic for topic in view.forbidden_topics],
    )
    _extend(
        lines, f"地点表（{SCENE_LOCATION_INSTRUCTION}）",
        [_location_row(row, view.pov_location) for row in view_location_rows(view)],
    )
    _extend(lines, "POV 知道的时限", [_clock_row(clock) for clock in view.pov_clocks])
    return "\n".join(lines)


def _world_view_section(context: Any) -> str | None:
    view = getattr(context, "world_view", None)
    return render_world_view(view) if view is not None else None


def _extend(lines: list[str], heading: str, rows: list[str]) -> None:
    rows = [row for row in rows if row]
    if rows:
        lines.append(f"  · {heading}：")
        lines.extend(f"    · {row}" for row in rows)


def _state_label(state: str) -> str:
    return KNOWLEDGE_STATE_LABELS.get(state, state)


def _onstage_row(entity: OnstageEntity) -> str:
    details = [
        f"位置 {entity.location_name}" if entity.location_name else "",
        f"生死 {entity.alive_status}" if entity.alive_status else "",
        f"束缚 {entity.restraint}" if entity.restraint else "",
        f"伤势 {entity.health}" if entity.health else "",
        f"公开身份 {entity.public_identity}" if entity.public_identity else "",
        f"随身 {'、'.join(entity.held_items)}" if entity.held_items else "",
    ]
    shown = "；".join(detail for detail in details if detail)
    return f"{entity.name or entity.ref}：{shown}" if shown else entity.name or entity.ref


def _knowledge_row(fact: KnownFact) -> str:
    if fact.pov_believes_false:
        return f"POV 以为（实为误信）：{fact.proposition}"
    if fact.pov_only_secret:
        return f"{fact.proposition}（{_NOT_FOR_READER}）"
    return f"{fact.proposition}（POV {_state_label(fact.state)}）"


def _reveal_row(view: ChapterWorldView, reveal: AllowedReveal) -> str:
    """A reveal by its observer: the reader, the POV or another actor."""
    state = _state_label(reveal.target_state)
    if reveal.observer == _READER:
        if reveal.target_state in _STATED_READER_STATES:
            return f"可以让读者{state}：{reveal.proposition}"
        return f"可以让读者{state}，只暗示不要明说：{reveal.proposition}"
    if view.pov_ref and reveal.observer == view.pov_ref:
        # In close third person the POV learning a truth tells the reader too: perception only.
        return (
            f"{view.pov_name or view.pov_ref}本章{state}{reveal.topic}："
            "只写 POV 察觉到的迹象与反应，不要向读者说出真相内容"
        )
    return f"{reveal.observer_name or reveal.observer}本章{state}：{reveal.proposition}；本章不得向读者明示"


def _location_row(location: ViewLocation, pov_location: ViewLocation | None) -> str:
    if pov_location is not None and location.id == pov_location.id:
        note = "POV 所在"
    elif location.minutes_from_pov is None:
        note = "POV 没有已知路线"
    else:
        note = f"POV 约 {location.minutes_from_pov} 分钟可达"
    return f"{location.id}：{location.name or location.id}（{note}）"


def _clock_row(clock: ViewClock) -> str:
    remaining = f"剩余 {clock.remaining_label}" if clock.remaining_label else "未定时"
    return f"{clock.label}：{remaining}" + (f"（{_NOT_FOR_READER}）" if clock.secret else "")


__all__ = [
    "FORBIDDEN_TOPIC_SUBJECT_SEPARATOR",
    "FORBIDDEN_TOPIC_TEMPLATE",
    "KNOWLEDGE_STATE_LABELS",
    "SCENE_LOCATION_INSTRUCTION",
    "_world_view_section",
    "forbidden_topic_text",
    "render_world_view",
    "view_location_rows",
]
