"""One computation owner for comment windows and their immutable read views."""

from __future__ import annotations

import json
from collections import defaultdict
from hashlib import sha256
from math import isfinite

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from forwin.audience.comment_analysis import current_signal_condition
from forwin.audience.directions import DIRECTIONS
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.publisher import (
    CommentAnalysisRecord,
    CommentSignalCandidate,
    PublisherRawComment,
    SignalWindowAggregate,
)
from forwin.publisher_runtime.comment_source import (
    body_hash,
    observed_time,
    source_hash,
    source_identity,
)

AGGREGATION_VERSION = "comment-window-v2"
MIN_CONFIDENCE = 0.8


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _time_window(values):
    values = list(values)
    known = sorted(
        parsed.isoformat()
        for value in values
        if (parsed := observed_time(value)) is not None
    )
    return {
        "start": known[0] if known else "",
        "end": known[-1] if known else "",
        "unknown_count": len(values) - len(known),
    }


def _analysis_is_current(comment, analysis):
    if analysis is None or not (
        analysis.status == "completed"
        and analysis.source_comment_id == comment.id
        and analysis.project_id == comment.project_id
        and analysis.content_sha256
        == comment.content_sha256
        == body_hash(comment.body_text or "")
        and analysis.source_sha256 == comment.source_sha256 == source_hash(comment)
        and analysis.input_body == (comment.body_text or "")
    ):
        return False
    try:
        saved = json.loads(analysis.source_identity_json)
    except (TypeError, ValueError):
        return False
    return isinstance(saved, dict) and all(
        saved.get(k) == v for k, v in source_identity(comment).items()
    )


def _published(comment, publications, commits, accepted_bodies):
    publication = publications.get(comment.source_publication_id)
    commit = commits.get(comment.source_canon_commit_id)
    body = accepted_bodies.get(commit.candidate_id) if commit else None
    return bool(
        comment.source_status == "confirmed"
        and publication
        and commit
        and publication.state == "published"
        and commit.status == "committed"
        and body
        and body["project_id"] == commit.project_id
        and body["chapter_plan_id"]
        == body["draft_chapter_plan_id"]
        == commit.chapter_plan_id
        and body["chapter_number"] == commit.chapter_number
        and publication.content_sha256
        == body["body_sha256"]
        == body["declared_body_sha256"]
        and publication.project_id == commit.project_id == comment.project_id
        and publication.chapter_plan_id
        == commit.chapter_plan_id
        == comment.source_chapter_plan_id
        and publication.chapter_number
        == commit.chapter_number
        == comment.source_chapter_number
        and publication.canon_commit_id == commit.id
        and publication.platform_id == comment.platform_id
        and publication.remote_book_id == comment.work_id
        and publication.remote_chapter_id == comment.chapter_id
    )


