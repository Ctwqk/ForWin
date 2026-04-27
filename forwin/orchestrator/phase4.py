from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from forwin.models import (
    CommentSignalCandidate,
    Entity,
    NPCIntentSnapshot,
    PlotThread,
    Project,
    PublisherRawComment,
    WorldSimulationTurn,
    new_id,
)
from forwin.orchestrator.thread_sampling import sample_active_threads
from forwin.state.query_helpers import load_latest_entity_states
from forwin.utils import parse_llm_json
from forwin.llm.compat import call_chat_compat
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure

logger = logging.getLogger(__name__)

_POSITIVE_COMMENT_KEYWORDS = ("喜欢", "精彩", "好看", "期待", "爽", "牛", "神")
_NEGATIVE_COMMENT_KEYWORDS = ("水", "拖", "崩", "失望", "弃", "烂", "短", "乱")
_QUESTION_COMMENT_KEYWORDS = ("为什么", "怎么", "是不是", "会不会", "求", "能不能")

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
    ("confusion", "general", ("为什么", "怎么", "是不是", "会不会", "看不懂", "不理解")),
    ("character_heat", "character", ("喜欢", "精彩", "好看", "帅", "魅力", "上头", "爽", "神", "牛", "期待")),
    ("relationship_interest", "character", ("cp", "互动", "感情", "在一起", "嗑", "关系线", "修罗场")),
    ("prediction", "plot", ("我猜", "预测", "盲猜", "应该是", "会不会", "是不是", "估计")),
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
class NPCIntentDraft:
    entity_id: str
    entity_name: str
    intent_kind: str
    objective: str
    tactic: str
    urgency: int
    notes: str


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


def classify_signal_level(
    *,
    unique_users: int,
    spans_chapters: int,
    severity: int,
    signal_type: str,
) -> str:
    if signal_type == "risk" and severity >= 3:
        return "watchlist" if unique_users < 2 else "confirmed"
    if unique_users < 2:
        return "noise"
    if spans_chapters < 2 and signal_type in ("character_heat", "relationship_interest", "prediction"):
        return "noise"
    if unique_users >= 3 and spans_chapters >= 2:
        return "confirmed"
    return "candidate"


def _keyword_dominant_sentiment(comments: Sequence[PublisherRawComment]) -> str:
    positive = 0
    negative = 0
    curious = 0
    for comment in comments:
        text = str(comment.body_text or "")
        if any(keyword in text for keyword in _POSITIVE_COMMENT_KEYWORDS):
            positive += 1
        if any(keyword in text for keyword in _NEGATIVE_COMMENT_KEYWORDS):
            negative += 1
        if any(keyword in text for keyword in _QUESTION_COMMENT_KEYWORDS):
            curious += 1
    if negative > max(positive, curious):
        return "negative"
    if positive > max(negative, curious):
        return "positive"
    if curious:
        return "curious"
    return "neutral"


def _keyword_feedback_summary(comment_count: int, dominant_sentiment: str) -> str:
    summary_parts = [f"最近 {comment_count} 条评论"]
    if dominant_sentiment == "negative":
        summary_parts.append("整体情绪偏担忧")
    elif dominant_sentiment == "positive":
        summary_parts.append("整体情绪偏积极")
    elif dominant_sentiment == "curious":
        summary_parts.append("读者对悬念追问较多")
    else:
        summary_parts.append("暂无明确结构化信号")
    return "，".join(summary_parts) + "。"


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
        ).scalars().all()
    }
    selected = [comment_map[comment_id] for comment_id in ordered_ids if comment_id in comment_map]
    return selected or list(fallback_rows[:limit])


