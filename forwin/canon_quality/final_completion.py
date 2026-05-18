from __future__ import annotations

import re

from .rule_profile import (
    LEGACY_CURRENT_BOOK_GLOSSARY,
    CanonGlossary,
    canon_glossary_from_payload,
)
from .signals import CanonQualitySignal, make_signal_id

FINAL_RESOLUTION_KEYWORDS = (
    "系统关闭",
    "系统已关闭",
    "系统已终止",
    "核心关闭",
    "主控程序已终止",
    "重置停止",
    "重置已取消",
    "倒计时结束",
    "倒计时归零",
    "危机解除",
    "重置序列中止",
    "重置被中止",
    "重置程序终止",
    "终止重置程序",
    "核心停摆",
    "系统停摆",
    "核心停止",
    "证据公开完成",
    "档案已经公开",
    "真相已经公开",
    "真相公开了",
    "全城居民看见",
    "直播画面传遍全城",
    "证据传遍全城",
    "档案传遍全城",
)

UNRESOLVED_FINAL_HOOK_KEYWORDS = (
    "新的起点",
    "未知",
    "等待着他",
    "还没有结束",
    "机械运转声",
    "包围",
    "追兵",
    "脚步声越来越近",
    "枪声",
    "爆炸",
    "倒下",
    "快跑",
    "活捉",
    "封锁所有出口",
    "门被撞开",
    "被困",
    "困在",
    "芯片损坏",
    "芯片边缘断裂",
    "断裂了一半",
    "闸门正在缓缓关闭",
    "闸门",
    "彻底的黑暗",
    "陷入了彻底的黑暗",
    "最后一丝光线被切断",
    "封闭的第五层",
    "第五层陷入",
    "铁门纹丝不动",
    "半截钥匙",
    "缺少了最关键",
    "装甲车",
    "驶近",
    "人群开始骚动",
    "念出了档案上第一行字",
    "捕获",
    "追捕",
)

HARD_UNRESOLVED_FINAL_HOOK_PATTERNS = (
    re.compile(r"(脚步声|追兵|枪声|巡检员|守卫|武装).{0,18}(越来越近|逼近|包围|追来|封锁|上膛)"),
    re.compile(r"(铁门|闸门|门|入口|出口).{0,18}(打不开|纹丝不动|封死|锁住)"),
    re.compile(r"(闸门|出口|入口).{0,18}(正在|开始|缓缓).{0,8}关闭"),
    re.compile(r"(半截|断裂|损坏|缺少|失效|打飞).{0,18}(钥匙|齿片|芯片|解码器)"),
    re.compile(r"(钥匙|齿片|芯片|解码器).{0,18}(半截|断裂|损坏|缺少|失效|打飞)"),
)

SOFT_UNRESOLVED_FINAL_HOOK_PATTERNS = (
    re.compile(r"(发现|得知|找到|获得|看见|意识到).{0,24}(线索|警告|坐标|入口|地图|笔记|密码|钥匙|芯片|秘密)"),
    re.compile(r"(真正的|真正).{0,16}(档案|核心|入口|答案|秘密).{0,20}(不在|在)"),
    re.compile(r"(不要相信|不能相信).{0,12}(任何人|[\u4e00-\u9fff]{2,4})"),
)

FINAL_CRISIS_KEYWORDS = (
    "重置",
    "倒计时",
    "主线危机",
    "危机",
    "真相",
)

PENDING_RESOLUTION_PATTERNS = (
    re.compile(r"(决定|准备|打算|正要|即将|开始|试图|尝试|必须|将要).{0,18}(公开|发布|播放|直播|宣布|关闭|阻止|停止|解除|中止|念出).{0,12}"),
    re.compile(r"(公开|发布|播放|直播|宣布|关闭|阻止|停止|解除|中止).{0,18}(之前|前|尚未|还没|未能|来不及|仍在|仍然).{0,12}"),
    re.compile(r"念出.{0,12}(第一行|第一段|开头|第一页)"),
)

POST_RESOLUTION_UNFINISHED_PATTERNS = (
    re.compile(r"(去|前往|赶往|回到|进入).{0,12}(核心区|控制室|档案库|地下层|调度室|广播室).{0,30}(最后|剩下|剩余|交给|交付|公开|关闭|阻止|完成)"),
    re.compile(r"(最后一段|最后一份|剩余|剩下).{0,12}(记忆记录|档案|证据|芯片|钥匙).{0,20}(交给|交付|公开|带去|送到|提交)"),
)