def aggregate_window(
    session, *, project_id, chapter_start, chapter_end, window_type="short", scale=None
):
    """Read all inputs before bucketing; persist a new snapshot only for new evidence.

    Raw row share locks serialize this finite read against ingestion and analysis's
    raw row write locks. No model calls occur while constructing a window.
    """
    from forwin.audience.feedback import classify_signal_level, estimate_reader_scale

    comments = session.scalars(
        select(PublisherRawComment)
        .execution_options(populate_existing=True)
        .where(PublisherRawComment.project_id == project_id)
        .order_by(PublisherRawComment.id)
        .with_for_update(read=True)
    ).all()
    unscoped = [
        c
        for c in comments
        if c.source_status == "unknown" or not c.source_chapter_number
    ]
    comments = [
        c
        for c in comments
        if c.source_status in {"chapter_known", "confirmed"}
        and chapter_start <= (c.source_chapter_number or 0) <= chapter_end
    ]
    if not comments:
        return []
    by_id = {c.id: c for c in comments}
    analyses = {
        a.id: a
        for a in session.scalars(
            select(CommentAnalysisRecord)
            .execution_options(populate_existing=True)
            .where(
                CommentAnalysisRecord.id.in_([c.active_analysis_id for c in comments])
            )
        ).all()
    }
    completed = {
        c.id
        for c in comments
        if _analysis_is_current(c, analyses.get(c.active_analysis_id))
    }
    publications = {
        p.id: p
        for p in session.scalars(
            select(CanonPublicationProtection)
            .execution_options(populate_existing=True)
            .where(
                CanonPublicationProtection.id.in_(
                    [c.source_publication_id for c in comments]
                )
            )
        ).all()
    }
    commits = {
        c.id: c
        for c in session.scalars(
            select(CanonCommitRecord)
            .execution_options(populate_existing=True)
            .where(
                CanonCommitRecord.id.in_([c.source_canon_commit_id for c in comments])
            )
        ).all()
    }
    accepted_bodies = {
        candidate.id: {
            "candidate_id": candidate.id,
            "draft_id": draft.id,
            "project_id": candidate.project_id,
            "chapter_plan_id": candidate.chapter_plan_id,
            "chapter_number": candidate.chapter_number,
            "draft_chapter_plan_id": draft.chapter_plan_id,
            "body_sha256": body_hash(draft.body_text),
            "declared_body_sha256": candidate.body_hash,
        }
        for candidate, draft in session.execute(
            select(CandidateDraftRecord, ChapterDraft)
            .execution_options(populate_existing=True)
            .join(
                ChapterDraft, ChapterDraft.id == CandidateDraftRecord.candidate_draft_id
            )
            .where(
                CandidateDraftRecord.id.in_(
                    [commit.candidate_id for commit in commits.values()]
                )
            )
        )
    }
    signals = session.scalars(
        select(CommentSignalCandidate)
        .execution_options(populate_existing=True)
        .where(
            CommentSignalCandidate.project_id == project_id,
            current_signal_condition(),
            CommentSignalCandidate.source_comment_id.in_(completed),
        )
        .order_by(CommentSignalCandidate.id)
    ).all()
    if not signals:
        return []
    if scale is None:
        scale = estimate_reader_scale(session, project_id, chapter_number=chapter_end)
    manifest = defaultdict(
        lambda: {
            "chapter_numbers": set(),
            "canon_commit_ids": set(),
            "publication_ids": set(),
            "platforms": set(),
        }
    )
    window_reasons = set()
    if unscoped:
        window_reasons.add("unscoped_comments")
    if len(completed) != len(comments):
        window_reasons.add("incomplete_analysis")
    analyzer_versions = {
        analyses[by_id[c].active_analysis_id].analyzer_version for c in completed
    }
    if len(analyzer_versions) > 1:
        window_reasons.add("mixed_analyzer_versions")
    for comment in comments:
        if not _published(comment, publications, commits, accepted_bodies):
            window_reasons.add("unproven_publication")
        entry = manifest[comment.source_chapter_plan_id or "unknown"]
        entry["chapter_numbers"].add(comment.source_chapter_number)
        entry["canon_commit_ids"].add(comment.source_canon_commit_id)
        entry["publication_ids"].add(comment.source_publication_id)
        entry["platforms"].add(comment.platform_id)
    if any(len(entry["canon_commit_ids"]) > 1 for entry in manifest.values()):
        window_reasons.add("mixed_publication_versions")
    source_scope = {
        "chapter_start": chapter_start,
        "chapter_end": chapter_end,
        "timing": {
            "received_at": _time_window(c.ingested_at for c in comments),
            "observed_at": _time_window(c.observed_at for c in comments),
            "remote_created_at": _time_window(c.remote_created_at for c in comments),
        },
        "chapters": {
            key: {k: sorted(v) for k, v in value.items()}
            for key, value in sorted(manifest.items())
        },
    }
    input_comments = [
        {
            "comment_id": c.id,
            "remote_created_at": c.remote_created_at,
            "observed_at": str(c.observed_at or ""),
            "ingested_at": str(c.ingested_at or ""),
            "analyzed_at": str(
                getattr(analyses.get(c.active_analysis_id), "analyzed_at", "") or ""
            ),
            "analysis_source_snapshot": json.loads(
                analyses[c.active_analysis_id].source_identity_json
            )
            if c.id in completed
            else {},
            "body_sha256": c.content_sha256,
            "source_sha256": c.source_sha256,
            "source_identity": source_identity(c),
            "analysis_id": c.active_analysis_id,
            "analysis_complete": c.id in completed,
            "analyzer_version": getattr(
                analyses.get(c.active_analysis_id), "analyzer_version", ""
            ),
        }
        for c in comments
    ]
    buckets = defaultdict(list)
    for signal in signals:
        direction = signal.direction or "unknown"
        if direction not in DIRECTIONS.get(signal.signal_type, ()):
            direction = "unknown"
        target = (signal.target_name or "").strip()
        target = "" if target in {"general", "整体"} else target
        buckets[(signal.signal_type, signal.target_type, target, direction)].append(
            signal
        )
    rows = []
    for (signal_type, target_type, target, direction), bucket in sorted(
        buckets.items()
    ):
        reasons = set(window_reasons)
        if direction == "unknown":
            reasons.add("unknown_direction")
        # A comment has one vote in this bucket, even if the analyzer repeats a span.
        votes = defaultdict(list)
        for signal in bucket:
            votes[signal.source_comment_id].append(signal)
        authors = set()
        platform_authors = defaultdict(set)
        platform_chapters = defaultdict(set)
        platform_severity = defaultdict(int)
        unknown_authors = 0
        confidences = []
        severity = 0
        for comment_id, duplicates in votes.items():
            comment = by_id[comment_id]
            author = (comment.author_id or "").strip()
            if author:
                authors.add((comment.platform_id, author))
                platform_authors[comment.platform_id].add(author)
            else:
                unknown_authors += 1
            platform_chapters[comment.platform_id].add(comment.source_chapter_number)
            # Conflicting/weak duplicate assertions cannot inflate confidence.
            invalid_confidence = any(
                not isfinite(s.confidence) or not 0 <= s.confidence <= 1
                for s in duplicates
            )
            if invalid_confidence:
                reasons.add("invalid_confidence")
            confidence = (
                0 if invalid_confidence else min(s.confidence for s in duplicates)
            )
            confidences.append(confidence)
            comment_severity = max(s.severity for s in duplicates)
            severity = max(severity, comment_severity)
            platform_severity[comment.platform_id] = max(
                platform_severity[comment.platform_id], comment_severity
            )
            if confidence < MIN_CONFIDENCE:
                reasons.add("low_confidence")
            if any(
                not s.evidence_span or s.evidence_span not in comment.body_text
                for s in duplicates
            ):
                reasons.add("ungrounded_evidence")
        if unknown_authors:
            reasons.add("unknown_authors")
        # Cross-platform accounts are not proven distinct humans: qualify within
        # a platform, then take the strongest supported level, never sum users.
        levels = [
            classify_signal_level(
                unique_users=len(users),
                spans_chapters=len(platform_chapters[platform]),
                severity=platform_severity[platform],
                signal_type=signal_type,
            )
            for platform, users in platform_authors.items()
        ]
        order = {"noise": 0, "candidate": 1, "watchlist": 2, "confirmed": 3}
        level = (
            max(levels, key=lambda v: order[v], default="noise")
            if not reasons
            else "noise"
        )
        signal_key = _json([signal_type, target_type, target or "general", direction])
        proof = {
            "aggregation_version": AGGREGATION_VERSION,
            "project_id": project_id,
            "signal_key": signal_key,
            "window_type": window_type,
            "source_scope": source_scope,
            "qualification_reasons": sorted(reasons),
            "input_comments": input_comments,
            "unscoped_comment_ids": sorted(c.id for c in unscoped),
            "evidence_comment_ids": sorted(votes),
            "evidence_analysis_ids": sorted(
                {by_id[c].active_analysis_id for c in votes}
            ),
            "signal_evidence": [
                {
                    "id": s.id,
                    "analysis_id": s.analysis_id,
                    "confidence": s.confidence
                    if isfinite(s.confidence)
                    else str(s.confidence),
                    "severity": s.severity,
                    "direction": s.direction,
                    "quote": s.evidence_span,
                }
                for s in bucket
            ],
            "platform_consensus": {
                platform: {
                    "known_authors": len(users),
                    "chapters": sorted(platform_chapters[platform]),
                    "max_severity": platform_severity[platform],
                }
                for platform, users in sorted(platform_authors.items())
            },
            "consensus_author_count": max(
                map(len, platform_authors.values()), default=0
            ),
            "spans_chapters": len({by_id[c].source_chapter_number for c in votes}),
            "accepted_body_evidence": [
                accepted_bodies[key] for key in sorted(accepted_bodies)
            ],
            "publication_evidence": [
                {
                    "id": p.id,
                    "canon_commit_id": p.canon_commit_id,
                    "chapter_plan_id": p.chapter_plan_id,
                    "chapter_number": p.chapter_number,
                    "platform_id": p.platform_id,
                    "remote_book_id": p.remote_book_id,
                    "remote_chapter_id": p.remote_chapter_id,
                    "content_sha256": p.content_sha256,
                    "state": p.state,
                }
                for p in sorted(publications.values(), key=lambda p: p.id)
            ],
            "reader_scale": {
                "estimate": scale.reader_estimate,
                "tier": scale.tier,
                "method": scale.estimation_method,
            },
            "min_confidence": MIN_CONFIDENCE,
        }
        digest = sha256(_json(proof).encode()).hexdigest()
        values = {
            "id": new_id(),
            "project_id": project_id,
            "aggregation_version": AGGREGATION_VERSION,
            "evidence_sha256": digest,
            "provenance_json": _json(proof),
            "direction": direction,
            "signal_key": signal_key,
            "signal_type": signal_type,
            "target_type": target_type,
            "target_name": target,
            "window_type": window_type,
            "window_chapter_start": chapter_start,
            "window_chapter_end": chapter_end,
            "total_comment_count": len(comments),
            "analyzed_comment_count": len(completed),
            "hit_comment_count": len(votes),
            "known_author_count": len(authors),
            "unique_user_count": max(map(len, platform_authors.values()), default=0),
            "unknown_author_comment_count": unknown_authors,
            "source_qualified": not reasons,
            "max_severity": severity,
            "avg_confidence": sum(confidences) / len(confidences),
            "signal_level": level,
            "reader_estimate": scale.reader_estimate,
            "reader_tier": scale.tier,
            "estimation_method": scale.estimation_method,
            "scale_confidence": 0.9
            if scale.estimation_method.startswith("platform_metric:")
            else 0.35,
        }
        session.execute(
            insert(SignalWindowAggregate).values(**values).on_conflict_do_nothing()
        )
        rows.append(
            session.scalar(
                select(SignalWindowAggregate).where(
                    SignalWindowAggregate.project_id == project_id,
                    SignalWindowAggregate.aggregation_version == AGGREGATION_VERSION,
                    SignalWindowAggregate.evidence_sha256 == digest,
                )
            )
        )
    return rows


