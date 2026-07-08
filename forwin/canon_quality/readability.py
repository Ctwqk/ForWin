from __future__ import annotations

import re

from forwin.protocol.writer import WriterOutput

from .signals import CanonQualitySignal, make_signal_id


COMMON_APPELLATIONS = (
    "馆员",
    "审计员",
    "调度员",
    "接线员",
    "工作人员",
    "管理员",
    "巡检员",
    "操作员",
    "负责人",
    "工程师",
    "主管",
    "高管",
    "队长",
    "守卫",
    "摊主",
)
INTERNAL_KEY_PATTERNS = (
    re.compile(r"trait-[a-z][a-z0-9-]*"),
    re.compile(r"<<FORWIN_[A-Z0-9_]+>>"),
)
EXPERIENCE_PLAN_FIELD_NAMES = (
    "active_subworld_ids",
    "chapter_entry_targets",
    "entity_admission_rule",
    "delight_beats",
    "planned_reward_tags",
    "delivered_reward_tags",
    "entry_target_reason",
    "subworld_id",
    "band_id",
)
PROTAGONIST_GENERIC_REFS = ("主角", "主人公", "工作人员", "馆员", "审计员")


def analyze_writer_output_readability(
    *,
    project_id: str,
    chapter_number: int,
    writer_output: WriterOutput,
    draft_id: str = "",
    protagonist_names: set[str] | None = None,
    appellations: set[str] | None = None,
) -> list[CanonQualitySignal]:
    body = str(writer_output.body or "")
    summary = str(writer_output.end_of_chapter_summary or "")
    signals: list[CanonQualitySignal] = []
    signals.extend(
        _appellation_referent_conflict_signals(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            body=body,
            appellations=set(appellations or set()) | set(COMMON_APPELLATIONS),
        )
    )
    internal_key_signal = _internal_key_leakage_signal(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
    )
    if internal_key_signal is not None:
        signals.append(internal_key_signal)
    protagonist_signals = _protagonist_name_signals(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body=body,
        protagonist_names=protagonist_names or set(),
    )
    signals.extend(protagonist_signals)
    title_signal = _chapter_title_mismatch_signal(
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        title=str(writer_output.title or ""),
    )
    if title_signal is not None:
        signals.append(title_signal)
    if not summary.strip():
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "chapter_summary_empty", "summary"),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="chapter_summary_empty",
                severity="error",
                target_scope="chapter",
                subject_key="summary",
                description="章节 end_of_chapter_summary 为空，不能接受为 canon。",
                evidence_refs=["summary:empty"],
                payload=_payload(draft_id=draft_id),
            )
        )
    return _dedupe_signals(signals)


def _appellation_referent_conflict_signals(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    body: str,
    appellations: set[str],
) -> list[CanonQualitySignal]:
    signals: list[CanonQualitySignal] = []
    for appellation in sorted(item for item in appellations if item):
        spans = [match.span() for match in re.finditer(re.escape(appellation), body)]
        if len(spans) < 2:
            continue
        has_male = False
        has_female = False
        evidence_refs: list[str] = []
        for start, end in spans:
            window = body[max(0, start - 18) : min(len(body), end + 18)]
            if "他" in window:
                has_male = True
                evidence_refs.append(f"body:{start}-{end}")
            if "她" in window:
                has_female = True
                evidence_refs.append(f"body:{start}-{end}")
        if not (has_male and has_female):
            continue
        signals.append(
            CanonQualitySignal(
                signal_id=make_signal_id(
                    project_id,
                    chapter_number,
                    "appellation_referent_conflict",
                    f"appellation:{appellation}",
                ),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="appellation_referent_conflict",
                severity="error",
                target_scope="body",
                subject_key=f"appellation:{appellation}",
                description=f"同一称谓「{appellation}」在同章中邻接冲突性别代词，读者无法稳定识别指代对象。",
                evidence_refs=sorted(set(evidence_refs))[:4],
                span_start=spans[0][0],
                span_end=spans[-1][1],
                payload={**_payload(draft_id=draft_id), "appellation": appellation},
            )
        )
    return signals


