"""Version-bound receipts from the existing semantic chapter review.

This module validates identity and grounding, never infers payoff from words.
Serialized snapshots keep nested review data immutable inside a Canon plan.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class FrozenEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ObligationContractSnapshot(FrozenEvidence):
    obligation_id: str
    contract_fingerprint: str
    contract_json: str


class ObligationResolutionEvidence(FrozenEvidence):
    obligation_id: str
    contract_fingerprint: str
    candidate_id: str
    draft_id: str
    body_sha256: str
    review_source: Literal["chapter_review_form", "historical_chapter_review_form"]
    review_identity: str
    judgment: Literal["fulfilled"] = "fulfilled"
    answer_json: str
    evidence_refs: tuple[str, ...]


class ObligationResolutionPlan(FrozenEvidence):
    project_id: str
    chapter_number: int
    candidate_id: str
    draft_id: str
    body_sha256: str
    review_source: Literal["chapter_review_form", "historical_chapter_review_form"] = (
        "chapter_review_form"
    )
    review_identity: str = ""
    form_json: str = ""
    answers_json: str = ""
    validation_report_json: str = ""
    contracts: tuple[ObligationContractSnapshot, ...] = ()
    evidence: tuple[ObligationResolutionEvidence, ...] = ()

    @property
    def resolved_obligation_ids(self) -> list[str]:
        return [item.obligation_id for item in self.evidence]


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def contract_snapshot(obligation: Any) -> ObligationContractSnapshot:
    value = lambda key, default: (
        obligation.get(key, default)
        if isinstance(obligation, dict)
        else getattr(obligation, key, default)
    )
    # All semantic and scheduling dependencies, excluding mutable lifecycle/audit
    # fields and context-derived must_resolve_now. Planned -> active is legitimate.
    defaults = {
        "id": "",
        "project_id": "",
        "origin_chapter_number": 0,
        "origin_draft_id": "",
        "origin_review_id": "",
        "origin_signal_ids": [],
        "origin_plan_snapshot_id": "",
        "obligation_type": "",
        "priority": "P1",
        "summary": "",
        "deferral_reason": "",
        "hardness": "soft_gap",
        "subject_refs": [],
        "evidence_refs": [],
        "deadline_chapter": 0,
        "deadline_policy": "block_at_deadline",
        "payoff_test": "",
        "resolution_conditions": [],
        "linked_plan_patch_ids": [],
        "linked_future_chapters": [],
        "blocking_policy": "block_at_deadline",
        "created_by": "system",
    }
    contract = {key: value(key, default) for key, default in defaults.items()}
    encoded = canonical_json(contract)
    return ObligationContractSnapshot(
        obligation_id=contract["id"],
        contract_fingerprint=body_sha256(encoded),
        contract_json=encoded,
    )


def _grounded(answer, body: str, expected_value: str) -> bool:
    return bool(
        answer is not None
        and answer.value == expected_value
        and answer.confidence >= 0.8
        and answer.evidence_quote
        and body.count(answer.evidence_quote) == 1
        and answer.subject_of_quote.strip()
        and answer.explanation.strip()
    )


def fulfillment_refs(ask, answer, body: str) -> tuple[str, ...]:
    """Require semantic subject and each condition assessment from the reviewer."""
    conditions = list(dict.fromkeys([ask.payoff_test, *ask.resolution_conditions]))
    if not ask.payoff_test.strip() or not ask.contract_fingerprint:
        return ()
    results = answer.condition_results
    if (
        len(results) != len(conditions)
        or {item.condition for item in results} != set(conditions)
        or not _grounded(answer.addressed, body, "fulfilled")
        or not _grounded(answer.payoff_evidence, body, "fulfilled")
        or not _grounded(answer.subject_matches, body, "true")
        or any(not _grounded(item.assessment, body, "fulfilled") for item in results)
    ):
        return ()
    quotes = [
        answer.addressed.evidence_quote,
        answer.payoff_evidence.evidence_quote,
        answer.subject_matches.evidence_quote,
        *[item.assessment.evidence_quote for item in results],
    ]
    digest = body_sha256(body)
    return tuple(
        dict.fromkeys(
            f"body:{digest}#{body.index(quote)}:{body.index(quote) + len(quote)}"
            for quote in quotes
        )
    )


def build_resolution_plan(
    *,
    obligations,
    form,
    answers,
    project_id: str,
    chapter_number: int,
    draft_id: str,
    candidate_id: str,
    chapter_body: str,
    review_source="chapter_review_form",
    validation_report=None,
) -> ObligationResolutionPlan:
    from forwin.canon_quality.chapter_review_form.form_schema import (
        ChapterReviewAnswers,
        ChapterReviewForm,
    )

    contracts = tuple(
        sorted(
            (contract_snapshot(item) for item in obligations),
            key=lambda item: item.obligation_id,
        )
    )
    body_hash = body_sha256(chapter_body)
    base = {
        "project_id": project_id,
        "chapter_number": chapter_number,
        "draft_id": draft_id,
        "candidate_id": candidate_id,
        "body_sha256": body_hash,
        "review_source": review_source,
        "contracts": contracts,
    }
    if form is None or answers is None:
        return ObligationResolutionPlan(**base)
    form = ChapterReviewForm.model_validate(form)
    answers = ChapterReviewAnswers.model_validate(answers)
    if (form.project_id, form.chapter_number) != (project_id, chapter_number) or (
        answers.project_id,
        answers.chapter_number,
        answers.form_schema_version,
    ) != (project_id, chapter_number, form.form_schema_version):
        raise ValueError("obligation review identity mismatch")
    asks = {item.id: item for item in form.obligations}
    answers_by_id = {item.id: item for item in answers.obligations}
    if (
        len(asks) != len(form.obligations)
        or len(answers_by_id) != len(answers.obligations)
        or set(asks) != set(answers_by_id)
    ):
        raise ValueError("obligation review tracked identity coverage mismatch")
    if asks and (
        form.reviewed_body_sha256 != body_hash
        or answers.reviewed_body_sha256 != body_hash
    ):
        raise ValueError("obligation review body identity changed or unknown")
    for item in obligations:
        if (
            getattr(item, "status", "") == "planned"
            and item.id in asks
            and item.origin_draft_id != draft_id
        ):
            raise ValueError("obligation planned origin draft mismatch")
    current = {item.obligation_id: item for item in contracts}
    for key, ask in asks.items():
        if key not in current or (ask.contract_fingerprint, ask.contract_json) != (
            current[key].contract_fingerprint,
            current[key].contract_json,
        ):
            raise ValueError("obligation review contract changed")
        contract = json.loads(current[key].contract_json)
        if any(
            getattr(ask, field) != contract[field]
            for field in (
                "summary",
                "payoff_test",
                "subject_refs",
                "resolution_conditions",
                "deadline_chapter",
            )
        ):
            raise ValueError("obligation review contract fields mismatch")
    from forwin.canon_quality.chapter_review_form.evidence_validator import (
        ValidationReport,
        validate_answers,
    )

    report = (
        ValidationReport.model_validate(validation_report)
        if validation_report is not None
        else validate_answers(form=form, answers=answers, chapter_text=chapter_body)
    )
    checked = validate_answers(form=form, answers=answers, chapter_text=chapter_body)
    rejected_paths = {item.path for item in [*report.rejected, *checked.rejected]}
    form_json, answers_json = canonical_json(form), canonical_json(answers)
    report_json = canonical_json(report)
    identity = body_sha256(
        canonical_json(
            {
                **base,
                "contracts": [c.model_dump() for c in contracts],
                "form": form_json,
                "answers": answers_json,
                "validation_report": report_json,
            }
        )
    )
    evidence = []
    for key, ask in asks.items():
        answer_index = next(
            i for i, item in enumerate(answers.obligations) if item.id == key
        )
        if any(
            path.startswith(f"obligations[{answer_index}].") for path in rejected_paths
        ):
            continue
        refs = fulfillment_refs(ask, answers_by_id[key], chapter_body)
        if refs:
            evidence.append(
                ObligationResolutionEvidence(
                    obligation_id=key,
                    contract_fingerprint=current[key].contract_fingerprint,
                    candidate_id=candidate_id,
                    draft_id=draft_id,
                    body_sha256=body_hash,
                    review_source=review_source,
                    review_identity=identity,
                    answer_json=canonical_json(answers_by_id[key]),
                    evidence_refs=refs,
                )
            )
    return ObligationResolutionPlan(
        **base,
        review_identity=identity,
        form_json=form_json,
        answers_json=answers_json,
        validation_report_json=report_json,
        evidence=tuple(evidence),
    )


def validate_resolution_plan(
    plan: ObligationResolutionPlan,
    *,
    obligations,
    project_id,
    chapter_number,
    candidate_id,
    draft_id,
    chapter_body,
):
    if (
        plan.project_id,
        plan.chapter_number,
        plan.candidate_id,
        plan.draft_id,
        plan.body_sha256,
    ) != (
        project_id,
        chapter_number,
        candidate_id,
        draft_id,
        body_sha256(chapter_body),
    ):
        raise ValueError("obligation evidence candidate/draft/body identity changed")
    rebuilt = build_resolution_plan(
        obligations=obligations,
        form=json.loads(plan.form_json) if plan.form_json else None,
        answers=json.loads(plan.answers_json) if plan.answers_json else None,
        project_id=project_id,
        chapter_number=chapter_number,
        candidate_id=candidate_id,
        draft_id=draft_id,
        chapter_body=chapter_body,
        review_source=plan.review_source,
        validation_report=json.loads(plan.validation_report_json)
        if plan.validation_report_json
        else None,
    )
    if rebuilt != plan:
        raise ValueError(
            "obligation contract or review evidence changed after preparation"
        )


def apply_resolution_plan(session, plan: ObligationResolutionPlan) -> list[str]:
    """Caller validates under its project/row locks and owns the Canon transaction."""
    from .repository import NarrativeObligationRepository

    repo = NarrativeObligationRepository(session)
    for evidence in plan.evidence:
        repo.mark_obligation_resolved(
            evidence.obligation_id,
            verifier_result={
                "source": evidence.review_source,
                "status": "pass",
                "evidence": evidence.model_dump(mode="json"),
            },
            evidence_refs=list(evidence.evidence_refs),
            resolution_chapter=plan.chapter_number,
        )
    return plan.resolved_obligation_ids


def context_obligations(session, project_id, chapter_number, *, draft_id: str = ""):
    from .repository import NarrativeObligationRepository

    repo = NarrativeObligationRepository(session)
    return [
        *repo.list_active_for_context(project_id, chapter_number=chapter_number),
        *[
            item
            for item in repo.list_planned_for_chapter(
                project_id, origin_chapter_number=chapter_number
            )
            if draft_id and item.origin_draft_id == draft_id
        ],
    ]