def aggregate_view(row):
    """The only aggregate read DTO; downstream owners do not recompute decisions."""
    proof = json.loads(row.provenance_json or "{}")
    return {
        "project_id": row.project_id,
        "window_type": row.window_type,
        "aggregate_id": row.id,
        "snapshot_created_at": row.created_at.isoformat() if row.created_at else None,
        "aggregation_version": row.aggregation_version,
        "evidence_sha256": row.evidence_sha256,
        "signal_key": row.signal_key,
        "signal_type": row.signal_type,
        "direction": row.direction,
        "target_type": row.target_type,
        "target_name": row.target_name,
        "level": row.signal_level,
        "signal_level": row.signal_level,
        "hit_count": row.hit_comment_count,
        "hit_comment_count": row.hit_comment_count,
        "unique_users": row.unique_user_count,
        "known_author_count": row.known_author_count,
        "unknown_author_comment_count": row.unknown_author_comment_count,
        "total_comment_count": row.total_comment_count,
        "analyzed_comment_count": row.analyzed_comment_count,
        "max_severity": row.max_severity,
        "spans_chapters": proof.get("spans_chapters", 0),
        "input_comments": proof.get("input_comments", []),
        "publication_evidence": proof.get("publication_evidence", []),
        "accepted_body_evidence": proof.get("accepted_body_evidence", []),
        "source_qualified": row.source_qualified,
        "source_scope": proof.get("source_scope", {}),
        "qualification_reasons": proof.get(
            "qualification_reasons", ["unversioned_aggregate"]
        ),
        "evidence_comment_ids": proof.get("evidence_comment_ids", []),
        "evidence_analysis_ids": proof.get("evidence_analysis_ids", []),
    }