class CommentAnalyzer:
    """Extracts structured comment signals in Phase 4."""

    def __init__(self, *, llm_client=None) -> None:
        self.llm_client = llm_client

    def analyze_and_store(
        self,
        *,
        session: Session,
        project_id: str,
        comments: Sequence[PublisherRawComment],
        chapter_number: int = 0,
    ) -> list[CommentSignalCandidate]:
        if not comments:
            return []

        comment_ids = [comment.id for comment in comments]
        existing = set(
            session.execute(
                select(CommentSignalCandidate.source_comment_id).where(
                    CommentSignalCandidate.source_comment_id.in_(comment_ids)
                )
            ).scalars().all()
        )
        to_analyze = [comment for comment in comments if comment.id not in existing]
        if not to_analyze:
            return []

        drafts_by_comment = self._analyze_comments_with_llm(to_analyze)
        if drafts_by_comment is None:
            drafts_by_comment = {}
            for comment in to_analyze:
                body = str(comment.body_text or "").strip()
                if not body:
                    continue
                fallback_signals = _keyword_fallback(body)
                if fallback_signals:
                    drafts_by_comment[comment.id] = fallback_signals

        rows: list[CommentSignalCandidate] = []
        for comment in to_analyze:
            for draft in drafts_by_comment.get(comment.id, []):
                row = CommentSignalCandidate(
                    id=new_id(),
                    project_id=project_id,
                    source_comment_id=comment.id,
                    signal_type=draft.signal_type,
                    target_type=draft.target_type,
                    target_name=draft.target_name,
                    severity=draft.severity,
                    confidence=draft.confidence,
                    evidence_span=draft.evidence_span,
                    signal_level="noise",
                    chapter_number=chapter_number,
                )
                session.add(row)
                rows.append(row)
        if rows:
            session.flush()
        return rows

    def _analyze_comments_with_llm(
        self,
        comments: Sequence[PublisherRawComment],
    ) -> dict[str, list[SignalDraft]] | None:
        if self.llm_client is None:
            return None

        comment_payload = [
            {"comment_index": index, "body": str(comment.body_text or "").strip()[:300]}
            for index, comment in enumerate(comments)
            if str(comment.body_text or "").strip()
        ]
        if not comment_payload:
            return None

        prompt = [
            {"role": "system", "content": "你是网文评论分析器，只输出 JSON。"},
            {
                "role": "user",
                "content": (
                    "请分析以下读者评论，提取信号。一条评论可产出多个信号。\n"
                    "signal_type 只能是：confusion / pacing / character_heat / risk / relationship_interest / prediction\n"
                    "返回格式："
                    "{\"signals\":[{\"comment_index\":0,"
                    "\"signal_type\":\"...\",\"target_type\":\"...\","
                    "\"target_name\":\"...\",\"severity\":1,\"confidence\":0.8,"
                    "\"evidence_span\":\"原文摘录\"}]}\n\n"
                    f"评论列表：{json.dumps(comment_payload, ensure_ascii=False)}"
                ),
            },
        ]

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
                )
        except Exception:
            logger.warning("CommentAnalyzer LLM call failed.", exc_info=True)
            return None

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
            return None

        index_to_comment_id: dict[int, str] = {}
        valid_comments = [comment for comment in comments if str(comment.body_text or "").strip()]
        for index, comment in enumerate(valid_comments):
            index_to_comment_id[index] = comment.id

        result: dict[str, list[SignalDraft]] = {}
        for item in payload.get("signals") or []:
            if not isinstance(item, dict):
                continue
            idx = item.get("comment_index", item.get("index"))
            if not isinstance(idx, int) or idx not in index_to_comment_id:
                continue
            signal_type = str(item.get("signal_type") or "").strip().lower()
            if signal_type not in _VALID_SIGNAL_TYPES:
                continue
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

        return result if result else None


