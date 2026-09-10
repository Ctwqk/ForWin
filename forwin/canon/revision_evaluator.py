"""Existing extraction, review, entity and BookState owners in candidate state."""

from __future__ import annotations

from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.extraction.contract import BookStateExtractionRequest
from forwin.book_state.extraction.graph_delta import BookStateGraphDeltaExtractor
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.canon.entity_admission import EntityAdmissionCommitter
from forwin.canon_quality.chapter_review_form.historical import (
    review_historical_chapter_with_form,
)
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.checker import ContinuityChecker
from forwin.context.assembler_core.assembler import ChapterContextAssembler
from forwin.models.project import ChapterPlan, Project
from forwin.naming.entity_registrar import EntityRegistrar, LLMEntityAdmissionClassifier
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.world_contracts import WorldContractRepository
from forwin.review.draft_service import DraftReviewService
from forwin.state.repo import StateRepository

from .revision_body import extract_revision_body
from .revision_validation import (
    RevisionChapterAssessment,
    RevisionCheck,
    revision_digest,
)


def historical_prefix_context(session, *, project_id: str, chapter_number: int):
    runtime = BookStateProjection(session).load_runtime_as_of(
        project_id, as_of_chapter=chapter_number - 1
    )
    world = runtime.world.snapshot()
    obligations = NarrativeObligationRepository(session).list_active_for_context(
        project_id, chapter_number=chapter_number
    )
    quality = CanonQualityRepository(session)
    signals = quality.list_open_signals(
        project_id, before_chapter=chapter_number, limit=None
    )
    characters = quality.list_character_transitions(
        project_id, before_chapter=chapter_number
    )
    latest_characters = aggregate_historical_characters(characters)
    countdowns = quality.list_countdown_entries(
        project_id, before_chapter=chapter_number, include_details=True
    )
    latest_countdowns = {row["countdown_key"]: row for row in countdowns}
    cognition = {}
    for key, view in runtime.cognition_by_observer.items():
        cognition[":".join(key)] = {
            name: sorted(getattr(view, name))
            for name in (
                "visible_refs",
                "hidden_refs",
                "suspected_refs",
                "confirmed_refs",
            )
        }
        cognition[":".join(key)].update(
            {
                "field_overrides": view.field_overrides,
                "false_facts": {
                    k: v.model_dump(mode="json") for k, v in view.false_facts.items()
                },
                "false_nodes": {
                    k: v.model_dump(mode="json") for k, v in view.false_nodes.items()
                },
                "false_edges": {
                    k: v.model_dump(mode="json") for k, v in view.false_edges.items()
                },
                "evidence_by_ref": view.evidence_by_ref,
            }
        )
    previous = BookStateRepository(session).latest_world_snapshot(
        project_id, chapter_number - 1
    )
    return {
        "project_id": project_id,
        "through_chapter": chapter_number - 1,
        "complete": True,
        "facts": {
            "possession": world,
            "life_state": world,
            "knowledge": cognition,
            "time": {
                "story_time": previous.as_of_story_time if previous else "",
                "world": world,
            },
            "place": {
                "nodes": {
                    k: v.model_dump(mode="json")
                    for k, v in runtime.map.nodes_by_id.items()
                },
                "edges": {
                    k: v.model_dump(mode="json")
                    for k, v in runtime.map.edges_by_id.items()
                },
                "world": world,
            },
            "obligations": {
                "rows": [o.model_dump(mode="json") for o in obligations],
                "narrative_nodes": {
                    k: v.model_dump(mode="json")
                    for k, v in runtime.narrative.nodes_by_id.items()
                },
                "narrative_edges": {
                    k: v.model_dump(mode="json")
                    for k, v in runtime.narrative.edges_by_id.items()
                },
            },
        },
        "character_rows": latest_characters,
        "countdown_rows": list(latest_countdowns.values()),
        "open_signal_rows": [s.model_dump(mode="json") for s in signals],
        "obligations": [o.model_dump(mode="json") for o in obligations],
    }


