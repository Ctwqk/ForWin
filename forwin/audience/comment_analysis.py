from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models import (
    CommentSignalCandidate,
    PublisherRawComment,
    Project,
    new_id,
)
from forwin.utils import parse_llm_json
from forwin.llm.compat import call_chat_compat
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure

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


class CommentAnalyzer:
    """Extracts structured reader comment signals for feedback aggregation."""

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
            )
            .scalars()
            .all()
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
                    '{"signals":[{"comment_index":0,'
                    '"signal_type":"...","target_type":"...",'
                    '"target_name":"...","severity":1,"confidence":0.8,'
                    '"evidence_span":"原文摘录"}]}\n\n'
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
        valid_comments = [
            comment for comment in comments if str(comment.body_text or "").strip()
        ]
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


def analyze_recent_comments(
    session: Session,
    project_id: str,
    *,
    analyzer: Callable[..., list[CommentSignalCandidate]],
    limit: int = 8,
) -> list[CommentSignalCandidate]:
    """Analyze recent comments with a caller-bound CommentAnalyzer method."""
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")
    title = str(project.title or "").strip()
    if not title:
        return []
    has_scoped_comments = session.scalar(
        select(PublisherRawComment.id).where(
            PublisherRawComment.project_id == project_id,
        ).limit(1)
    ) is not None
    stmt = select(PublisherRawComment)
    if has_scoped_comments:
        stmt = stmt.where(PublisherRawComment.project_id == project_id)
    else:
        stmt = stmt.where(
            PublisherRawComment.project_id.in_((project_id, "")),
            PublisherRawComment.work_name == title,
        )
    comments = session.scalars(stmt.order_by(
        PublisherRawComment.synced_at.desc(),
        PublisherRawComment.updated_at.desc(),
    ).limit(limit)).all()
    if not comments:
        return []
    return analyzer(session=session, project_id=project_id, comments=comments)