def load_recent_signals(
    session: Session,
    project_id: str,
    *,
    chapter_range: int = 5,
    current_chapter: int = 0,
    before_chapter: int | None = None,
) -> list[CommentSignalCandidate]:
    stmt = select(CommentSignalCandidate).where(CommentSignalCandidate.project_id == project_id)

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
        ).scalars().all()
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
        ).scalars().all()
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
    }

    title = str(project_title or "").strip()
    if not title:
        return empty_snapshot

    normalized_allowed_titles = {
        str(item).strip()
        for item in (allowed_chapter_titles or [])
        if str(item).strip()
    }
    has_project_scoped_comments = False
    if project_id:
        has_project_scoped_comments = bool(
            session.execute(
                select(func.count(PublisherRawComment.id)).where(
                    PublisherRawComment.project_id == project_id
                )
            ).scalar_one()
        )

    stmt = select(PublisherRawComment)
    if project_id and has_project_scoped_comments:
        stmt = stmt.where(PublisherRawComment.project_id == project_id)
    elif project_id:
        stmt = stmt.where(
            or_(
                PublisherRawComment.project_id == project_id,
                PublisherRawComment.project_id == "",
            ),
            PublisherRawComment.work_name == title,
        )
    else:
        stmt = stmt.where(PublisherRawComment.work_name == title)

    if normalized_allowed_titles:
        stmt = stmt.where(
            or_(
                PublisherRawComment.chapter_title.in_(sorted(normalized_allowed_titles)),
                PublisherRawComment.chapter_title == "",
            )
        )

    rows = session.execute(
        stmt.order_by(PublisherRawComment.synced_at.desc(), PublisherRawComment.updated_at.desc()).limit(limit)
    ).scalars().all()
    if not rows:
        return empty_snapshot

    if analyze_missing and project_id:
        analyzer = CommentAnalyzer(llm_client=llm_client)
        analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=rows,
            chapter_number=chapter_number or max((before_chapter or 1) - 1, 0),
        )

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

    highlight_rows = _load_highlight_comments(session, rows, signal_rows, limit=min(limit, 4))
    keyword_dominant = _keyword_dominant_sentiment(highlight_rows or rows)

    highlighted_topics: list[str] = []
    confirmed_signals: list[dict[str, Any]] = []
    if aggregated_signals:
        sorted_signals = _sorted_signal_values(aggregated_signals)
        dominant_signal = sorted_signals[0]
        dominant_sentiment = f"{dominant_signal['signal_type']}:{dominant_signal['level']}"
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
        feedback_summary = _keyword_feedback_summary(len(rows), dominant_sentiment)

    return {
        "comment_count": len(rows),
        "dominant_sentiment": dominant_sentiment,
        "feedback_summary": feedback_summary,
        "highlighted_topics": highlighted_topics,
        "confirmed_signals": confirmed_signals,
        "recent_comments": highlight_rows,
        "signals": aggregated_signals,
    }


