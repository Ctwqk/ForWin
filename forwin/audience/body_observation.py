"""Bind explicit observations to accepted BODY and actual Writer input evidence.

This owner never reviews a chapter or changes Canon. Quotes validate attribution,
not the observer's semantic judgment, and no observation establishes causality.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audience.actions import action_hint, action_hint_available
from forwin.candidate_drafts import candidate_body_hash
from forwin.canon.projection_lock import lock_projection_project
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.genesis import PromptTrace
from forwin.models.project import ChapterPlan, Project
from forwin.models.publisher import FeedbackActionRecord
from forwin.writer.feedback_input import text_sha256


def _object(raw: object) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _list(raw: object) -> list:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _utc(value) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def _verified_inputs(
    session: Session,
    action: FeedbackActionRecord,
    candidate: CandidateDraftRecord,
    canon: CanonCommitRecord,
) -> list[dict]:
    trace_meta = _object(_object(candidate.metadata_json).get("prompt_trace"))
    snapshot = _object(trace_meta.get("input_snapshot"))
    if (
        trace_meta.get("trace_scope") != "writer"
        or snapshot.get("chapter_number") != canon.chapter_number
    ):
        return []
    events = _list(snapshot.get("feedback_inputs"))
    try:
        hint_hash = text_sha256(action_hint(action).text)
    except (ValueError, KeyError, TypeError):
        return []
    verified = {}
    for inclusion in _list(action.prompt_inclusions_json):
        if not isinstance(inclusion, dict) or not (
            inclusion.get("input_status") == "attempted_input"
            and inclusion.get("chapter_number") == canon.chapter_number
            and inclusion.get("hint_sha256") == hint_hash
            and inclusion.get("prompt_trace_id")
            and inclusion.get("input_id")
        ):
            continue
        matches = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("input_id") == inclusion["input_id"]
        ]
        if len(matches) != 1:
            continue
        event = matches[0]
        if not (
            event.get("input_status") == "attempted_input"
            and event.get("adapter_outcome") in {"returned", "raised", "unknown"}
            and all(
                inclusion.get(key) == event.get(key)
                for key in (
                    "input_status",
                    "messages_sha256",
                    "stage_key",
                    "adapter_outcome",
                    "exception_type",
                )
            )
            and isinstance(event.get("messages_sha256"), str)
            and len(event["messages_sha256"]) == 64
            and any(
                isinstance(hint, dict)
                and hint.get("action_id") == action.id
                and hint.get("hint_sha256") == hint_hash
                for hint in _list(event.get("hints"))
            )
        ):
            continue
        trace = session.get(
            PromptTrace, inclusion["prompt_trace_id"], populate_existing=True
        )
        if (
            trace is None
            or trace.project_id != action.project_id
            or trace.trace_scope != "writer"
        ):
            continue
        persisted = _object(trace.input_snapshot_json)
        persisted_matches = [
            item
            for item in _list(persisted.get("feedback_inputs"))
            if isinstance(item, dict) and item.get("input_id") == event["input_id"]
        ]
        if (
            persisted.get("chapter_number") != canon.chapter_number
            or trace.stage_key != trace_meta.get("stage_key")
            or len(persisted_matches) != 1
            or persisted_matches[0] != event
            or not trace.created_at
            or not canon.created_at
            or _utc(trace.created_at) > _utc(canon.created_at)
        ):
            continue
        verified[event["input_id"]] = dict(inclusion)
    return [verified[key] for key in sorted(verified)]


def _validated_assessment(assessment: dict, *, body: str, digest: str) -> dict:
    if not isinstance(assessment, dict) or set(assessment) != {
        "assessment",
        "observer",
        "explanation",
        "reviewed_content_sha256",
        "quotes",
    }:
        raise ValueError(
            "BODY assessment requires explicit observer, source, hash and quotes"
        )
    observer = assessment.get("observer")
    status = assessment.get("assessment")
    if (
        status not in {"observed", "not_observed", "unknown"}
        or not isinstance(observer, dict)
        or set(observer) != {"kind", "source_ref"}
        or observer.get("kind") not in {"human", "frozen_observer"}
        or not isinstance(observer.get("source_ref"), str)
        or not observer["source_ref"].strip()
        or not isinstance(assessment.get("explanation"), str)
        or not assessment["explanation"].strip()
        or assessment.get("reviewed_content_sha256") != digest
    ):
        raise ValueError("BODY assessment identity or observer source is invalid")
    quotes = assessment.get("quotes")
    if not isinstance(quotes, list) or (status != "unknown" and not quotes):
        raise ValueError("BODY assessment needs exact BODY quotes")
    for quote in quotes:
        if (
            not isinstance(quote, dict)
            or set(quote) != {"start", "end", "text"}
            or type(quote.get("start")) is not int
            or type(quote.get("end")) is not int
            or not 0 <= quote["start"] < quote["end"] <= len(body)
            or quote.get("text") != body[quote["start"] : quote["end"]]
        ):
            raise ValueError("BODY quote does not match the accepted content")
    return assessment


class FeedbackBodyObservationService:
    def record(
        self,
        *,
        session: Session,
        project_id: str,
        action_id: str,
        canon_commit_id: str,
        assessment: dict | None = None,
    ) -> dict:
        """Append evidence in the caller transaction; absent assessment stays unknown.

        Caller must flush its owning writes first. Assessed entries require the
        same immutable Canon plan and three agreeing copies of input provenance.
        Missing legacy evidence is recorded as unknown, never reconstructed from
        the current plan or a different attempt from the same chapter.
        """
        if session.new or session.dirty or session.deleted:
            raise ValueError("BODY observation requires flushed owner inputs")
        with session.no_autoflush:
            lock_projection_project(session, project_id)
            if session.get(Project, project_id) is None:
                raise ValueError("BODY observation project not found")
            action = session.scalar(
                select(FeedbackActionRecord)
                .where(
                    FeedbackActionRecord.id == action_id,
                    FeedbackActionRecord.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            canon = session.get(
                CanonCommitRecord, canon_commit_id, populate_existing=True
            )
            if (
                action is None
                or canon is None
                or canon.project_id != project_id
                or canon.status != "committed"
            ):
                raise ValueError(
                    "BODY observation needs an owned committed Canon and action"
                )
            try:
                qualified = action_hint_available(action, canon.chapter_number)
            except (TypeError, ValueError):
                qualified = False
            if not qualified:
                raise ValueError(
                    "BODY observation action is not selected, qualified or in scope"
                )
            chapter = session.get(
                ChapterPlan, canon.chapter_plan_id, populate_existing=True
            )
            candidate = session.get(
                CandidateDraftRecord, canon.candidate_id, populate_existing=True
            )
            if (
                chapter is None
                or chapter.project_id != project_id
                or chapter.chapter_number != canon.chapter_number
                or candidate is None
                or candidate.project_id != project_id
                or candidate.chapter_plan_id != chapter.id
                or candidate.chapter_number != canon.chapter_number
            ):
                raise ValueError("BODY observation Canon candidate ownership mismatch")
            draft = session.get(
                ChapterDraft, candidate.candidate_draft_id, populate_existing=True
            )
            if (
                draft is None
                or draft.chapter_plan_id != chapter.id
                or not draft.body_text
            ):
                raise ValueError("BODY observation accepted draft identity unavailable")
            digest = candidate_body_hash(draft.body_text)
            if not candidate.body_hash or candidate.body_hash != digest:
                raise ValueError("BODY changed after acceptance")
            plan = _object(candidate.canon_commit_plan_json)
            plan_revision = (
                plan.get("plan_revision")
                if (
                    all(
                        plan.get(key) == value
                        for key, value in {
                            "canon_commit_id": canon.id,
                            "candidate_id": candidate.id,
                            "candidate_body_hash": digest,
                            "project_id": project_id,
                            "chapter_number": canon.chapter_number,
                        }.items()
                    )
                    and isinstance(plan.get("plan_revision"), str)
                    and plan["plan_revision"]
                    and plan["plan_revision"] == candidate.plan_revision
                )
                else None
            )
            inputs = _verified_inputs(session, action, candidate, canon)
            reasons = []
            if not inputs:
                reasons.append("writer_input_unverified")
            if not plan_revision:
                reasons.append("plan_identity_unavailable")
            if assessment is not None:
                _validated_assessment(assessment, body=draft.body_text, digest=digest)
                if assessment["assessment"] != "unknown":
                    if not inputs:
                        raise ValueError("BODY assessment lacks verified Writer input")
                    if not plan_revision:
                        raise ValueError(
                            "BODY assessment lacks immutable plan identity"
                        )
            else:
                reasons.append("unassessed")
            source = {
                "aggregate_id": action.aggregate_id,
                "aggregate_evidence": _object(action.aggregate_evidence_json),
                "action_payload": _object(action.action_payload_json),
                "direction": action.direction,
                "source_qualified": action.source_qualified,
                "selected_at_chapter": action.selected_at_chapter,
                "selected_at": _utc(action.selected_at),
                "target_chapter_start": action.target_chapter_start,
                "target_chapter_end": action.target_chapter_end,
                "hint_valid_from_chapter": action.hint_valid_from_chapter,
                "hint_expires_at_chapter": action.hint_expires_at_chapter,
            }
            result = {
                "action_id": action.id,
                "canon_commit_id": canon.id,
                "chapter_plan_id": chapter.id,
                "chapter_number": canon.chapter_number,
                "candidate_id": candidate.id,
                "draft_id": draft.id,
                "content_sha256": digest,
                "committed_at": _utc(canon.created_at),
                "plan_revision": plan_revision,
                "body_seen": True,
                "assessment": assessment["assessment"] if assessment else "unknown",
                "quotes": assessment["quotes"] if assessment else [],
                "observer": assessment["observer"] if assessment else None,
                "explanation": assessment["explanation"] if assessment else "",
                "reviewed_content_sha256": assessment["reviewed_content_sha256"]
                if assessment
                else None,
                "prompt_input_ids": [item["input_id"] for item in inputs],
                "prompt_trace_ids": sorted(
                    {item["prompt_trace_id"] for item in inputs}
                ),
                "input_evidence": inputs,
                "source": source,
                "source_sha256": _sha(source),
                "reasons": reasons,
                "causal_claim": False,
            }
            result["evidence_sha256"] = _sha(result)
            raw = action.body_observation_json
            history = _object(raw)
            if raw not in (None, "", "{}") and (
                history.get("version") != 1
                or not isinstance(history.get("observations"), list)
                or not all(isinstance(item, dict) for item in history["observations"])
            ):
                raise ValueError(
                    "BODY observation history is invalid; preserve original evidence"
                )
            observations = history.get("observations", [])
            if result not in observations:
                action.body_observation_json = json.dumps(
                    {"version": 1, "observations": [*observations, result]},
                    ensure_ascii=False,
                )
                session.flush()
            return result