def _internal_key_leakage_signal(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    body: str,
) -> CanonQualitySignal | None:
    matches: list[tuple[int, int, str]] = []
    for pattern in INTERNAL_KEY_PATTERNS:
        match = pattern.search(body)
        if match is not None:
            matches.append((match.start(), match.end(), match.group(0)))
    for field_name in EXPERIENCE_PLAN_FIELD_NAMES:
        start = body.find(field_name)
        if start >= 0:
            matches.append((start, start + len(field_name), field_name))
    if not matches:
        return None
    start, end, token = sorted(matches, key=lambda item: item[0])[0]
    return CanonQualitySignal(
        signal_id=make_signal_id(project_id, chapter_number, "internal_key_leakage_v2", f"internal:{token}"),
        project_id=project_id,
        chapter_number=chapter_number,
        signal_type="internal_key_leakage_v2",
        severity="error",
        target_scope="body",
        subject_key=f"internal:{token}",
        description=f"章节正文泄漏内部键或技能标识「{token}」，不能进入读者可见 canon。",
        evidence_refs=[f"body:{start}-{end}"],
        span_start=start,
        span_end=end,
        payload={**_payload(draft_id=draft_id), "internal_key": token},
    )


def _protagonist_name_signals(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    body: str,
    protagonist_names: set[str],
) -> list[CanonQualitySignal]:
    names = sorted(
        {str(name or "").strip() for name in protagonist_names if str(name or "").strip()},
        key=len,
        reverse=True,
    )
    if not names:
        return []
    name_count = sum(_term_count(body, name) for name in names)
    if name_count == 0:
        return [
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "protagonist_name_missing", "protagonist"),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="protagonist_name_missing",
                severity="error",
                target_scope="body",
                subject_key="protagonist",
                description=f"章节正文没有出现主角真名「{'/'.join(names[:3])}」，可能被泛称或错误称谓替代。",
                evidence_refs=["body:protagonist-name-count=0"],
                payload={**_payload(draft_id=draft_id), "protagonist_names": names},
            )
        ]
    generic_count = sum(_term_count(body, term) for term in PROTAGONIST_GENERIC_REFS)
    denominator = name_count + generic_count
    if generic_count >= 3 and denominator > 0 and (name_count / denominator) < 0.3:
        first_name = names[0]
        start = body.find(first_name)
        return [
            CanonQualitySignal(
                signal_id=make_signal_id(project_id, chapter_number, "protagonist_name_diluted", "protagonist"),
                project_id=project_id,
                chapter_number=chapter_number,
                signal_type="protagonist_name_diluted",
                severity="warning",
                target_scope="body",
                subject_key="protagonist",
                description="主角真名相对泛称谓出现比例过低，读者可能难以追踪主视角。",
                evidence_refs=[f"body:{max(0, start)}-{max(0, start) + len(first_name)}"],
                span_start=max(0, start),
                span_end=max(0, start) + len(first_name),
                payload={
                    **_payload(draft_id=draft_id),
                    "protagonist_names": names,
                    "name_count": name_count,
                    "generic_count": generic_count,
                },
            )
        ]
    return []


def _chapter_title_mismatch_signal(
    *,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    title: str,
) -> CanonQualitySignal | None:
    match = re.search(r"第\s*(\d+)\s*章", title)
    if match is None:
        return None
    observed = int(match.group(1))
    expected = int(chapter_number or 0)
    if observed == expected:
        return None
    return CanonQualitySignal(
        signal_id=make_signal_id(project_id, chapter_number, "chapter_title_mismatch", "title"),
        project_id=project_id,
        chapter_number=chapter_number,
        signal_type="chapter_title_mismatch",
        severity="error",
        target_scope="chapter",
        subject_key="title",
        description=f"章节标题编号为第{observed}章，但当前 chapter_number 是 {expected}。",
        evidence_refs=[f"title:{match.start()}-{match.end()}"],
        span_start=match.start(),
        span_end=match.end(),
        payload={**_payload(draft_id=draft_id), "observed_chapter_number": observed, "expected_chapter_number": expected},
    )


def _term_count(text: str, term: str) -> int:
    if not term:
        return 0
    return sum(1 for _ in re.finditer(re.escape(term), text))


def _payload(*, draft_id: str) -> dict[str, object]:
    return {
        "draft_id": draft_id,
        "source_mode": "deterministic",
        "blocking_origin": "readability",
        "confidence": 1.0,
    }


def _dedupe_signals(signals: list[CanonQualitySignal]) -> list[CanonQualitySignal]:
    seen: set[str] = set()
    deduped: list[CanonQualitySignal] = []
    for signal in signals:
        if signal.signal_id in seen:
            continue
        seen.add(signal.signal_id)
        deduped.append(signal)
    return deduped


__all__ = ["analyze_writer_output_readability"]