class NPCIntentGenerator:
    def __init__(self, *, llm_client=None, active_thread_limit: int = 20) -> None:
        self.llm_client = llm_client
        self.active_thread_limit = max(1, int(active_thread_limit))

    def generate(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        limit: int = 5,
    ) -> list[NPCIntentDraft]:
        project = session.get(Project, project_id)
        entities = session.execute(
            select(Entity)
            .where(
                Entity.project_id == project_id,
                Entity.kind == "character",
                Entity.is_active == True,  # noqa: E712
            )
            .order_by(Entity.importance.desc(), Entity.created_at_chapter.asc())
            .limit(limit)
        ).scalars().all()

        active_threads = sample_active_threads(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            limit=min(2, self.active_thread_limit),
            stale_window=2,
            recent_window=2,
        ).threads
        thread_focus = "、".join(thread.name for thread in active_threads) or "当前主线"
        feedback = _load_reader_feedback(
            session,
            project.title if project else "",
            project_id=project_id,
            chapter_number=chapter_number,
            limit=6,
            llm_client=self.llm_client,
        )

        llm_intents = self._generate_with_llm(
            entities=entities,
            active_threads=active_threads,
            chapter_number=chapter_number,
            thread_focus=thread_focus,
            feedback_summary=str(feedback.get("summary") or ""),
        )
        if llm_intents is not None:
            return llm_intents

        intents: list[NPCIntentDraft] = []
        latest_states = load_latest_entity_states(session, [entity.id for entity in entities])
        dominant_sentiment = str(feedback.get("dominant_sentiment") or "neutral")
        for index, entity in enumerate(entities):
            latest_state = latest_states.get(entity.id)
            state = {}
            if latest_state is not None:
                try:
                    state = json.loads(latest_state.state_json or "{}") or {}
                except (json.JSONDecodeError, TypeError):
                    state = {}
            status = str(state.get("status", "normal") or "normal")
            location = str(state.get("location", "") or "")

            intent_kind = "pressure" if index == 0 else "pursue"
            urgency = max(1, min(5, 5 - index))
            objective = (
                f"围绕{thread_focus}采取下一步行动，争取在下一章改变局面。"
                if status in {"normal", "active", ""}
                else f"在{status}状态下寻找翻盘机会，仍然围绕{thread_focus}行动。"
            )
            tactic_parts = []
            if location:
                tactic_parts.append(f"优先利用{location}的地利")
            if index == 0:
                tactic_parts.append("主动制造信息差")
            else:
                tactic_parts.append("跟进主角留下的线索")

            if dominant_sentiment.startswith("risk:"):
                urgency = min(5, urgency + 1)
                tactic_parts.append("优先消化读者指出的风险点")
            elif dominant_sentiment.startswith("pacing:"):
                urgency = min(5, urgency + 1)
                tactic_parts.append("尽快回应读者对节奏推进的担忧")
            elif dominant_sentiment.startswith("confusion:"):
                tactic_parts.append("优先回应读者困惑较多的悬念信息点")
            elif dominant_sentiment.startswith("character_heat:"):
                tactic_parts.append("顺势照顾读者关注度较高的角色线")
            elif dominant_sentiment.startswith("relationship_interest:"):
                tactic_parts.append("顺势推进读者持续关注的关系/互动张力")
            elif dominant_sentiment.startswith("prediction:"):
                tactic_parts.append("保持受控 ambiguity，不要太早摊开谜底")
            elif dominant_sentiment == "negative":
                urgency = min(5, urgency + 1)
                tactic_parts.append("尽快回应读者对推进节奏的担忧")
            elif dominant_sentiment == "curious":
                tactic_parts.append("优先回应读者最关心的悬念")
            elif dominant_sentiment == "positive":
                tactic_parts.append("照顾读者当前最期待的兑现点")

            notes = f"重要度{entity.importance}，第{chapter_number + 1}章前生效。"
            intents.append(
                NPCIntentDraft(
                    entity_id=entity.id,
                    entity_name=entity.name,
                    intent_kind=intent_kind,
                    objective=objective,
                    tactic="；".join(tactic_parts),
                    urgency=urgency,
                    notes=notes,
                )
            )
        return intents

    def _generate_with_llm(
        self,
        *,
        entities: list[Entity],
        active_threads: list[PlotThread],
        chapter_number: int,
        thread_focus: str,
        feedback_summary: str,
    ) -> list[NPCIntentDraft] | None:
        if self.llm_client is None or not entities:
            return None
        entity_payload = [
            {
                "entity_id": entity.id,
                "entity_name": entity.name,
                "importance": entity.importance,
            }
            for entity in entities
        ]
        prompt = [
            {
                "role": "system",
                "content": "你是网文角色调度器，只输出 JSON，不要解释。",
            },
            {
                "role": "user",
                "content": (
                    "请为下一章生成 NPC 意图，只输出 JSON。\n"
                    f"当前章节：第 {chapter_number} 章\n"
                    f"主线焦点：{thread_focus}\n"
                    f"活跃线程：{json.dumps([t.name for t in active_threads], ensure_ascii=False)}\n"
                    f"读者反馈摘要：{feedback_summary}\n"
                    f"候选角色：{json.dumps(entity_payload, ensure_ascii=False)}\n\n"
                    "返回格式："
                    '{"intents":[{"entity_name":"角色名","intent_kind":"pursue|pressure|evade|ally","objective":"一句中文目标","tactic":"一句中文策略","urgency":1,"notes":"补充说明"}]}'
                ),
            },
        ]
        try:
            raw = call_chat_compat(
                self.llm_client,
                prompt,
                temperature=0.45,
                max_tokens=900,
                response_format={"type": "json_object"},
                task_family="phase4",
                stage_key="npc_intents",
                output_schema={"type": "object"},
            )
        except TypeError as exc:
            if "response_format" not in str(exc):
                logger.warning("Phase4 NPC LLM call failed.", exc_info=True)
                return None
            try:
                raw = call_chat_compat(
                    self.llm_client,
                    prompt,
                    temperature=0.45,
                    max_tokens=900,
                    task_family="phase4",
                    stage_key="npc_intents",
                )
            except Exception:
                logger.warning("Phase4 NPC LLM fallback call failed.", exc_info=True)
                return None
        except Exception:
            logger.warning("Phase4 NPC LLM call failed.", exc_info=True)
            return None
        try:
            payload = parse_llm_json(raw, error_prefix="NPC intent parser")
        except Exception as exc:  # noqa: BLE001
            mark_latest_attempt_parse_failure(
                self.llm_client,
                parser_name="NPC intent parser",
                stage_key="npc_intents",
                schema_name="npc_intents",
                raw_output=raw,
                error=exc,
            )
            logger.warning("Phase4 NPC intent parse failed.", exc_info=True)
            return None
        entity_map = {entity.name: entity for entity in entities}
        intents: list[NPCIntentDraft] = []
        for row in payload.get("intents") or []:
            if not isinstance(row, dict):
                continue
            entity = entity_map.get(str(row.get("entity_name", "")).strip())
            if entity is None:
                continue
            try:
                intents.append(
                    NPCIntentDraft(
                        entity_id=entity.id,
                        entity_name=entity.name,
                        intent_kind=str(row.get("intent_kind") or "pursue"),
                        objective=str(row.get("objective") or "").strip(),
                        tactic=str(row.get("tactic") or "").strip(),
                        urgency=max(1, min(5, int(row.get("urgency") or 3))),
                        notes=str(row.get("notes") or "").strip(),
                    )
                )
            except Exception:
                continue
        return intents or None


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
        project = session.get(Project, project_id)
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
                last_beat.chapter_number if last_beat is not None else thread.opened_at_chapter
            )
            if chapter_number - reference_chapter >= 2:
                stale_threads.append(thread.name)
        feedback = _load_reader_feedback(
            session,
            project.title if project else "",
            project_id=project_id,
            chapter_number=chapter_number,
            limit=8,
            llm_client=self.llm_client,
        )

        llm_turn = self._simulate_with_llm(
            chapter_number=chapter_number,
            active_threads=active_threads,
            stale_threads=stale_threads,
            feedback_summary=str(feedback.get("summary") or ""),
        )
        if llm_turn is not None:
            return llm_turn

        dominant_sentiment = str(feedback.get("dominant_sentiment") or "neutral")
        pressure_level = "steady"
        shifts: list[str] = []
        if stale_threads:
            pressure_level = "rising" if len(stale_threads) == 1 else "critical"
            shifts.append(f"悬置线程：{'、'.join(stale_threads)}")
        if dominant_sentiment.startswith("risk:"):
            pressure_level = "critical"
            shifts.append("读者指出当前情节存在明显风险点")
        elif dominant_sentiment.startswith("pacing:"):
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者对当前节奏存在持续担忧")
        elif dominant_sentiment.startswith("confusion:"):
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者困惑点正在放大世界压迫感")
        elif dominant_sentiment.startswith("character_heat:"):
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者热度正在推高后续兑现压力")
        elif dominant_sentiment.startswith("relationship_interest:"):
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者对关系线的持续关注正在推高互动兑现压力")
        elif dominant_sentiment.startswith("prediction:"):
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者猜测正在放大谜面兑现与 managed ambiguity 的压力")
        elif dominant_sentiment == "negative":
            pressure_level = "critical" if stale_threads else "rising"
            shifts.append("读者对最近推进节奏表达了明显担忧")
        elif dominant_sentiment == "curious":
            if pressure_level == "steady":
                pressure_level = "rising"
            shifts.append("读者对悬念的追问正在放大世界压迫感")
        elif dominant_sentiment == "positive":
            shifts.append("读者期待正在推高后续兑现压力")
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
        active_threads: list[PlotThread],
        stale_threads: list[str],
        feedback_summary: str,
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
                    f"读者反馈摘要：{feedback_summary}\n\n"
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


def _load_reader_feedback(
    session: Session,
    project_title: str,
    *,
    project_id: str = "",
    chapter_number: int = 0,
    limit: int = 6,
    llm_client=None,
) -> dict[str, object]:
    snapshot = build_reader_feedback_snapshot(
        session,
        project_title,
        project_id=project_id,
        chapter_number=chapter_number,
        limit=limit,
        llm_client=llm_client,
        analyze_missing=True,
    )
    return {
        "dominant_sentiment": snapshot["dominant_sentiment"],
        "summary": snapshot["feedback_summary"],
        "highlights": [str(comment.body_text or "")[:120] for comment in snapshot["recent_comments"]],
        "signals": snapshot["signals"],
    }


def save_npc_intents(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    intents: list[NPCIntentDraft],
) -> None:
    for item in intents:
        session.add(
            NPCIntentSnapshot(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                entity_id=item.entity_id,
                entity_name=item.entity_name,
                intent_kind=item.intent_kind,
                objective=item.objective,
                tactic=item.tactic,
                urgency=item.urgency,
                notes=item.notes,
            )
        )
    session.flush()


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
