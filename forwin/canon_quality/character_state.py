from __future__ import annotations

from typing import Any

from .signals import CanonQualitySignal, CharacterStateTransition, make_signal_id

ANALYZER_VERSION = 2
CONTEXT_RADIUS = 96
HARD_TERMINAL_KEYWORDS = ("死亡", "已死", "身亡", "阵亡", "遗体", "死亡证明", "处决", "公开处刑", "枪决", "牺牲", "自爆", "以命换")
SOFT_TERMINAL_KEYWORDS = ("濒死", "临终", "奄奄一息", "被清除", "重伤后失踪")
ACTIVE_KEYWORDS = ("出现", "行动", "参与", "发言", "战斗", "协助", "带路", "推门而入", "拔枪", "突围")
BRIDGE_KEYWORDS = ("救出", "营救", "脱困", "释放", "恢复", "苏醒", "痊愈", "伪装", "伪造", "误判", "假死", "并未死亡", "死亡证明是伪造")


def analyze_character_state_transitions(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_transitions: list[dict[str, Any] | CharacterStateTransition] | None = None,
    central_characters: set[str] | None = None,
) -> tuple[list[CanonQualitySignal], list[CharacterStateTransition]]:
    text = str(body or "")
    names = _candidate_names(text, previous_transitions or [], central_characters or set())
    signals: list[CanonQualitySignal] = []
    transitions: list[CharacterStateTransition] = []

    for name in sorted(names):
        if name not in text:
            continue
        evidence = [f"body:{max(0, text.find(name))}-{max(0, text.find(name)) + len(name)}"]
        hard_match = _first_context_keyword_match(text, name, HARD_TERMINAL_KEYWORDS)
        soft_match = _first_context_keyword_match(text, name, SOFT_TERMINAL_KEYWORDS)
        active_match = _first_context_keyword_match(text, name, ACTIVE_KEYWORDS)
        bridge_match = _first_context_keyword_match(text, name, BRIDGE_KEYWORDS)
        if hard_match and not _context_has_bridge(hard_match["context"]):
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="life_state",
                    to_state="dead",
                    terminality="hard_terminal",
                    can_participate=False,
                    evidence_refs=[f"body:{hard_match['start']}-{hard_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": hard_match["keyword"],
                        "trigger_span": [hard_match["start"], hard_match["end"]],
                        "context_excerpt": hard_match["context"],
                    },
                )
            )
        elif soft_match and not _context_has_bridge(soft_match["context"]):
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="life_state",
                    to_state="terminally_wounded",
                    terminality="soft_terminal",
                    can_participate=False,
                    evidence_refs=[f"body:{soft_match['start']}-{soft_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": soft_match["keyword"],
                        "trigger_span": [soft_match["start"], soft_match["end"]],
                        "context_excerpt": soft_match["context"],
                    },
                )
            )
        if active_match:
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="participation",
                    to_state="active",
                    terminality="none",
                    can_participate=True,
                    evidence_refs=[f"body:{active_match['start']}-{active_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": active_match["keyword"],
                        "trigger_span": [active_match["start"], active_match["end"]],
                        "context_excerpt": active_match["context"],
                    },
                )
            )
        if bridge_match:
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="bridge_event",
                    to_state="bridge_explained",
                    terminality="none",
                    can_participate=True,
                    evidence_refs=[f"body:{bridge_match['start']}-{bridge_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": bridge_match["keyword"],
                        "trigger_span": [bridge_match["start"], bridge_match["end"]],
                        "context_excerpt": bridge_match["context"],
                    },
                )
            )

    terminal_by_name: dict[str, dict[str, Any]] = {}
    for raw in previous_transitions or []:
        item = raw.model_dump(mode="json") if isinstance(raw, CharacterStateTransition) else dict(raw)
        name = str(item.get("character_name") or "").strip()
        if not name:
            continue
        terminality = str(item.get("terminality") or "")
        can_participate = bool(item.get("can_participate", True))
        if terminality in {"hard_terminal", "soft_terminal"} or not can_participate:
            if not _previous_terminal_transition_is_reliable(item):
                continue
            terminal_by_name[name] = item

    for name, previous in terminal_by_name.items():
        if name not in text or not _first_context_keyword_match(text, name, ACTIVE_KEYWORDS):
            continue
        if _first_context_keyword_match(text, name, BRIDGE_KEYWORDS):
            continue
        subject = f"character:{name}"
        previous_chapter = int(previous.get("chapter_number", 0) or 0)
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "terminal_state_active_conflict", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="terminal_state_active_conflict",
                severity="error",
                target_scope="character",
                subject_key=subject,
                description=f"{name} 在第 {previous_chapter} 章进入终止态后，本章无桥接事件仍作为活跃参与者出现。",
                evidence_refs=[f"chapter:{previous_chapter}", f"body:{text.find(name)}-{text.find(name) + len(name)}"],
                span_start=text.find(name),
                span_end=text.find(name) + len(name),
                payload={"draft_id": draft_id, "previous_transition": previous},
            )
        )
    return signals, transitions


def _candidate_names(
    text: str,
    previous_transitions: list[dict[str, Any] | CharacterStateTransition],
    central_characters: set[str],
) -> set[str]:
    names = {str(item or "").strip() for item in central_characters if str(item or "").strip()}
    for raw in previous_transitions:
        item = raw.model_dump(mode="json") if isinstance(raw, CharacterStateTransition) else dict(raw)
        name = str(item.get("character_name") or "").strip()
        if name:
            names.add(name)
    if not names:
        for marker in ("沈砚", "林澈", "顾岚", "洛庭若", "林远"):
            if marker in text:
                names.add(marker)
    return names


def _first_context_keyword_match(text: str, name: str, keywords: tuple[str, ...]) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for occurrence in _name_occurrences(text, name):
        window_start = max(0, occurrence - CONTEXT_RADIUS)
        window_end = min(len(text), occurrence + len(name) + CONTEXT_RADIUS)
        context = text[window_start:window_end]
        for keyword in keywords:
            offset = context.find(keyword)
            if offset < 0:
                continue
            start = window_start + offset
            matches.append(
                {
                    "keyword": keyword,
                    "start": start,
                    "end": start + len(keyword),
                    "context": context,
                    "distance": min(abs(start - occurrence), abs(start - (occurrence + len(name)))),
                }
            )
    if not matches:
        return None
    return sorted(matches, key=lambda item: (int(item["distance"]), int(item["start"])))[0]


def _name_occurrences(text: str, name: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        index = text.find(name, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + max(1, len(name))


def _context_has_bridge(context: str) -> bool:
    return any(keyword in context for keyword in BRIDGE_KEYWORDS)


def _previous_terminal_transition_is_reliable(item: dict[str, Any]) -> bool:
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        return True
    if int(payload.get("analyzer_version") or 0) >= ANALYZER_VERSION:
        return True
    if payload.get("trigger_keyword"):
        return True
    # Older deterministic analyzer rows only stored draft_id and were produced
    # by a chapter-wide keyword pass. They are too noisy to block later canon.
    if payload.get("draft_id"):
        return False
    return True
