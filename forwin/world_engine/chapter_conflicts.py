"""Residual chapter-text boundary violations become chapter_extraction conflicts.

A violation the draft repair loop fixes never gets here: that loop is the automatic rewrite_chapter. What the
loop leaves, or what canon preparation's review gate finds, is stored once per chapter and boundary and routed:
critical conflicts go to a human, the rest to Codex when the policy delegates to it. Codex decides once per boundary;
its rewrite_chapter buys the chapter one more repair pass, and whatever is found after that pass goes to a human.

Conflict summaries name entities, never a fact's proposition; concealed entities get the neutral label.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.knowledge import FACT_REF_PREFIX, is_concealed
from forwin.book_state.narrative import OPEN_PROTECTION_UNTIL
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.protections import MILESTONE_KIND, DeltaBoundaryViolation
from forwin.book_state.query import BookStateQuery
from forwin.book_state.reviewer import BookStateReviewVerdict
from forwin.book_state.runtime import BookStateRuntime
from forwin.generation.gate_delegation import GateAuditWriter
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import ObserverType
from forwin.protocol.review import ReviewVerdict
from forwin.runtime.policy import RuntimePolicy
from forwin.world_view.builder import _subject_names
from forwin.writer.prompt_core.world_repair import READER_OBSERVER_LABEL
from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL, forbidden_topic_text

from .conflict_delegate import WorldConflictDelegate, build_conflict_context_snapshot
from .conflict_events import conflict_detected_event
from .conflict_repository import WorldConflictRepository
from .conflict_resolution import conflict_resolved_event
from .conflict_routing import CHAPTER_CONFLICT_OPTIONS, CHAPTER_CONFLICT_SOURCE, available_options, route_conflict
from .conflict_types import ConflictDecider, ConflictDecision, ConflictDraft


# The repair phase, with its own attempt budget, that rewrites a chapter for a decided rewrite_chapter.
WORLD_CONFLICT_REPAIR_PHASE = "world_conflict_repair"
# The proposed_change kind of a chapter conflict: the chapter, the broken boundary and the candidate that broke it.
CHAPTER_BOUNDARY_CHANGE = "chapter_boundary"
# Why an open chapter conflict closed on its own: a later candidate no longer breaks the boundary.
NO_LONGER_VIOLATED_RATIONALE = "A later candidate of the chapter no longer breaks this boundary."
# Which check found a chapter conflict's violation; only the same check can find that it is gone, since the draft
# review never sees a boundary only canon preparation's review gate reports.
DRAFT_REVIEW_DETECTION = "draft_review"
CANON_GATE_DETECTION = "canon_gate"
_CANDIDATE_REF_PREFIX = "candidate:"
_READER = ObserverType.READER.value
_READER_LABEL = "the reader"


def violations_from_verdict(verdict: ReviewVerdict) -> list[DeltaBoundaryViolation]:
    """The boundary violations a draft review reports, from world consistency's ``original_result["boundary"]``."""
    return [
        DeltaBoundaryViolation(**boundary) for issue in verdict.issues
        if isinstance(boundary := issue.original_result.get("boundary"), dict)
    ]


def violations_from_gate(gate_verdict: BookStateReviewVerdict) -> list[DeltaBoundaryViolation]:
    """The boundary violations a BookState review gate verdict reports, from its issues' ``detail``."""
    return [DeltaBoundaryViolation(**issue.detail) for issue in gate_verdict.issues if issue.detail]


def chapter_origin_key(violation: DeltaBoundaryViolation, *, chapter_number: int) -> str:
    """One key per chapter and boundary: the protection's node, or the milestone ref the chapter ends."""
    if violation.kind == MILESTONE_KIND:
        return f"{CHAPTER_CONFLICT_SOURCE}:{chapter_number}:milestone:{violation.ref}"
    return f"{CHAPTER_CONFLICT_SOURCE}:{chapter_number}:{violation.protection_node_id}"


