from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.audience.comment_analysis import (
    CommentAnalysisStore,
    current_signal_condition,
)
from forwin.audience.feedback import (
    classify_signal_level,
    keyword_dominant_sentiment,
    keyword_feedback_summary,
)
from forwin.book_state.thread_sampling import SampledThread, sample_active_threads
from forwin.llm.compat import call_chat_compat
from forwin.models import (
    CommentSignalCandidate,
    PublisherRawComment,
    WorldSimulationTurn,
    new_id,
)
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.utils import parse_llm_json

logger = logging.getLogger(__name__)


def _read_optional_phase4_llm_timeout_seconds(
    env: dict[str, str] | None = None,
) -> float:
    raw = (env or os.environ).get("FORWIN_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 20.0
    return value if value > 0 else 20.0


_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS = _read_optional_phase4_llm_timeout_seconds()

_VALID_SIGNAL_TYPES = frozenset(
    {
        "confusion",
        "pacing",
        "character_heat",
        "risk",
        "relationship_interest",
        "prediction",
    }
)
_VALID_TARGET_TYPES = frozenset({"character", "arc", "plot", "setting", "general"})
_LEVEL_ORDER = {"noise": 0, "candidate": 1, "watchlist": 2, "confirmed": 3}

_KEYWORD_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("risk", "plot", ("崩", "烂", "弃", "逻辑", "bug", "矛盾", "失望")),
    ("pacing", "arc", ("水", "拖", "慢", "短", "快", "赶", "乱")),
    (
        "confusion",
        "general",
        ("为什么", "怎么", "是不是", "会不会", "看不懂", "不理解"),
    ),
    (
        "character_heat",
        "character",
        ("喜欢", "精彩", "好看", "帅", "魅力", "上头", "爽", "神", "牛", "期待"),
    ),
    (
        "relationship_interest",
        "character",
        ("cp", "互动", "感情", "在一起", "嗑", "关系线", "修罗场"),
    ),
    (
        "prediction",
        "plot",
        ("我猜", "预测", "盲猜", "应该是", "会不会", "是不是", "估计"),
    ),
]


@dataclass(slots=True)
class SignalDraft:
    signal_type: str
    target_type: str
    target_name: str
    severity: int
    confidence: float
    evidence_span: str


@dataclass(slots=True)
class WorldTurnDraft:
    pressure_level: str
    pressure_summary: str
    notable_shifts: list[str]


def _signal_key(signal_type: str, target_type: str, target_name: str) -> str:
    return f"{signal_type}:{target_type}:{target_name or 'general'}"


def _signal_target_label(target_name: str) -> str:
    return str(target_name or "").strip() or "整体"


def _keyword_fallback(body: str) -> list[SignalDraft]:
    signals: list[SignalDraft] = []
    for signal_type, target_type, keywords in _KEYWORD_RULES:
        hits = [keyword for keyword in keywords if keyword in body]
        if not hits:
            continue
        severity = 3 if signal_type == "risk" else 2 if len(hits) >= 2 else 1
        signals.append(
            SignalDraft(
                signal_type=signal_type,
                target_type=target_type,
                target_name="",
                severity=severity,
                confidence=0.4,
                evidence_span=body[:80],
            )
        )
    return signals


def _signal_rank(signal: dict[str, Any]) -> tuple[int, int, int, int, str]:
    level = str(signal.get("level") or "noise")
    signal_type = str(signal.get("signal_type") or "")
    boost = 2 if signal_type == "risk" and level in {"watchlist", "confirmed"} else 0
    signal_priority = {
        "risk": 5,
        "confusion": 4,
        "prediction": 3,
        "pacing": 2,
        "relationship_interest": 1,
        "character_heat": 0,
    }
    return (
        _LEVEL_ORDER.get(level, 0) + boost,
        signal_priority.get(signal_type, 0),
        int(signal.get("max_severity") or 0),
        int(signal.get("hit_count") or 0),
        _signal_target_label(str(signal.get("target_name") or "")),
    )


