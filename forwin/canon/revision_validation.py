"""Frozen identity and complete coverage for a finite historical revision.

This module never accepts Canon. Its results are evidence for the existing
CanonAdmissionService, which must recheck the baseline in its own transaction.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

RevisionStatus = Literal["pass", "fail", "unknown"]
REQUIRED_REVISION_CHECKS = (
    "body_extraction",
    "continuity",
    "entity_identity",
    "book_state",
    "possession",
    "knowledge",
    "life_state",
    "time",
    "place",
    "obligations",
)
_MODEL_IDENTITY_FIELDS = {
    "provider",
    "model",
    "model_revision",
    "temperature",
    "max_tokens",
    "prompt_revision",
    "extractor_revision",
    "allowed_routes",
    "route_fingerprint",
}


def revision_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class FrozenRevisionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RevisionChapterInput(FrozenRevisionModel):
    chapter_plan_id: str
    chapter_number: int
    base_commit_id: str
    candidate_id: str
    draft_id: str
    title: str
    body: str
    body_sha256: str

    @model_validator(mode="after")
    def verified_body(self) -> Self:
        if hashlib.sha256(self.body.encode()).hexdigest() != self.body_sha256:
            raise ValueError("revision body hash mismatch")
        return self


class RevisionManifest(FrozenRevisionModel):
    project_id: str
    base_book_revision: int
    from_chapter: int
    through_chapter: int
    policy_fingerprint: str
    model_identity: dict[str, str | int | float]
    chapters: tuple[RevisionChapterInput, ...]
    baseline_identity: tuple[dict, ...] = ()
    source_state_fingerprint: str = ""
    policy_version: int = 1
    publisher_bindings: tuple[dict, ...] = ()

    @model_validator(mode="after")
    def complete_identity(self) -> Self:
        if self.from_chapter < 1 or self.through_chapter < self.from_chapter:
            raise ValueError("revision requires a complete positive range")
        if [c.chapter_number for c in self.chapters] != list(
            range(self.from_chapter, self.through_chapter + 1)
        ):
            raise ValueError("revision requires a complete contiguous chapter range")
        for name in ("chapter_plan_id", "base_commit_id", "draft_id"):
            if len({getattr(c, name) for c in self.chapters}) != len(self.chapters):
                raise ValueError(f"duplicate revision identity: {name}")
        if (
            set(self.model_identity) - _MODEL_IDENTITY_FIELDS
            or not self.model_identity.get("model")
            or not self.model_identity.get("provider")
            or any(
                isinstance(value, (dict, list))
                for value in self.model_identity.values()
            )
        ):
            raise ValueError(
                "model identity must omit credentials and contain provider/model"
            )
        return self


class RevisionCheck(FrozenRevisionModel):
    dimension: str
    status: RevisionStatus
    evidence_refs: tuple[str, ...] = ()
    explanation: str = ""


class RevisionChapterAssessment(FrozenRevisionModel):
    chapter_number: int
    body_sha256: str
    checks: tuple[RevisionCheck, ...] = ()
    # Fresh plans are stored with durable evidence; old delta manifests are not inputs.
    prepared_changes: dict = Field(default_factory=dict)
    model_calls: tuple[dict, ...] = ()


class RevisionValidationResult(FrozenRevisionModel):
    validation_id: str
    manifest: RevisionManifest
    status: RevisionStatus
    chapters: tuple[RevisionChapterAssessment, ...]
    uncovered: tuple[str, ...] = ()


def assess_revision(
    manifest: RevisionManifest,
    chapters: tuple[RevisionChapterAssessment, ...],
    *,
    uncovered: tuple[str, ...] = (),
) -> RevisionValidationResult:
    """Missing range, unknown checks and ungrounded passes cannot earn acceptance."""
    missing = list(uncovered)
    known_failure = False
    expected = {c.chapter_number: c for c in manifest.chapters}
    received: dict[int, RevisionChapterAssessment] = {}
    for assessment in chapters:
        number = assessment.chapter_number
        if number in received or number not in expected:
            missing.append(f"chapter:{number}:unexpected-or-duplicate")
        received[number] = assessment
    for number, source in expected.items():
        assessment = received.get(number)
        if assessment is None:
            missing.append(f"chapter:{number}:not-validated")
            continue
        if assessment.body_sha256 != source.body_sha256:
            missing.append(f"chapter:{number}:body-identity")
        checks: dict[str, RevisionCheck] = {}
        for check in assessment.checks:
            if check.dimension in checks:
                missing.append(f"chapter:{number}:{check.dimension}:duplicate")
            checks[check.dimension] = check
            known_failure |= check.status == "fail"
        for dimension in REQUIRED_REVISION_CHECKS:
            check = checks.get(dimension)
            if check is None or check.status == "unknown":
                missing.append(f"chapter:{number}:{dimension}:not-covered")
            elif check.status == "pass" and (
                not check.explanation.strip()
                or not any(
                    ref.startswith(f"body:{source.body_sha256}")
                    for ref in check.evidence_refs
                )
            ):
                missing.append(f"chapter:{number}:{dimension}:missing-body-evidence")
    status: RevisionStatus = (
        "fail" if known_failure else "unknown" if missing else "pass"
    )
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "chapters": [c.model_dump(mode="json") for c in chapters],
        "uncovered": sorted(missing),
        "status": status,
    }
    return RevisionValidationResult(
        validation_id="revision-" + revision_digest(payload),
        manifest=manifest,
        status=status,
        chapters=chapters,
        uncovered=tuple(sorted(missing)),
    )


def validate_chapter_sequence(
    manifest: RevisionManifest,
    *,
    evaluate: Callable[[RevisionChapterInput], RevisionChapterAssessment],
    budget_seconds: float = 1800,
) -> RevisionValidationResult:
    """Every frozen body is evaluated in order; interruption retains the full range."""
    started = time.monotonic()
    chapters = []
    uncovered = []
    for chapter in manifest.chapters:
        if time.monotonic() - started >= budget_seconds:
            uncovered.append(
                f"chapter:{chapter.chapter_number}:validation-budget-exhausted"
            )
            break
        try:
            assessment = evaluate(chapter)
        except Exception as exc:  # noqa: BLE001 — owner boundary preserves evidence and rejects incomplete validation.
            # Model/service failures are missing evidence, never a successful check.
            uncovered.append(f"chapter:{chapter.chapter_number}:{type(exc).__name__}")
            break
        chapters.append(assessment)
        if any(check.status != "pass" for check in assessment.checks):
            break
    if time.monotonic() - started >= budget_seconds:
        uncovered.append("validation-budget-exhausted")
    return assess_revision(manifest, tuple(chapters), uncovered=tuple(uncovered))