def chapter_conflict_draft(
    v: DeltaBoundaryViolation,
    *,
    project_id: str,
    chapter_number: int,
    story_time: int,
    candidate_id: str = "",
    names: Mapping[str, str] | None = None,
    detected_by: str = DRAFT_REVIEW_DETECTION,
) -> ConflictDraft:
    """The conflict for a boundary chapter ``chapter_number``'s text still breaks, as ``detected_by`` found it.

    ``names`` maps the violation's entity refs to display names and its fact ref to the names of the fact's
    subjects; a ref it does not name gets the neutral label. The console shows them beside the boundary.
    """
    return ConflictDraft(
        project_id=project_id,
        source=CHAPTER_CONFLICT_SOURCE,
        origin_key=chapter_origin_key(v, chapter_number=chapter_number),
        detected_after_chapter=chapter_number - 1,
        story_time=story_time,
        involved_refs=[ref for ref in dict.fromkeys((v.ref, v.observer_ref, v.fact_ref)) if ref],
        violated="milestone" if v.kind == MILESTONE_KIND else "protection",
        impact_level=v.impact_level,
        summary=_summary(v, chapter_number=chapter_number, names=names or {}),
        proposed_change={
            "kind": CHAPTER_BOUNDARY_CHANGE, "chapter": chapter_number, "boundary": asdict(v),
            "candidate_id": candidate_id, "detected_by": detected_by, "names": dict(names or {}),
        },
        blocks_from_chapter=chapter_number,
    )


def boundary_names(runtime: BookStateRuntime, violation: DeltaBoundaryViolation) -> dict[str, str]:
    """Display names for a violation's refs: an entity by its name, or the neutral label when it is concealed or
    unknown; the fact by the names of the subjects a scene could show, never by its proposition."""
    names = {
        ref: _entity_name(runtime, ref)
        for ref in (violation.ref, violation.observer_ref) if ref and ref != _READER
    }
    fact = runtime.world.facts_by_id.get(violation.fact_ref.removeprefix(FACT_REF_PREFIX))
    if fact is not None:
        names[violation.fact_ref] = ", ".join(_subject_names(runtime, fact))
    return names


def boundary_fix_names(runtime: BookStateRuntime, violation: DeltaBoundaryViolation) -> dict[str, str]:
    """The names a writer-facing fix for the violation is written with: ``name`` for the protected entity,
    ``observer`` for who may not learn the fact, and ``topic`` for the fact as its subjects' names show it.

    Concealed entities get the neutral label; the fact's proposition never fills them.
    """
    fact = runtime.world.facts_by_id.get(violation.fact_ref.removeprefix(FACT_REF_PREFIX))
    observer = violation.observer_ref
    return {
        "name": _entity_name(runtime, violation.ref) if violation.ref else "",
        "observer": (READER_OBSERVER_LABEL if observer == _READER
                     else _entity_name(runtime, observer) if observer else ""),
        "topic": forbidden_topic_text(_subject_names(runtime, fact)) if fact is not None else "",
    }


def conflict_detected_by(row: WorldPlanConflictRow) -> str:
    """Which check found the chapter conflict's violation: DRAFT_REVIEW_DETECTION or CANON_GATE_DETECTION."""
    return str(json.loads(row.proposed_change_json or "{}").get("detected_by") or "")


def _entity_name(runtime: BookStateRuntime, ref: str) -> str:
    node = runtime.world.get_node(ref)
    if node is not None:
        return UNNAMED_ACTOR_LABEL if is_concealed(runtime.world, node) else node.name.strip()
    place = runtime.map.nodes_by_id.get(ref)
    return place.name.strip() if place is not None else ""


def _summary(v: DeltaBoundaryViolation, *, chapter_number: int, names: Mapping[str, str]) -> str:
    def name(ref: str) -> str:
        return _READER_LABEL if ref == _READER else names.get(ref) or UNNAMED_ACTOR_LABEL

    chapter = f"Chapter {chapter_number}"
    until = f" through chapter {v.until_chapter}" if v.until_chapter != OPEN_PROTECTION_UNTIL else ""
    if v.kind == MILESTONE_KIND:
        return f"{chapter} ends {name(v.ref)}; a later milestone of the plan still needs them."
    if v.kind == "must_not_know":
        subjects = names.get(v.fact_ref)
        secret = f"the secret about {subjects}" if subjects else "an unrevealed secret"
        return f"{chapter} lets {name(v.observer_ref)} learn {secret}; the plan keeps it from them{until}."
    if v.kind == "must_be_alive":
        return f"{chapter} kills {name(v.ref)}; the plan keeps them alive{until}."
    if v.kind == "must_hold_state":
        return f"{chapter} changes {v.field} of {name(v.ref)}; the plan holds it{until}."
    return f"{chapter} ends {name(v.ref)}; the plan keeps them in existence{until}."