def _sorted_signal_values(signals: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(signals.values(), key=_signal_rank, reverse=True)


def _build_structured_feedback_summary(
    comment_count: int,
    signal_values: Sequence[dict[str, Any]],
) -> str:
    summary_parts = [f"最近 {comment_count} 条评论"]
    primary = signal_values[0]
    summary_parts.append(
        "主导信号："
        f"{_signal_target_label(str(primary.get('target_name') or ''))}:"
        f"{primary.get('signal_type')}:{primary.get('level')}"
    )
    if len(signal_values) > 1:
        summary_parts.append(
            "关注点："
            + "、".join(
                f"{_signal_target_label(str(item.get('target_name') or ''))}:"
                f"{item.get('signal_type')}:{item.get('level')}"
                for item in signal_values[:3]
            )
        )
    return "，".join(summary_parts) + "。"


def _load_highlight_comments(
    session: Session,
    fallback_rows: Sequence[PublisherRawComment],
    signal_rows: Sequence[CommentSignalCandidate],
    *,
    limit: int,
) -> list[PublisherRawComment]:
    if not signal_rows:
        return list(fallback_rows[:limit])

    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    for signal in signal_rows:
        comment_id = signal.source_comment_id
        if comment_id in seen_ids:
            continue
        seen_ids.add(comment_id)
        ordered_ids.append(comment_id)
        if len(ordered_ids) >= limit:
            break

    if not ordered_ids:
        return list(fallback_rows[:limit])

    comment_map = {
        row.id: row
        for row in session.execute(
            select(PublisherRawComment).where(PublisherRawComment.id.in_(ordered_ids))
        )
        .scalars()
        .all()
    }
    selected = [
        comment_map[comment_id]
        for comment_id in ordered_ids
        if comment_id in comment_map
    ]
    return selected or list(fallback_rows[:limit])


class CommentAnalyzer:
    """Extracts structured comment signals in Phase 4."""

    def __init__(
        self,
        *,
        llm_client=None,
        analyzer_version="comment-v2",
        max_attempts=3,
        retry_delay_seconds=60,
    ) -> None:
        self.llm_client = llm_client
        self.store = CommentAnalysisStore(
            analyzer_version=analyzer_version
            + (":llm" if llm_client is not None else ":keyword"),
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )

    def pending_comments(self, *, session, project_id, limit):
        return self.store.pending(session, project_id=project_id, limit=limit)

    def analyze_and_store(
        self,
        *,
        session: Session,
        project_id: str,
        comments: Sequence[PublisherRawComment],
        chapter_number: int = 0,
    ) -> list[CommentSignalCandidate]:
        records = self.store.begin(
            session,
            project_id=project_id,
            comments=comments,
            generation_chapter_number=chapter_number,
        )
        if not records:
            return []
        try:
            if self.llm_client is None:
                drafts_by_comment = {
                    comment.id: _keyword_fallback(comment.body_text or "")
                    for comment, _ in records
                }
            else:
                drafts_by_comment = self._analyze_comments_with_llm(
                    [comment for comment, _ in records]
                )
        except Exception as exc:  # noqa: BLE001 -- persist bounded retry state for optional analysis
            for _, record in records:
                self.store.failed(record, exc)
            session.flush()
            return []

        rows = []
        for comment, record in records:
            drafts = drafts_by_comment.get(comment.id, [])
            for draft in drafts:
                row = CommentSignalCandidate(
                    id=new_id(),
                    project_id=project_id,
                    source_comment_id=comment.id,
                    analysis_id=record.id,
                    signal_type=draft.signal_type,
                    target_type=draft.target_type,
                    target_name=draft.target_name,
                    severity=draft.severity,
                    confidence=draft.confidence,
                    evidence_span=draft.evidence_span,
                    signal_level="noise",
                    chapter_number=(comment.source_chapter_number or 0)
                    if comment.source_status in {"chapter_known", "confirmed"}
                    else 0,
                )
                session.add(row)
                rows.append(row)
            self.store.complete(record, len(drafts))
            comment.active_analysis_id = record.id
        session.flush()
        return rows

    def _analyze_comments_with_llm(
        self,
        comments: Sequence[PublisherRawComment],
    ) -> dict[str, list[SignalDraft]] | None:
        if self.llm_client is None:
            return None

        comment_payload = [
            {"comment_index": index, "body": str(comment.body_text or "").strip()}
            for index, comment in enumerate(comments)
        ]
        if not comment_payload:
            return {}

        prompt = [
            {"role": "system", "content": "你是网文评论分析器，只输出 JSON。"},
            {
                "role": "user",
                "content": (
                    "请分析以下读者评论，提取信号。一条评论可产出多个信号。\n"
                    "signal_type 只能是：confusion / pacing / character_heat / risk / relationship_interest / prediction\n"
                    "返回格式："
                    '{"signals":[{"comment_index":0,'
                    '"signal_type":"...","target_type":"...",'
                    '"target_name":"...","severity":1,"confidence":0.8,'
                    '"evidence_span":"原文摘录"}]}\n\n'
                    f"评论列表：{json.dumps(comment_payload, ensure_ascii=False)}"
                ),
            },
        ]

        before_events = len(getattr(self.llm_client, "llm_attempt_events", []) or [])
        before_trace = copy.deepcopy(getattr(self.llm_client, "last_call_trace", None))
        try:
            try:
                raw = call_chat_compat(
                    self.llm_client,
                    prompt,
                    temperature=0.3,
                    max_tokens=min(1200, 200 + len(comment_payload) * 120),
                    response_format={"type": "json_object"},
                    task_family="phase4",
                    stage_key="comment_analysis",
                    output_schema={"type": "object"},
                    timeout_seconds=_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS,
                    retry_on_timeout=False,
                )
            except TypeError as exc:
                if "response_format" not in str(exc):
                    raise
                raw = call_chat_compat(
                    self.llm_client,
                    prompt,
                    temperature=0.3,
                    max_tokens=min(1200, 200 + len(comment_payload) * 120),
                    task_family="phase4",
                    stage_key="comment_analysis",
                    timeout_seconds=_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS,
                    retry_on_timeout=False,
                )
        except Exception:
            logger.warning("CommentAnalyzer LLM call failed.", exc_info=True)
            raise

        events = list(getattr(self.llm_client, "llm_attempt_events", []) or [])[
            before_events:
        ]
        trace = getattr(self.llm_client, "last_call_trace", None)
        if trace != before_trace and isinstance(trace, dict):
            events.append(trace)
        if any(
            row.get("finish_reason")
            in {"length", "max_tokens", "content_filter", "error"}
            or row.get("stop_reason") == "max_tokens"
            for row in events
        ):
            raise ValueError("comment analysis response was truncated or filtered")

        try:
            payload = parse_llm_json(raw, error_prefix="CommentAnalyzer")
        except Exception as exc:  # noqa: BLE001
            mark_latest_attempt_parse_failure(
                self.llm_client,
                parser_name="CommentAnalyzer",
                stage_key="comment_analysis",
                schema_name="comment_analysis",
                raw_output=raw,
                error=exc,
            )
            logger.warning("CommentAnalyzer JSON parse failed.", exc_info=True)
            raise

        if not isinstance(payload, dict) or not isinstance(
            payload.get("signals"), list
        ):
            raise TypeError("comment analysis requires an explicit signals array")
        index_to_comment_id = {
            index: comment.id for index, comment in enumerate(comments)
        }

        result: dict[str, list[SignalDraft]] = {}
        for item in payload.get("signals") or []:
            if not isinstance(item, dict):
                raise TypeError("comment signal must be an object")
            idx = item.get("comment_index", item.get("index"))
            if type(idx) is not int or idx not in index_to_comment_id:
                raise ValueError("comment signal has an invalid source index")
            signal_type = str(item.get("signal_type") or "").strip().lower()
            if signal_type not in _VALID_SIGNAL_TYPES:
                raise ValueError("comment signal has an invalid type")
            target_type = str(item.get("target_type") or "general").strip().lower()
            if target_type not in _VALID_TARGET_TYPES:
                target_type = "general"
            try:
                severity = max(1, min(4, int(item.get("severity") or 1)))
            except (TypeError, ValueError):
                severity = 1
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.5)))
            except (TypeError, ValueError):
                confidence = 0.5

            comment_id = index_to_comment_id[idx]
            draft = SignalDraft(
                signal_type=signal_type,
                target_type=target_type,
                target_name=str(item.get("target_name") or "").strip()[:60],
                severity=severity,
                confidence=confidence,
                evidence_span=str(item.get("evidence_span") or "").strip()[:120],
            )
            result.setdefault(comment_id, []).append(draft)

        return result


