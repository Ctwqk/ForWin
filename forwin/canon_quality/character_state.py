from __future__ import annotations

import re
from typing import Any

from .signals import CanonQualitySignal, CharacterStateTransition, make_signal_id

ANALYZER_VERSION = 2
CONTEXT_RADIUS = 96
HARD_TERMINAL_KEYWORDS = ("死亡", "已死", "身亡", "阵亡", "遗体", "死亡证明", "处决", "公开处刑", "枪决", "牺牲", "自爆", "以命换")
SOFT_TERMINAL_KEYWORDS = ("濒死", "临终", "奄奄一息", "被清除", "重伤后失踪")
ACTIVE_KEYWORDS = ("出现", "行动", "参与", "发言", "战斗", "协助", "带路", "推门而入", "拔枪", "突围")
BRIDGE_KEYWORDS = ("救出", "营救", "脱困", "释放", "恢复", "苏醒", "痊愈", "伪装", "伪造", "误判", "假死", "并未死亡", "死亡证明是伪造")
CUSTODY_CAPTURE_KEYWORDS = (
    "被关押",
    "被关在",
    "被关进",
    "被扣押",
    "被捕",
    "被固定",
    "被束缚",
    "被锁在",
    "被磁扣锁",
    "关在",
    "锁在",
    "束缚",
    "固定在",
    "重新关押",
    "重新关进",
)
CUSTODY_RELEASE_KEYWORDS = ("救出", "救下", "营救", "解救", "脱困", "释放", "获救")
CUSTODY_RECAPTURE_BRIDGE_KEYWORDS = (
    "再次被捕",
    "再度被捕",
    "重新被捕",
    "又被捕",
    "又被带走",
    "重新关押",
    "被重新关押",
    "再度关押",
    "重新关进",
    "再次关进",
    "又被关",
    "重新控制",
    "被押回",
    "被抓回",
    "被拖回",
)


def analyze_character_state_transitions(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    previous_transitions: list[dict[str, Any] | CharacterStateTransition] | None = None,
    central_characters: set[str] | None = None,
    recent_canon_text: str = "",
    recent_canon_chapter_number: int = 0,
) -> tuple[list[CanonQualitySignal], list[CharacterStateTransition]]:
    text = str(body or "")
    names = _candidate_names(text, previous_transitions or [], central_characters or set())
    signals: list[CanonQualitySignal] = []
    transitions: list[CharacterStateTransition] = []

    for name in sorted(names):
        if name not in text:
            continue
        evidence = [f"body:{max(0, text.find(name))}-{max(0, text.find(name)) + len(name)}"]
        hard_match = _first_terminal_keyword_match(text, name, HARD_TERMINAL_KEYWORDS)
        soft_match = _first_terminal_keyword_match(text, name, SOFT_TERMINAL_KEYWORDS)
        active_match = _first_context_keyword_match(text, name, ACTIVE_KEYWORDS)
        bridge_match = _first_context_keyword_match(text, name, BRIDGE_KEYWORDS)
        custody_capture_match = _first_custody_keyword_match(text, name, CUSTODY_CAPTURE_KEYWORDS, kind="capture")
        custody_release_match = _first_custody_keyword_match(text, name, CUSTODY_RELEASE_KEYWORDS, kind="release")
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
        elif (
            soft_match
            and not _context_has_bridge(soft_match["context"])
            and not _is_memory_clearance_false_positive(soft_match["context"], soft_match["keyword"])
        ):
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
        if custody_capture_match:
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="custody_state",
                    to_state="captured",
                    terminality="none",
                    can_participate=True,
                    evidence_refs=[f"body:{custody_capture_match['start']}-{custody_capture_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": custody_capture_match["keyword"],
                        "trigger_span": [custody_capture_match["start"], custody_capture_match["end"]],
                        "context_excerpt": custody_capture_match["context"],
                    },
                )
            )
        if custody_release_match:
            transitions.append(
                CharacterStateTransition(
                    project_id=project_id,
                    character_name=name,
                    chapter_number=chapter_number,
                    transition_type="custody_state",
                    to_state="free",
                    terminality="none",
                    can_participate=True,
                    evidence_refs=[f"body:{custody_release_match['start']}-{custody_release_match['end']}"],
                    payload={
                        "draft_id": draft_id,
                        "analyzer_version": ANALYZER_VERSION,
                        "trigger_keyword": custody_release_match["keyword"],
                        "trigger_span": [custody_release_match["start"], custody_release_match["end"]],
                        "context_excerpt": custody_release_match["context"],
                    },
                )
            )

    terminal_by_name: dict[str, dict[str, Any]] = {}
    release_by_name: dict[str, dict[str, Any]] = {}
    for raw in previous_transitions or []:
        item = raw.model_dump(mode="json") if isinstance(raw, CharacterStateTransition) else dict(raw)
        name = str(item.get("character_name") or "").strip()
        if not name:
            continue
        transition_type = str(item.get("transition_type") or "")
        to_state = str(item.get("to_state") or "")
        if transition_type == "custody_state" and to_state in {"free", "released", "rescued", "escaped"}:
            release_by_name[name] = item
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
    release_by_name.update(
        _recent_canon_release_facts(
            text=text,
            recent_canon_text=recent_canon_text,
            recent_canon_chapter_number=recent_canon_chapter_number,
            names=names,
        )
    )
    for name, previous in release_by_name.items():
        if name not in text:
            continue
        capture_match = _first_custody_keyword_match(text, name, CUSTODY_CAPTURE_KEYWORDS, kind="capture")
        if not capture_match:
            continue
        if _context_has_recapture_bridge(capture_match["context"]) or _context_has_recapture_bridge(text):
            continue
        subject = f"character:{name}"
        previous_chapter = int(previous.get("chapter_number", 0) or 0)
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "custody_state_regression", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="custody_state_regression",
                severity="error",
                target_scope="character",
                subject_key=subject,
                description=f"{name} 已在第 {previous_chapter} 章被救出/脱困，本章又写成被关押或被固定，但没有再次被捕或重新关押桥接。",
                evidence_refs=[f"chapter:{previous_chapter}", f"body:{capture_match['start']}-{capture_match['end']}"],
                span_start=int(capture_match["start"]),
                span_end=int(capture_match["end"]),
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
        names.update(extract_candidate_character_names(text))
    return names