@dataclass(frozen=True)
class ChapterConflictOutcome:
    # The conflicts of the violations recorded, one per boundary.
    conflict_ids: tuple[str, ...]
    # An open conflict a human decides; it blocks the chapter. "" when none.
    blocking_conflict_id: str
    # The conflicts Codex decided rewrite_chapter for: the chapter gets one more repair pass.
    codex_rewrite_ids: tuple[str, ...]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChapterConflictRecorder:
    """Stores a candidate's residual boundary violations as chapter conflicts and lets Codex decide its share.

    The chapter stage holds the generation lease, so the open row of a boundary is the only one to reuse.
    """

    def __init__(
        self,
        session: Session,
        *,
        policy: RuntimePolicy,
        delegate: WorldConflictDelegate,
        audit_writer_factory: Callable[[], GateAuditWriter],
    ) -> None:
        self.session = session
        self.policy = policy
        self.delegate = delegate
        self.audit_writer_factory = audit_writer_factory
        self.repo = WorldConflictRepository(session)

    def record(
        self,
        *,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
        violations: Sequence[DeltaBoundaryViolation],
        detected_by: str = DRAFT_REVIEW_DETECTION,
        allow_codex: bool = True,
    ) -> ChapterConflictOutcome:
        """Record ``candidate_id``'s violations of chapter ``chapter_number`` that ``detected_by`` found.

        Each boundary reuses the open conflict this check found earlier, or gets a new one with its DETECTED event:
        a conflict the other check found stays that check's to close. Open conflicts the same
        check found earlier, whose boundary the candidate no longer breaks, are resolved as an automatic
        rewrite_chapter. Without ``allow_codex`` every conflict goes to a human. Codex reviews each conflict routed
        to it after a commit, so no conflict row stays locked across the model call; the caller commits the rest.
        """
        drafts = self._drafts(project_id, chapter_number, candidate_id, violations, detected_by=detected_by)
        rows = [
            self._open_or_create(draft, chapter_number=chapter_number, allow_codex=allow_codex) for draft in drafts
        ]
        self._resolve_no_longer_violated(
            project_id, chapter_number, candidate_id, kept={draft.origin_key for draft in drafts},
            detected_by=detected_by,
        )
        for_codex = [row.id for row in rows if row.status == "open" and row.required_decider == "codex"]
        codex_rewrite_ids = tuple(
            row_id for row_id in for_codex if self._codex_rewrites(project_id, row_id, chapter_number=chapter_number)
        )
        return ChapterConflictOutcome(
            conflict_ids=tuple(row.id for row in rows),
            blocking_conflict_id=next(
                (row.id for row in rows if row.status == "open" and row.required_decider == "human"), "",
            ),
            codex_rewrite_ids=codex_rewrite_ids,
        )

    def _drafts(
        self, project_id: str, chapter_number: int, candidate_id: str, violations: Sequence[DeltaBoundaryViolation],
        *, detected_by: str,
    ) -> list[ConflictDraft]:
        if not violations:
            return []
        as_of = chapter_number - 1
        runtime = BookStateProjection(self.session).load_runtime_as_of(project_id, as_of_chapter=as_of)
        story_time = BookStateQuery(self.session).current_story_minute(project_id, as_of_chapter=as_of) or 0
        drafts: dict[str, ConflictDraft] = {}
        for violation in violations:
            draft = chapter_conflict_draft(
                violation, project_id=project_id, chapter_number=chapter_number, story_time=story_time,
                candidate_id=candidate_id, names=boundary_names(runtime, violation), detected_by=detected_by,
            )
            drafts.setdefault(draft.origin_key, draft)
        return list(drafts.values())

    def _open_or_create(
        self, draft: ConflictDraft, *, chapter_number: int, allow_codex: bool,
    ) -> WorldPlanConflictRow:
        # Only the check that found a row may find its boundary gone, so a row is reused by that check alone.
        found_by = draft.proposed_change["detected_by"]
        row = next((row for row in self.repo.open_by_origin(draft.project_id, [draft.origin_key])
                    if conflict_detected_by(row) == found_by), None)
        if row is not None:
            if not allow_codex and row.required_decider == "codex":
                self.repo.escalate(row)
            return row
        decider: ConflictDecider = (
            "human" if not allow_codex or self._codex_decided(draft.project_id, draft.origin_key)
            else route_conflict(draft, policy=self.policy)
        )
        row = self.repo.create(draft, required_decider=decider, options=available_options(draft))
        self.session.add(conflict_detected_event(row, chapter_number=chapter_number))
        return row

    def _codex_decided(self, project_id: str, origin_key: str) -> bool:
        """Whether Codex already decided a conflict of this boundary: it decides once per boundary."""
        decisions = self.session.scalars(select(WorldPlanConflictRow.decision_json).where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.origin_key == origin_key,
            WorldPlanConflictRow.status == "resolved",
        ))
        return any(json.loads(decision or "{}").get("decided_by") == "codex" for decision in decisions)

    def _resolve_no_longer_violated(
        self, project_id: str, chapter_number: int, candidate_id: str, *, kept: set[str], detected_by: str,
    ) -> None:
        rows = self.session.scalars(
            select(WorldPlanConflictRow).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.source == CHAPTER_CONFLICT_SOURCE,
                WorldPlanConflictRow.blocks_from_chapter == chapter_number,
                WorldPlanConflictRow.status == "open",
            ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        for row in rows:
            if row.origin_key in kept or conflict_detected_by(row) != detected_by:
                continue
            self.repo.mark_resolved(row, decision=ConflictDecision(
                option="rewrite_chapter", decided_by="auto", rationale=NO_LONGER_VIOLATED_RATIONALE,
                decided_at=_utcnow(),
            ), result_refs=[f"{_CANDIDATE_REF_PREFIX}{candidate_id}"])
            self.session.add(conflict_resolved_event(row, chapter_number=chapter_number, extra={
                "candidate_id": candidate_id,
            }))

    def _codex_rewrites(self, project_id: str, conflict_id: str, *, chapter_number: int) -> bool:
        """Codex's decision on one conflict: rewrite_chapter resolves it, anything else hands it to a human.

        Returns whether Codex chose rewrite_chapter.
        """
        self.session.commit()
        row = self.repo.get(project_id, conflict_id)
        if row is None or row.status != "open":
            return False
        delegation = self.delegate.decide(
            conflict=row,
            context_snapshot=build_conflict_context_snapshot(self.session, conflict=row),
            policy=self.policy,
            audit_writer=self.audit_writer_factory(),
        )
        row = self.repo.get(project_id, conflict_id, for_update=True)
        if row is None or row.status != "open":
            return False
        if delegation.escalate or delegation.option is None:
            # The delegate recorded the escalation; the conflict only changes hands.
            self.repo.escalate(row)
            return False
        self.repo.mark_resolved(row, decision=ConflictDecision(
            option=delegation.option, decided_by="codex", rationale=delegation.rationale, decided_at=_utcnow(),
        ), result_refs=[delegation.trace_id] if delegation.trace_id else [])
        self.session.add(conflict_resolved_event(row, chapter_number=chapter_number, extra={
            "trace_id": delegation.trace_id,
        }))
        return delegation.option == "rewrite_chapter"


__all__ = [
    "CHAPTER_BOUNDARY_CHANGE",
    "CHAPTER_CONFLICT_OPTIONS",
    "CHAPTER_CONFLICT_SOURCE",
    "CANON_GATE_DETECTION",
    "DRAFT_REVIEW_DETECTION",
    "NO_LONGER_VIOLATED_RATIONALE",
    "WORLD_CONFLICT_REPAIR_PHASE",
    "ChapterConflictOutcome",
    "ChapterConflictRecorder",
    "boundary_fix_names",
    "boundary_names",
    "chapter_conflict_draft",
    "chapter_origin_key",
    "conflict_detected_by",
    "violations_from_gate",
    "violations_from_verdict",
]