def analyze_final_completion(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str = "",
    body: str,
    title: str = "",
    summary: str = "",
    is_final_chapter: bool = False,
    canon_glossary: CanonGlossary | dict | None = None,
) -> list[CanonQualitySignal]:
    if not is_final_chapter:
        return []
    text = str(body or "")
    title_text = str(title or "")
    summary_text = str(summary or "")
    combined_text = "\n".join(item for item in (title_text, summary_text, text) if item)
    resolution_keywords = _final_resolution_keywords(canon_glossary)
    crisis_keywords = _final_crisis_keywords(canon_glossary)
    if not combined_text:
        return []
    tail_start = max(0, len(text) - 700)
    tail = text[tail_start:]
    unresolved_context = "\n".join(item for item in (title_text, summary_text, tail) if item)
    scan_context = unresolved_context
    if _has_final_resolution(unresolved_context, keywords=resolution_keywords):
        scan_context = unresolved_context[_last_final_resolution_end(unresolved_context, keywords=resolution_keywords) :]
    post_resolution_scan_context = tail if _has_final_resolution(combined_text, keywords=resolution_keywords) else ""
    first_tail_resolution_end = _first_final_resolution_end(tail, keywords=resolution_keywords)
    post_resolution_candidates = [post_resolution_scan_context]
    if first_tail_resolution_end:
        post_resolution_candidates.insert(0, tail[first_tail_resolution_end:])
    post_resolution_task = ""
    for candidate in post_resolution_candidates:
        post_resolution_task = _first_pattern_match(candidate, POST_RESOLUTION_UNFINISHED_PATTERNS)
        if post_resolution_task:
            break
    matched = [keyword for keyword in UNRESOLVED_FINAL_HOOK_KEYWORDS if keyword in scan_context]
    if post_resolution_task:
        matched.append(post_resolution_task)
    hard_pattern_match = _first_pattern_match(scan_context, HARD_UNRESOLVED_FINAL_HOOK_PATTERNS)
    if hard_pattern_match:
        matched.append(hard_pattern_match)
    soft_pattern_match = _first_pattern_match(scan_context, SOFT_UNRESOLVED_FINAL_HOOK_PATTERNS)
    if soft_pattern_match and not _has_final_resolution(combined_text):
        matched.append(soft_pattern_match)
    pending_match = _pending_resolution_match(scan_context)
    subject = "book:finale"
    repair_hint = (
        "终章不能以主线危机、追杀、未知装置或新谜团收尾。"
        "请明确写出主线危机如何被关闭、倒计时如何解除，以及核心真相如何已经公开或付清代价。"
    )
    if matched:
        evidence_ref, span_start, span_end = _first_evidence(
            text=text,
            summary=summary_text,
            title=title_text,
            keywords=matched,
            body_window_start=tail_start,
        )
        return [
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "final_hook_unresolved", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="final_hook_unresolved",
                severity="error",
                target_scope="book",
                subject_key=subject,
                description=f"终章尾部仍保留未解决主危机或新钩子：{', '.join(matched[:4])}。修复要求：{repair_hint}",
                evidence_refs=[evidence_ref],
                span_start=span_start,
                span_end=span_end,
                payload={"draft_id": draft_id, "matched_keywords": matched, "repair_hint": repair_hint},
            )
        ]

    if pending_match and not _has_final_resolution(unresolved_context, keywords=resolution_keywords):
        evidence_ref, span_start, span_end = _pending_evidence(
            text=text,
            summary=summary_text,
            title=title_text,
            context=unresolved_context,
            matched_text=pending_match,
            body_window_start=tail_start,
        )
        return [
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "final_resolution_pending", subject),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="final_resolution_pending",
                severity="error",
                target_scope="book",
                subject_key=subject,
                description=(
                    f"终章只写到主线解决动作正在准备或刚开始，尚未完成：{pending_match}。"
                    f"修复要求：{repair_hint}"
                ),
                evidence_refs=[evidence_ref],
                span_start=span_start,
                span_end=span_end,
                payload={"draft_id": draft_id, "matched_text": pending_match, "repair_hint": repair_hint},
            )
        ]

    if _has_final_resolution(combined_text, keywords=resolution_keywords):
        return []
    crisis_matched = [keyword for keyword in crisis_keywords if keyword in combined_text]
    if not crisis_matched:
        return []
    evidence_ref, span_start, span_end = _first_evidence(
        text=text,
        summary=summary_text,
        title=title_text,
        keywords=crisis_matched,
        body_window_start=0,
    )
    return [
        CanonQualitySignal(
            signal_id=make_signal_id(project_id, chapter_number, "final_resolution_missing", subject),
            project_id=project_id,
            chapter_number=chapter_number,
            signal_type="final_resolution_missing",
            severity="error",
            target_scope="book",
            subject_key=subject,
            description=(
                "终章涉及主线危机，但没有明确关闭主危机、解除倒计时或公开核心真相。"
                f"修复要求：{repair_hint}"
            ),
            evidence_refs=[evidence_ref],
            span_start=span_start,
            span_end=span_end,
            payload={"draft_id": draft_id, "matched_keywords": crisis_matched, "repair_hint": repair_hint},
        )
    ]