def load_recent_signals(
    session: Session,
    project_id: str,
    *,
    chapter_range: int = 5,
    current_chapter: int = 0,
    before_chapter: int | None = None,
) -> list[CommentSignalCandidate]:
    stmt = select(CommentSignalCandidate).where(
        CommentSignalCandidate.project_id == project_id,
        current_signal_condition(),
    )

    end_chapter = 0
    if before_chapter is not None and before_chapter > 0:
        end_chapter = before_chapter - 1
        stmt = stmt.where(CommentSignalCandidate.chapter_number < before_chapter)
    elif current_chapter > 0:
        end_chapter = current_chapter
        stmt = stmt.where(CommentSignalCandidate.chapter_number <= current_chapter)

    if end_chapter > 0 and chapter_range > 0:
        min_chapter = max(0, end_chapter - chapter_range + 1)
        stmt = stmt.where(CommentSignalCandidate.chapter_number >= min_chapter)

    return list(
        session.execute(
            stmt.order_by(CommentSignalCandidate.created_at.desc()).limit(200)
        )
        .scalars()
        .all()
    )


def aggregate_and_level_signals(
    session: Session,
    signals: Sequence[CommentSignalCandidate],
) -> dict[str, dict[str, Any]]:
    if not signals:
        return {}

    comment_ids = {signal.source_comment_id for signal in signals}
    comment_map = {
        row.id: row
        for row in session.execute(
            select(PublisherRawComment).where(PublisherRawComment.id.in_(comment_ids))
        )
        .scalars()
        .all()
    }

    buckets: dict[str, dict[str, Any]] = {}
    for signal in signals:
        key = _signal_key(signal.signal_type, signal.target_type, signal.target_name)
        bucket = buckets.setdefault(
            key,
            {
                "signal_key": key,
                "signal_type": signal.signal_type,
                "target_type": signal.target_type,
                "target_name": signal.target_name,
                "user_ids": set(),
                "hit_count": 0,
                "max_severity": 0,
                "chapters": set(),
            },
        )
        source_comment = comment_map.get(signal.source_comment_id)
        author_key = ""
        if source_comment is not None:
            author_key = (
                str(source_comment.author_id or "").strip()
                or str(source_comment.author_name or "").strip()
            )
        bucket["user_ids"].add(author_key or signal.source_comment_id)
        bucket["hit_count"] += 1
        bucket["max_severity"] = max(bucket["max_severity"], signal.severity)
        if signal.chapter_number > 0:
            bucket["chapters"].add(signal.chapter_number)

    result: dict[str, dict[str, Any]] = {}
    for key, bucket in buckets.items():
        unique_users = len(bucket["user_ids"])
        spans_chapters = len(bucket["chapters"])
        level = classify_signal_level(
            unique_users=unique_users,
            spans_chapters=spans_chapters,
            severity=bucket["max_severity"],
            signal_type=bucket["signal_type"],
        )
        result[key] = {
            "signal_key": key,
            "signal_type": bucket["signal_type"],
            "target_type": bucket["target_type"],
            "target_name": bucket["target_name"],
            "unique_users": unique_users,
            "hit_count": bucket["hit_count"],
            "max_severity": bucket["max_severity"],
            "spans_chapters": spans_chapters,
            "level": level,
        }
    return result


