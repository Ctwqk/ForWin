from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import (
    NarrativeObligation,
    ObligationResolutionCandidate,
)


_CAUSAL_MARKERS = ("因为", "为了", "所以", "原因", "动机", "才会", "承认")


class ObligationResolutionVerifier:
    def verify(
        self,
        *,
        obligation: NarrativeObligation,
        chapter_number: int = 0,
        chapter_body: str = "",
        evidence_refs: list[str] | None = None,
        ledger_payload: dict[str, Any] | None = None,
        accepted_chapter_text: str = "",
    ) -> ObligationResolutionCandidate:
        body = str(accepted_chapter_text or chapter_body or "")
        resolved_chapter = int(chapter_number or obligation.deadline_chapter or 0)
        refs = [str(item) for item in evidence_refs or [] if str(item or "").strip()]
        markers = _matched_payoff_markers(payoff_test=obligation.payoff_test, text=body)
        if accepted_chapter_text and not refs:
            refs = [f"chapter:{resolved_chapter}"]
        if not refs:
            return ObligationResolutionCandidate(
                obligation_id=obligation.id,
                chapter_number=resolved_chapter,
                resolution_type=obligation.obligation_type,
                evidence_refs=[],
                explanation="resolution rejected: evidence_refs are required",
                verifier_result="fail",
                status="fail",
                matched_markers=[],
                reason="resolution rejected: evidence_refs are required",
            )

        verifier_result = self._verify_by_type(
            obligation_type=obligation.obligation_type,
            chapter_body=body,
            ledger_payload=ledger_payload or {},
        )
        if markers and verifier_result != "fail":
            verifier_result = "pass"
        explanation = self._explanation_for(obligation.obligation_type, verifier_result)
        return ObligationResolutionCandidate(
            obligation_id=obligation.id,
            chapter_number=resolved_chapter,
            resolution_type=obligation.obligation_type,
            evidence_refs=refs,
            explanation=explanation,
            verifier_result=verifier_result,
            status=verifier_result,  # type: ignore[arg-type]
            matched_markers=markers,
            reason=explanation,
        )

    @staticmethod
    def _verify_by_type(
        *,
        obligation_type: str,
        chapter_body: str,
        ledger_payload: dict[str, Any],
    ) -> str:
        if obligation_type == "motivation_gap":
            return "pass" if any(marker in chapter_body for marker in _CAUSAL_MARKERS) else "warn"
        if obligation_type in {"countdown_explanation", "artifact_count_explanation"}:
            status = str(ledger_payload.get("status") or "").strip().lower()
            return "pass" if status in {"consistent", "resolved"} else "warn"
        if obligation_type == "transition_bridge_needed":
            return "pass" if any(marker in chapter_body for marker in ("随后", "因此", "转而", "赶到", "醒来")) else "warn"
        if obligation_type in {"identity_ambiguity", "reveal_escalation_needed"}:
            return "pass" if any(marker in chapter_body for marker in ("真相", "身份", "伪装", "谎言", "证据")) else "warn"
        return "pass"

    @staticmethod
    def _explanation_for(obligation_type: str, result: str) -> str:
        if result == "pass":
            return f"{obligation_type} obligation has concrete resolution evidence"
        if obligation_type in {"countdown_explanation", "artifact_count_explanation"}:
            return f"{obligation_type} obligation has textual evidence but no confirmed ledger payload"
        return f"{obligation_type} obligation has evidence refs but weak deterministic confirmation"


def verify_active_obligations_after_acceptance(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    accepted_text: str,
    verifier: ObligationResolutionVerifier | None = None,
) -> dict[str, list[str]]:
    repo = NarrativeObligationRepository(session)
    resolved_ids: list[str] = []
    warned_ids: list[str] = []
    failed_ids: list[str] = []
    checker = verifier or ObligationResolutionVerifier()
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number):
        result = checker.verify(
            obligation=obligation,
            chapter_number=chapter_number,
            accepted_chapter_text=accepted_text,
        )
        if result.status == "pass":
            repo.mark_obligation_resolved(
                obligation.id,
                verifier_result=result.model_dump(mode="json"),
                evidence_refs=result.evidence_refs,
                resolution_chapter=chapter_number,
            )
            resolved_ids.append(obligation.id)
        elif result.status == "warn":
            warned_ids.append(obligation.id)
        else:
            failed_ids.append(obligation.id)
    return {
        "resolved_obligation_ids": resolved_ids,
        "warned_obligation_ids": warned_ids,
        "failed_obligation_ids": failed_ids,
    }


def expire_unresolved_obligations_after_acceptance(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> dict[str, list[str]]:
    repo = NarrativeObligationRepository(session)
    expired_ids: list[str] = []
    blocked_ids: list[str] = []
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number + 1):
        if obligation.status != "active":
            continue
        if int(obligation.deadline_chapter or 0) > int(chapter_number or 0):
            continue
        expired = repo.expire_obligation(
            obligation.id,
            reason="deadline passed after accepted chapter",
        )
        if expired is None:
            continue
        expired_ids.append(expired.id)
        if expired.blocking_policy == "block_at_deadline":
            blocked = repo.block_expired_obligation(expired.id)
            if blocked is not None:
                blocked_ids.append(blocked.id)
    return {
        "expired_obligation_ids": expired_ids,
        "blocked_obligation_ids": blocked_ids,
    }


_PAYOFF_STOP_WORDS = {
    "必须",
    "需要",
    "解释",
    "给出",
    "完成",
    "偿还",
    "终章",
    "本章",
}


def _matched_payoff_markers(*, payoff_test: str, text: str) -> list[str]:
    source = str(payoff_test or "")
    body = str(text or "")
    markers: list[str] = []
    for raw in re.split(r"[，。；、,.;:\s]+", source):
        candidate = raw.strip()
        candidate = re.sub(r"^第?\d+章?(前|内)?", "", candidate)
        for stop_word in _PAYOFF_STOP_WORDS:
            candidate = candidate.replace(stop_word, "")
        candidate = candidate.strip()
        if len(candidate) < 2:
            continue
        if candidate in body and candidate not in markers:
            markers.append(candidate)
    return markers