def extract_candidate_character_names(text: str) -> set[str]:
    content = str(text or "")
    if not content:
        return set()
    keywords = (
        HARD_TERMINAL_KEYWORDS
        + SOFT_TERMINAL_KEYWORDS
        + ACTIVE_KEYWORDS
        + BRIDGE_KEYWORDS
        + CUSTODY_CAPTURE_KEYWORDS
        + CUSTODY_RELEASE_KEYWORDS
    )
    names: set[str] = set()
    for keyword in sorted(set(keywords), key=len, reverse=True):
        escaped = re.escape(keyword)
        for match in re.finditer(rf"([\u4e00-\u9fff]{{2,4}}).{{0,8}}{escaped}", content):
            name = _clean_candidate_name(match.group(1))
            if name:
                names.add(name)
        for match in re.finditer(rf"{escaped}(?:被[\u4e00-\u9fff]{{0,6}}的)?([\u4e00-\u9fff]{{2,4}})", content):
            name = _clean_candidate_name(match.group(1))
            if name:
                names.add(name)
    return names


def _clean_candidate_name(name: str) -> str:
    candidate = str(name or "").strip(" ，。！？；：、\"'“”‘’（）()[]【】")
    candidate = candidate.lstrip("的")
    if len(candidate) < 2 or len(candidate) > 4:
        return ""
    if any(
        marker in candidate
        for marker in (
            "救出",
            "救下",
            "营救",
            "解救",
            "脱困",
            "释放",
            "获救",
            "被关",
            "关押",
            "被捕",
            "扣押",
            "束缚",
            "固定",
        )
    ):
        return ""
    stopwords = {
        "系统",
        "记忆",
        "倒计时",
        "终端",
        "屏幕",
        "城市",
        "旧城",
        "档案",
        "公会",
        "协议",
        "核心",
        "窗口",
        "通道",
        "巡检员",
        "所有人",
        "他们",
        "她们",
        "两人",
        "二人",
    }
    if candidate in stopwords or any(candidate.endswith(word) for word in ("系统", "协议", "窗口", "通道")):
        return ""
    return candidate


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


def _first_terminal_keyword_match(text: str, name: str, keywords: tuple[str, ...]) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for occurrence in _name_occurrences(text, name):
        window_end = min(len(text), occurrence + len(name) + CONTEXT_RADIUS)
        context = text[occurrence:window_end]
        for keyword in keywords:
            offset = context.find(keyword)
            if offset < 0:
                continue
            start = occurrence + offset
            matches.append(
                {
                    "keyword": keyword,
                    "start": start,
                    "end": start + len(keyword),
                    "context": context,
                    "distance": abs(start - (occurrence + len(name))),
                }
            )
    if not matches:
        return None
    return sorted(matches, key=lambda item: (int(item["distance"]), int(item["start"])))[0]