def build_reader_feedback_snapshot(
    session: Session,
    project_title: str,
    *,
    project_id: str = "",
    chapter_number: int = 0,
    before_chapter: int | None = None,
    limit: int = 6,
    llm_client=None,
    analyze_missing: bool = False,
    allowed_chapter_titles: Sequence[str] | None = None,
) -> dict[str, Any]:
    empty_snapshot = {
        "comment_count": 0,
        "dominant_sentiment": "neutral",
        "feedback_summary": "",
        "highlighted_topics": [],
        "confirmed_signals": [],
        "recent_comments": [],
        "signals": {},
        "analysis_status": {},
    }

    title = str(project_title or "").strip()
    if not title:
        return empty_snapshot

    normalized_allowed_titles = {
        str(item).strip()
        for item in (allowed_chapter_titles or [])
        if str(item).strip()
    }
    if not project_id:
        return empty_snapshot
    stmt = select(PublisherRawComment).where(
        PublisherRawComment.project_id == project_id
    )
    analyzer = CommentAnalyzer(llm_client=llm_client)
    if analyze_missing:
        analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=analyzer.pending_comments(
                session=session, project_id=project_id, limit=limit
            ),
            chapter_number=chapter_number or max((before_chapter or 1) - 1, 0),
        )

    if normalized_allowed_titles:
        stmt = stmt.where(
            or_(
                PublisherRawComment.chapter_title.in_(
                    sorted(normalized_allowed_titles)
                ),
                PublisherRawComment.chapter_title == "",
            )
        )

    rows = (
        session.execute(
            stmt.order_by(
                PublisherRawComment.synced_at.desc(),
                PublisherRawComment.updated_at.desc(),
            ).limit(limit)
        )
        .scalars()
        .all()
    )
    if not rows:
        return empty_snapshot

    signal_rows: list[CommentSignalCandidate] = []
    aggregated_signals: dict[str, dict[str, Any]] = {}
    if project_id:
        signal_rows = load_recent_signals(
            session,
            project_id,
            chapter_range=5,
            current_chapter=chapter_number,
            before_chapter=before_chapter,
        )
        if signal_rows:
            aggregated_signals = aggregate_and_level_signals(session, signal_rows)

    highlight_rows = _load_highlight_comments(
        session, rows, signal_rows, limit=min(limit, 4)
    )
    keyword_dominant = keyword_dominant_sentiment(highlight_rows or rows)

    highlighted_topics: list[str] = []
    confirmed_signals: list[dict[str, Any]] = []
    if aggregated_signals:
        sorted_signals = _sorted_signal_values(aggregated_signals)
        dominant_signal = sorted_signals[0]
        dominant_sentiment = (
            f"{dominant_signal['signal_type']}:{dominant_signal['level']}"
        )
        highlighted_topics = [
            f"{_signal_target_label(str(item['target_name']))}:{item['signal_type']}:{item['level']}"
            for item in sorted_signals[:3]
        ]
        confirmed_signals = [
            {
                "signal_key": item["signal_key"],
                "signal_type": item["signal_type"],
                "target_name": item["target_name"],
                "level": item["level"],
                "hit_count": item["hit_count"],
                "max_severity": item["max_severity"],
            }
            for item in sorted_signals
            if item["level"] in {"confirmed", "watchlist"}
        ][:6]
        feedback_summary = _build_structured_feedback_summary(len(rows), sorted_signals)
    else:
        dominant_sentiment = keyword_dominant
        feedback_summary = keyword_feedback_summary(len(rows), dominant_sentiment)

    return {
        "comment_count": len(rows),
        "dominant_sentiment": dominant_sentiment,
        "feedback_summary": feedback_summary,
        "highlighted_topics": highlighted_topics,
        "confirmed_signals": confirmed_signals,
        "recent_comments": highlight_rows,
        "signals": aggregated_signals,
        "analysis_status": analyzer.store.status(session, project_id=project_id),
    }