class HistoricalBodyEvaluator:
    def __init__(self, *, writer, policy):
        self.writer = writer
        self.policy = policy
        self.llm_client = writer.llm_client

    def evaluate(self, session, manifest, chapter):
        model_start = len(getattr(self.llm_client, "evidence", []))
        repo = StateRepository(session)
        plan = session.get(ChapterPlan, chapter.chapter_plan_id)
        context = ChapterContextAssembler().assemble(repo, manifest.project_id, plan)
        output = extract_revision_body(
            writer=self.writer, context=context, title=chapter.title, body=chapter.body
        )
        evidence = (f"body:{chapter.body_sha256}",)
        checks = [
            RevisionCheck(
                dimension="body_extraction",
                status="pass",
                evidence_refs=evidence,
                explanation="All three existing extraction parts inspected the complete frozen body without window fallback.",
            )
        ]
        entity = EntityRegistrar(
            session=session,
            classifier=LLMEntityAdmissionClassifier(
                self.llm_client, historical_full_input=True
            ),
        ).plan_writer_output(
            project_id=manifest.project_id,
            chapter_number=chapter.chapter_number,
            writer_output=output,
        )
        output = entity.writer_output
        conflicts = [
            decision
            for decision in entity.plan.decisions
            if decision.action == "plan_conflict"
        ]
        missing_reasons = (
            "classifier_error:",
            "entity registrar classifier unavailable",
            "classifier omitted this name",
        )
        entity_status = "pass"
        if entity.plan.blocked:
            entity_status = (
                "unknown"
                if conflicts
                and all(
                    decision.reason.startswith(missing_reasons)
                    for decision in conflicts
                )
                else "fail"
            )
        checks.append(
            RevisionCheck(
                dimension="entity_identity",
                status=entity_status,
                evidence_refs=evidence,
                explanation="Existing entity registrar: "
                + (
                    "; ".join(
                        f"{decision.mention_name}: {decision.reason}"
                        for decision in conflicts
                    )
                    or "identity constraints satisfied"
                ),
            )
        )
        if any(check.status != "pass" for check in checks):
            return self._assessment(chapter, checks)
        checker = ContinuityChecker(
            repo,
            min_chars=self.policy.chapter_length.min_chars,
            max_chars=self.policy.chapter_length.max_chars,
        )
        # The extended form below owns semantic review once. Reuse deterministic
        # review components here without starting another LLM/form reviewer.
        review = DraftReviewService(
            experience_review_enabled=False,
            canon_quality_review_in_hub_enabled=False,
            llm_enabled=False,
        ).review(
            project_id=manifest.project_id,
            repo=repo,
            context=context,
            writer_output=output,
            continuity_checker=checker,
        )
        checks.append(
            RevisionCheck(
                dimension="continuity",
                status="fail" if review.verdict == "fail" else "pass",
                evidence_refs=evidence,
                explanation="Existing continuity/plan/movement/personality reviewers: "
                + review.verdict,
            )
        )
        if any(check.status != "pass" for check in checks):
            return self._assessment(chapter, checks)
        prefix = historical_prefix_context(
            session,
            project_id=manifest.project_id,
            chapter_number=chapter.chapter_number,
        )
        form = review_historical_chapter_with_form(
            session=session,
            project_id=manifest.project_id,
            chapter_number=chapter.chapter_number,
            writer_output=output,
            llm_client=self.llm_client,
            prefix_context=prefix,
            draft_id=chapter.draft_id,
            target_total_chapters=session.get(
                Project, manifest.project_id
            ).target_total_chapters,
        )
        checks.extend(
            RevisionCheck.model_validate(check.model_dump()) for check in form.checks
        )
        # Coverage gaps are unknown, and a known contradiction remains fail.
        # Neither may be reclassified by a downstream gate or extraction error.
        if any(check.status != "pass" for check in checks):
            return self._assessment(chapter, checks)
        obligations = NarrativeObligationRepository(session).list_active_for_context(
            manifest.project_id, chapter_number=chapter.chapter_number
        )
        gate = evaluate_canon_admission(
            project_id=manifest.project_id,
            chapter_number=chapter.chapter_number,
            draft_id=chapter.draft_id,
            review_verdict=review.verdict,
            signals=form.review.signals,
            obligations=obligations,
            analyzer_results=form.review.raw_analyzer_results,
            mode="strict",
        )
        extraction = BookStateGraphDeltaExtractor(session=session).extract(
            BookStateExtractionRequest(
                project_id=manifest.project_id,
                chapter_number=chapter.chapter_number,
                writer_output=output,
                chapter_intent=WorldContractRepository(session).get_chapter_intent(
                    manifest.project_id, chapter.chapter_number
                ),
                review_verdict_id=f"revision-body-{chapter.body_sha256}",
            )
        )
        approved = None
        failure = ""
        if extraction.accepted and extraction.changes is not None:
            # Even identical prose receives context-bound fresh evidence IDs.
            nonce = revision_digest(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "chapter": chapter.chapter_number,
                }
            )
            changes = extraction.changes.model_copy(
                update={
                    "graph_deltas": [
                        delta.model_copy(
                            update={"id": f"revision-delta-{nonce}-{index}"}
                        )
                        for index, delta in enumerate(extraction.changes.graph_deltas)
                    ]
                }
            )
            checked = BookStateReviewGate(session).review(changes)
            approved = checked.approved_changes if checked.accepted else None
            failure = "; ".join(issue.message for issue in checked.issues)
        else:
            failure = "; ".join(issue.message for issue in extraction.issues)
        gate_failed = not gate.commit_allowed
        checks.append(
            RevisionCheck(
                dimension="book_state",
                status="pass" if approved is not None and not gate_failed else "fail",
                evidence_refs=evidence,
                explanation=failure
                or "Existing BookState review and strict Canon gate completed.",
            )
        )
        prepared = {}
        if all(check.status == "pass" for check in checks):
            compiled = BookStateCompiler(session).compile(
                approved, compiler_run_id=f"revision-validation-{nonce}"
            )
            if not compiled.committed:
                raise ValueError(
                    "fresh candidate projection failed: "
                    + "; ".join(compiled.blocked_reasons)
                )
            EntityAdmissionCommitter(session).apply(
                project_id=manifest.project_id, plan=entity.plan
            )
            quality = CanonQualityRepository(session)
            quality.activate_candidate_projection(
                project_id=manifest.project_id,
                chapter_number=chapter.chapter_number,
                draft_id=chapter.draft_id,
                body_sha256=chapter.body_sha256,
                historical_form=form,
                projection_id=nonce,
            )
            obligation_owner = NarrativeObligationRepository(session)
            obligation_owner.set_candidate_acceptance(
                project_id=manifest.project_id,
                chapter_number=chapter.chapter_number,
                acceptance_id=f"revision-validation-{nonce}",
                draft_id=chapter.draft_id,
            )
            obligation_owner.apply_reviewed_resolutions(
                project_id=manifest.project_id,
                chapter_number=chapter.chapter_number,
                chapter_body=chapter.body,
                historical_form=form,
            )
            obligation_owner.activate_planned_for_chapter(
                manifest.project_id,
                origin_chapter_number=chapter.chapter_number,
                acceptance_id=f"revision-validation-{nonce}",
                draft_id=chapter.draft_id,
            )
            session.flush()
            prepared = {
                "approved_changes": approved.model_dump(mode="json"),
                "entity_plan": entity.plan.model_dump(mode="json"),
                "writer_output": output.model_dump(mode="json", exclude={"body"}),
                "review": review.model_dump(mode="json"),
                "historical_form": form.model_dump(mode="json"),
                "model_calls": list(self.llm_client.evidence[model_start:]),
            }
        return self._assessment(chapter, checks, prepared)

    @staticmethod
    def _assessment(chapter, checks, prepared=None):
        return RevisionChapterAssessment(
            chapter_number=chapter.chapter_number,
            body_sha256=chapter.body_sha256,
            checks=tuple(checks),
            prepared_changes=prepared or {},
        )


def aggregate_historical_characters(rows):
    """Preserve each current state dimension when a character has many transitions."""
    result = {}
    for row in sorted(rows, key=lambda r: int(r.get("chapter_number", 0))):
        name = str(row.get("character_name") or row.get("name") or "")
        if not name:
            raise ValueError("historical character transition identity missing")
        item = result.setdefault(
            name,
            {
                "character_name": name,
                "chapter_number": 0,
                "payload": {},
                "life_state": "unknown",
                "custody_state": "unknown",
            },
        )
        item["chapter_number"] = max(
            item["chapter_number"], int(row.get("chapter_number", 0))
        )
        item["payload"].update(row.get("payload") or {})
        state = row.get("to_state")
        if state in {"alive", "wounded", "dead"}:
            item["life_state"] = state
        if state in {"free", "captured"}:
            item["custody_state"] = state
    return list(result.values())
