from __future__ import annotations

from typing import Any

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
        chapter_number: int,
        chapter_body: str,
        evidence_refs: list[str],
        ledger_payload: dict[str, Any] | None = None,
    ) -> ObligationResolutionCandidate:
        refs = [str(item) for item in evidence_refs if str(item or "").strip()]
        if not refs:
            return ObligationResolutionCandidate(
                obligation_id=obligation.id,
                chapter_number=int(chapter_number or 0),
                resolution_type=obligation.obligation_type,
                evidence_refs=[],
                explanation="resolution rejected: evidence_refs are required",
                verifier_result="fail",
            )

        body = str(chapter_body or "")
        verifier_result = self._verify_by_type(
            obligation_type=obligation.obligation_type,
            chapter_body=body,
            ledger_payload=ledger_payload or {},
        )
        return ObligationResolutionCandidate(
            obligation_id=obligation.id,
            chapter_number=int(chapter_number or 0),
            resolution_type=obligation.obligation_type,
            evidence_refs=refs,
            explanation=self._explanation_for(obligation.obligation_type, verifier_result),
            verifier_result=verifier_result,
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