def _final_resolution_keywords(canon_glossary: CanonGlossary | dict | None) -> tuple[str, ...]:
    glossary = canon_glossary_from_payload(canon_glossary or {})
    phrases: list[str] = list(FINAL_RESOLUTION_KEYWORDS)
    for profile in glossary.countdowns.values():
        phrases.extend(str(item).strip() for item in profile.resolution_phrases if str(item).strip())
    if not glossary.countdowns and not glossary.final_crisis_terms:
        for profile in LEGACY_CURRENT_BOOK_GLOSSARY.countdowns.values():
            phrases.extend(str(item).strip() for item in profile.resolution_phrases if str(item).strip())
    return tuple(dict.fromkeys(item for item in phrases if item))


def _final_crisis_keywords(canon_glossary: CanonGlossary | dict | None) -> tuple[str, ...]:
    glossary = canon_glossary_from_payload(canon_glossary or {})
    phrases = [*FINAL_CRISIS_KEYWORDS, *glossary.final_crisis_terms]
    if not glossary.countdowns and not glossary.final_crisis_terms:
        phrases.extend(LEGACY_CURRENT_BOOK_GLOSSARY.final_crisis_terms)
        phrases.extend(LEGACY_CURRENT_BOOK_GLOSSARY.mechanism_terms)
    return tuple(dict.fromkeys(item for item in phrases if item))


def _has_final_resolution(text: str, *, keywords: tuple[str, ...] | None = None) -> bool:
    return _last_final_resolution_end(text, keywords=keywords) > 0


def _last_final_resolution_end(text: str, *, keywords: tuple[str, ...] | None = None) -> int:
    pending_spans = _pending_resolution_spans(text)
    last_end = -1
    for keyword in keywords or FINAL_RESOLUTION_KEYWORDS:
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            end = index + len(keyword)
            if not any(span_start <= index and end <= span_end for span_start, span_end in pending_spans):
                last_end = max(last_end, end)
            start = end
    return max(0, last_end)


def _first_final_resolution_end(text: str, *, keywords: tuple[str, ...] | None = None) -> int:
    pending_spans = _pending_resolution_spans(text)
    first: tuple[int, int] | None = None
    for keyword in keywords or FINAL_RESOLUTION_KEYWORDS:
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            end = index + len(keyword)
            if not any(span_start <= index and end <= span_end for span_start, span_end in pending_spans):
                if first is None or index < first[0]:
                    first = (index, end)
            start = end
    return first[1] if first else 0


def _pending_resolution_match(text: str) -> str:
    for pattern in PENDING_RESOLUTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def _first_pattern_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def _pending_resolution_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in PENDING_RESOLUTION_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    return spans


def _pending_evidence(
    *,
    text: str,
    summary: str,
    title: str,
    context: str,
    matched_text: str,
    body_window_start: int,
) -> tuple[str, int | None, int | None]:
    body_window = text[body_window_start:] if body_window_start else text
    index = body_window.find(matched_text)
    if index >= 0:
        start = body_window_start + index
        end = min(len(text), start + max(80, len(matched_text)))
        return f"body:{start}-{end}", start, end
    index = summary.find(matched_text)
    if index >= 0:
        return f"summary:{index}-{min(len(summary), index + max(80, len(matched_text)))}", None, None
    index = title.find(matched_text)
    if index >= 0:
        return f"title:{index}-{min(len(title), index + max(80, len(matched_text)))}", None, None
    index = context.find(matched_text)
    if index >= 0:
        return f"final_context:{index}-{index + len(matched_text)}", None, None
    return "finale:0-0", None, None


def _first_evidence(
    *,
    text: str,
    summary: str,
    title: str,
    keywords: list[str],
    body_window_start: int = 0,
) -> tuple[str, int | None, int | None]:
    body_window = text[body_window_start:] if body_window_start else text
    for keyword in keywords:
        index = body_window.find(keyword)
        if index >= 0:
            start = body_window_start + index
            end = min(len(text), start + max(80, len(keyword)))
            return f"body:{start}-{end}", start, end
    for keyword in keywords:
        index = summary.find(keyword)
        if index >= 0:
            end = min(len(summary), index + max(80, len(keyword)))
            return f"summary:{index}-{end}", None, None
    for keyword in keywords:
        index = title.find(keyword)
        if index >= 0:
            end = min(len(title), index + max(80, len(keyword)))
            return f"title:{index}-{end}", None, None
    return "finale:0-0", None, None
