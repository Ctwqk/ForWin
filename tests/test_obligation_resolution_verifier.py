from __future__ import annotations

from forwin.canon_quality.obligation_verifier import ObligationResolutionVerifier
from forwin.narrative_obligations.types import NarrativeObligation


def _obligation(obligation_type: str = "motivation_gap") -> NarrativeObligation:
    return NarrativeObligation(
        id="obl-verify",
        project_id="p1",
        origin_chapter_number=10,
        obligation_type=obligation_type,
        priority="P1",
        status="active",
        summary="韩砚协助陆明的动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=12,
        payoff_test="必须给出韩砚协助陆明的明确动机证据。",
    )


def test_obligation_verifier_rejects_self_claim_without_evidence() -> None:
    result = ObligationResolutionVerifier().verify(
        obligation=_obligation(),
        chapter_number=12,
        chapter_body="本章已经解释了韩砚的动机。",
        evidence_refs=[],
    )

    assert result.verifier_result == "fail"
    assert result.evidence_refs == []


def test_obligation_verifier_accepts_motivation_gap_with_evidence_and_causal_text() -> None:
    result = ObligationResolutionVerifier().verify(
        obligation=_obligation(),
        chapter_number=12,
        chapter_body="韩砚低声承认，因为陆明握着能救回他妹妹的证据，所以他才冒险协助。",
        evidence_refs=["chapter:12:paragraph:8"],
    )

    assert result.verifier_result == "pass"
    assert result.obligation_id == "obl-verify"
    assert result.resolution_type == "motivation_gap"


def test_obligation_verifier_warns_when_countdown_resolution_lacks_ledger_confirmation() -> None:
    result = ObligationResolutionVerifier().verify(
        obligation=_obligation("countdown_explanation"),
        chapter_number=12,
        chapter_body="倒计时被重置，但正文没有给出 ledger 证据。",
        evidence_refs=["chapter:12:paragraph:4"],
    )

    assert result.verifier_result == "warn"
    assert "ledger" in result.explanation