def _first_custody_keyword_match(
    text: str,
    name: str,
    keywords: tuple[str, ...],
    *,
    kind: str,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for occurrence in _name_occurrences(text, name):
        window_start = max(0, occurrence - CONTEXT_RADIUS)
        window_end = min(len(text), occurrence + len(name) + CONTEXT_RADIUS)
        context = text[window_start:window_end]
        name_start = occurrence - window_start
        name_end = name_start + len(name)
        for keyword in keywords:
            search_from = 0
            while True:
                offset = context.find(keyword, search_from)
                if offset < 0:
                    break
                search_from = offset + max(1, len(keyword))
                if not _custody_keyword_targets_name(
                    context,
                    name_start=name_start,
                    name_end=name_end,
                    keyword_start=offset,
                    keyword_end=offset + len(keyword),
                    keyword=keyword,
                    kind=kind,
                ):
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


def _custody_keyword_targets_name(
    context: str,
    *,
    name_start: int,
    name_end: int,
    keyword_start: int,
    keyword_end: int,
    keyword: str,
    kind: str,
) -> bool:
    if keyword_start >= name_end:
        between = context[name_end:keyword_start]
        if kind == "release":
            return len(between) <= 8 and ("被" in between or keyword in {"脱困", "释放", "获救"})
        return len(between) <= 12
    if keyword_end <= name_start:
        between = context[keyword_end:name_start]
        if kind == "release":
            return len(between) <= 18
        return len(between) <= 12 and ("的" in between or not between.strip())
    return True


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


def _context_has_recapture_bridge(context: str) -> bool:
    return any(keyword in str(context or "") for keyword in CUSTODY_RECAPTURE_BRIDGE_KEYWORDS)


def _recent_canon_release_facts(
    *,
    text: str,
    recent_canon_text: str,
    recent_canon_chapter_number: int,
    names: set[str],
) -> dict[str, dict[str, Any]]:
    if not recent_canon_text.strip():
        return {}
    facts: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        if name not in text or name not in recent_canon_text:
            continue
        match = _first_custody_keyword_match(recent_canon_text, name, CUSTODY_RELEASE_KEYWORDS, kind="release")
        if not match:
            continue
        facts[name] = {
            "character_name": name,
            "chapter_number": int(recent_canon_chapter_number or 0),
            "transition_type": "custody_state",
            "to_state": "free",
            "terminality": "none",
            "can_participate": True,
            "payload": {
                "analyzer_version": ANALYZER_VERSION,
                "trigger_keyword": match["keyword"],
                "context_excerpt": match["context"],
                "source": "recent_canon_text",
            },
        }
    return facts


def _is_memory_clearance_false_positive(context: str, keyword: str) -> bool:
    if keyword != "被清除":
        return False
    local = str(context or "")
    trigger_index = local.find(keyword)
    if trigger_index < 0:
        return False
    nearby = local[max(0, trigger_index - 24) : trigger_index + len(keyword) + 12]
    return any(marker in nearby for marker in ("记忆", "档案", "记录", "痕迹", "数据"))


def _previous_terminal_transition_is_reliable(item: dict[str, Any]) -> bool:
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        return True
    trigger_keyword = str(payload.get("trigger_keyword") or "")
    context_excerpt = str(payload.get("context_excerpt") or "")
    character_name = str(item.get("character_name") or "")
    if trigger_keyword and context_excerpt and character_name:
        trigger_index = context_excerpt.find(trigger_keyword)
        name_index = context_excerpt.find(character_name)
        if trigger_index >= 0 and name_index >= 0 and trigger_index < name_index:
            return False
        if _is_memory_clearance_false_positive(context_excerpt, trigger_keyword):
            return False
    if int(payload.get("analyzer_version") or 0) >= ANALYZER_VERSION:
        return True
    if trigger_keyword:
        return True
    # Older deterministic analyzer rows only stored draft_id and were produced
    # by a chapter-wide keyword pass. They are too noisy to block later canon.
    if payload.get("draft_id"):
        return False
    terminality = str(item.get("terminality") or "")
    to_state = str(item.get("to_state") or "")
    if terminality == "hard_terminal" or to_state == "dead":
        return True
    return False