class WorldSimulator:
    def __init__(self, *, llm_client=None, active_thread_limit: int = 20) -> None:
        self.llm_client = llm_client
        self.active_thread_limit = max(1, int(active_thread_limit))

    def simulate(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> WorldTurnDraft:
        sampled = sample_active_threads(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            limit=self.active_thread_limit,
            stale_window=2,
            recent_window=2,
        )
        active_threads = sampled.threads
        latest_beats = sampled.latest_beats
        stale_threads: list[str] = []
        for thread in active_threads:
            last_beat = latest_beats.get(thread.id)
            reference_chapter = (
                last_beat.chapter_number
                if last_beat is not None
                else thread.opened_at_chapter
            )
            if chapter_number - reference_chapter >= 2:
                stale_threads.append(thread.name)
        llm_turn = self._simulate_with_llm(
            chapter_number=chapter_number,
            active_threads=active_threads,
            stale_threads=stale_threads,
        )
        if llm_turn is not None:
            return llm_turn

        pressure_level = "steady"
        shifts: list[str] = []
        if stale_threads:
            pressure_level = "rising" if len(stale_threads) == 1 else "critical"
            shifts.append(f"悬置线程：{'、'.join(stale_threads)}")
        if chapter_number >= 3:
            shifts.append("世界正在对主角行动产生连锁反应")
            if pressure_level == "steady":
                pressure_level = "rising"
        if not shifts:
            shifts.append("主要矛盾仍处于可控推进状态")

        pressure_summary = (
            f"第{chapter_number}章后，世界压力为 {pressure_level}。"
            f"重点变化：{'；'.join(shifts)}。"
        )
        return WorldTurnDraft(
            pressure_level=pressure_level,
            pressure_summary=pressure_summary,
            notable_shifts=shifts,
        )

    def _simulate_with_llm(
        self,
        *,
        chapter_number: int,
        active_threads: list[SampledThread],
        stale_threads: list[str],
    ) -> WorldTurnDraft | None:
        if self.llm_client is None:
            return None
        prompt = [
            {
                "role": "system",
                "content": "你是网文世界模拟器，只输出 JSON，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请判断当前章节之后的世界压力，并输出下一章前的重要连锁反应。\n"
                    f"当前章节：第 {chapter_number} 章\n"
                    f"活跃线程数：{len(active_threads)}\n"
                    f"悬置线程：{json.dumps(stale_threads, ensure_ascii=False)}\n\n"
                    '返回格式：{"pressure_level":"steady|rising|critical","pressure_summary":"一句中文总结","notable_shifts":["变化1","变化2"]}'
                ),
            },
        ]
        try:
            raw = call_chat_compat(
                self.llm_client,
                prompt,
                temperature=0.35,
                max_tokens=700,
                response_format={"type": "json_object"},
                task_family="phase4",
                stage_key="world_pressure",
                output_schema={"type": "object"},
                timeout_seconds=_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS,
                retry_on_timeout=False,
            )
        except TypeError as exc:
            if "response_format" not in str(exc):
                logger.warning("Phase4 world LLM call failed.", exc_info=True)
                return None
            try:
                raw = call_chat_compat(
                    self.llm_client,
                    prompt,
                    temperature=0.35,
                    max_tokens=700,
                    task_family="phase4",
                    stage_key="world_pressure",
                    timeout_seconds=_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS,
                    retry_on_timeout=False,
                )
            except Exception:
                logger.warning("Phase4 world LLM fallback call failed.", exc_info=True)
                return None
        except Exception:
            logger.warning("Phase4 world LLM call failed.", exc_info=True)
            return None
        try:
            payload = parse_llm_json(raw, error_prefix="World simulator parser")
        except Exception as exc:  # noqa: BLE001
            mark_latest_attempt_parse_failure(
                self.llm_client,
                parser_name="World simulator parser",
                stage_key="world_pressure",
                schema_name="world_pressure",
                raw_output=raw,
                error=exc,
            )
            logger.warning("Phase4 world parse failed.", exc_info=True)
            return None
        level = str(payload.get("pressure_level") or "steady").strip().lower()
        if level not in {"steady", "rising", "critical"}:
            level = "steady"
        notable = [
            str(item).strip()
            for item in (payload.get("notable_shifts") or [])
            if str(item).strip()
        ]
        return WorldTurnDraft(
            pressure_level=level,
            pressure_summary=str(payload.get("pressure_summary") or "").strip()
            or f"第{chapter_number}章后，世界压力为 {level}。",
            notable_shifts=notable or ["世界仍在对主角行动做出反应"],
        )


def save_world_turn(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    turn: WorldTurnDraft,
) -> None:
    session.add(
        WorldSimulationTurn(
            id=new_id(),
            project_id=project_id,
            chapter_number=chapter_number,
            pressure_level=turn.pressure_level,
            pressure_summary=turn.pressure_summary,
            notable_shifts_json=json.dumps(turn.notable_shifts, ensure_ascii=False),
        )
    )
    session.flush()
