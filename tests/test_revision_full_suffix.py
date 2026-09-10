"""Real preparation/projection/form/admission owners; only model transport is deterministic."""

import json

from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.canon.revision_service import (
    RevisionValidationService,
    revision_model_identity,
    save_revision_proposal,
)
from forwin.models.canon import CanonCommitRecord, CanonRevisionValidationRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic

prepared_canon = atomic.prepared_canon


class BodyModel:
    provider = "fixture"
    model = "whole-body-model"

    def __init__(self):
        self.calls = []
        self.llm_attempt_events = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.llm_attempt_events.append(
            {
                "provider": self.provider,
                "model": self.model,
                "stage_key": kwargs.get("stage_key", ""),
                "finish_reason": "stop",
                "http_status": 200,
            }
        )
        try:
            payload = json.loads(messages[-1]["content"])
        except (ValueError, KeyError):
            payload = {}
        if "form" in payload:
            form = payload["form"]
            body = payload["chapter_body"]
            assert (
                not form["characters"]
                and not form["countdowns"]
                and not form["obligations"]
                and not form["open_signals"]
            ), form
            return json.dumps(
                {
                    "answers": {
                        "project_id": form["project_id"],
                        "chapter_number": form["chapter_number"],
                        "form_schema_version": form["form_schema_version"],
                        "chapter_summary": "档案静候查阅。",
                        "characters": [],
                        "countdowns": [],
                        "obligations": [],
                        "open_signals": [],
                    },
                    "body_sha256": payload["body_sha256"],
                    "prefix_sha256": payload["prefix_sha256"],
                    "coverage_complete": True,
                    "coverage": [
                        {
                            "dimension": dimension,
                            "status": "pass",
                            "explanation": "完整正文仅描述档案环境，没有人物动作、信息改变或承诺变化，与完整前缀相符。",
                            "body_evidence": [
                                {"start": 0, "end": len(body), "quote": body}
                            ],
                            "prefix_refs": [f"/facts/{dimension}"],
                        }
                        for dimension in (
                            "possession",
                            "knowledge",
                            "life_state",
                            "time",
                            "place",
                            "obligations",
                        )
                    ],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "state_changes": [],
                "new_events": [],
                "delivered_payoffs": [],
                "thread_beats": [],
                "time_advance": None,
                "lore_candidates": [],
                "timeline_hints": [],
                "writer_notes": [],
                "entity_mentions": [],
                "end_of_chapter_summary": "档案静候查阅。",
            }
        )


def _book(fixture, *, origin_obligation=False):
    body = "\n".join(
        f"第{n}排档案安放在木架深处，纸页记载着不同年代的往事，字迹安静地留在原处。"
        for n in range(70)
    )
    if origin_obligation:
        body = "档案管理员承诺：下一章结束前查明封馆原因。\n" + body
    with fixture.Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="Quiet archive",
            premise="An archive remains still.",
            genre="thriller",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            automation_json=json.dumps({"primary_publish_platform": "qidian"}),
        )
        project.target_total_chapters = 10
        arc = updater.create_arc_plan(project.id, "Archive")
        first = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="Archive entrance",
            one_line="An unchanged archive",
            goals=[],
        )
        second = updater.create_chapter_plan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=2,
            title="Archive shelves",
            one_line="An unchanged archive",
            goals=[],
        )
        first_plan = atomic._prepare_chapter_candidate(
            session,
            project_id=project.id,
            chapter=first,
            version=1,
            body=body,
            summary="Archive",
            delta_id="archive-one",
            delta_summary="Archive",
            node_id="archive-event-one",
        )
        if origin_obligation:
            from forwin.models.narrative_obligation import NarrativeObligationRow

            origin = session.get(CandidateDraftRecord, first_plan.candidate_id)
            session.add(
                NarrativeObligationRow(
                    project_id=project.id,
                    origin_chapter_number=1,
                    origin_draft_id=origin.candidate_draft_id,
                    origin_review_id=origin.review_id,
                    status="planned",
                    obligation_type="future_payoff",
                    summary="下一章结束前查明封馆原因。",
                    deadline_chapter=2,
                    payoff_test="正文明确解释封馆原因",
                )
            )
        ids = (project.id, first.id, second.id)
    owner = CanonAdmissionService(session_factory=fixture.Session)
    first_commit = owner.commit_plan(first_plan)
    assert not first_commit.blocked, first_commit
    with fixture.Session.begin() as session:
        atomic._complete_post_canon_barrier(
            session,
            commit_id=first_commit.commit_id,
            project_id=ids[0],
            chapter_number=1,
            candidate_id=first_plan.candidate_id,
        )
        second = session.get(ChapterPlan, ids[2])
        second_plan = atomic._prepare_chapter_candidate(
            session,
            project_id=ids[0],
            chapter=second,
            version=1,
            body=body.replace("往事", "旧事"),
            summary="Shelves",
            delta_id="archive-two",
            delta_summary="Shelves",
            node_id="archive-event-two",
        )
    second_commit = owner.commit_plan(second_plan)
    assert not second_commit.blocked, second_commit
    return (
        ids,
        body,
        [first_commit.commit_id, second_commit.commit_id],
        second_plan.candidate_id,
    )


def test_wording_revision_reextracts_every_body_and_atomically_reaccepts_unchanged_successor(
    prepared_canon,
):
    fixture = prepared_canon
    ids, body, old_ids, successor_candidate = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
            expected_book_revision=2,
        )
        proposal_id = proposal.id
        old_history = {
            id: session.get(CanonCommitRecord, id).result_json for id in old_ids
        }
        old_successor_draft = session.get(
            CandidateDraftRecord, successor_candidate
        ).candidate_draft_id
    client = BodyModel()
    writer = ChapterWriter(client)
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=proposal_id)
    if prepared.blocked:
        with fixture.Session() as session:
            result = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
            assert False, result.result_json if result else prepared
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert not outcome.blocked, outcome
    assert len(client.calls) == 8, (
        "3 complete extraction calls + 1 extended form per chapter"
    )
    with fixture.Session() as session:
        assert session.get(Project, ids[0]).book_revision == 3
        new_ids = [session.get(ChapterPlan, id).active_commit_id for id in ids[1:]]
        assert all(new != old for new, old in zip(new_ids, old_ids))
        assert {
            id: session.get(CanonCommitRecord, id).result_json for id in old_ids
        } == old_history
        assert (
            session.get(CanonCommitRecord, new_ids[1]).candidate_id
            == successor_candidate
        )
        assert (
            session.get(CandidateDraftRecord, successor_candidate).candidate_draft_id
            == old_successor_draft
        )


import pytest


@pytest.mark.parametrize(
    "problem,expected",
    [
        ("key_missing", "fail"),
        ("unknown", "unknown"),
        ("model_fallback", "unknown"),
        ("truncated", "unknown"),
    ],
)
def test_suffix_contradiction_or_incomplete_model_coverage_preserves_mainline(
    prepared_canon, problem, expected
):
    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        proposal_id = proposal.id

    class ProblemModel(BodyModel):
        def chat(self, messages, **kwargs):
            result = super().chat(messages, **kwargs)
            if problem == "model_fallback":
                self.llm_attempt_events[-1]["model"] = "unexpected-model"
            if problem == "truncated":
                self.llm_attempt_events[-1]["finish_reason"] = "length"
            value = json.loads(result)
            if "coverage" in value and value["answers"]["chapter_number"] == 2:
                value["coverage"][0]["status"] = (
                    "fail" if problem == "key_missing" else "unknown"
                )
                value["coverage"][0]["explanation"] = (
                    "The successor requires a key removed by the replacement."
                    if problem == "key_missing"
                    else "Possession cannot be verified."
                )
            return json.dumps(value)

    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(ProblemModel()),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=proposal_id)
    assert prepared.blocked
    with fixture.Session() as session:
        evidence = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
        assert evidence.status == expected, evidence.result_json
        assert [
            session.get(ChapterPlan, id).active_commit_id for id in ids[1:]
        ] == old_ids
        assert session.get(Project, ids[0]).book_revision == 2


@pytest.mark.parametrize(
    "race", ["book_revision", "published", "body_changed", "policy_changed"]
)
def test_final_acceptance_rechecks_full_frozen_baseline(prepared_canon, race):
    from forwin.models.canon import CanonPublicationProtection

    fixture = prepared_canon
    ids, body, old_ids, successor = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    writer = ChapterWriter(BodyModel())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    with fixture.Session.begin() as session:
        project = session.get(Project, ids[0])
        if race == "book_revision":
            project.book_revision += 1
        if race == "policy_changed":
            project.setting_summary = "A changed setting."
        if race == "body_changed":
            candidate = session.get(CandidateDraftRecord, successor)
            session.get(
                ChapterDraft, candidate.candidate_draft_id
            ).body_text += " Changed."
        if race == "published":
            session.add(
                CanonPublicationProtection(
                    project_id=ids[0],
                    chapter_plan_id=ids[2],
                    chapter_number=2,
                    canon_commit_id=old_ids[1],
                    upload_job_id="concurrent-publication",
                    platform_id="fanqie",
                    content_sha256="remote-body",
                    state="published",
                )
            )
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert outcome.blocked
    with fixture.Session() as session:
        assert [
            session.get(ChapterPlan, id).active_commit_id for id in ids[1:]
        ] == old_ids
        assert (
            session.get(
                CanonRevisionValidationRecord, prepared.plan.revision_validation_id
            ).accepted_book_revision
            is None
        )


@pytest.mark.parametrize(
    "stage", ["book_state", "entity", "obligation", "outbox", "chapter"]
)
def test_revision_atomic_failure_restores_whole_acceptance_set(prepared_canon, stage):
    from forwin.canon.revision_replica import capture_revision
    from forwin.canon.revision_service import revision_policy_fingerprint

    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    writer = ChapterWriter(BodyModel())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared

    def source_rows():
        with fixture.Session() as session:
            snapshot = capture_revision(
                session,
                project_id=ids[0],
                candidate_id=candidate_id,
                model_identity=revision_model_identity(writer),
                policy_fingerprint=revision_policy_fingerprint(
                    session.get(Project, ids[0])
                ),
            )
            return {
                name: sorted(repr(sorted(row.items())) for row in rows)
                for name, rows in snapshot.tables.items()
            }

    before = source_rows()

    def fail(actual):
        if actual == stage:
            raise RuntimeError("injected failure")

    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan,
        revision_model_identity=revision_model_identity(writer),
        failure_injector=fail,
    )
    assert outcome.blocked
    with fixture.Session() as session:
        assert [
            session.get(ChapterPlan, id).active_commit_id for id in ids[1:]
        ] == old_ids
        assert (
            len(
                list(
                    session.scalars(
                        select(CanonCommitRecord).where(
                            CanonCommitRecord.project_id == ids[0]
                        )
                    )
                )
            )
            == 2
        )
        assert session.get(Project, ids[0]).book_revision == 2
    assert source_rows() == before


def test_existing_approve_owner_validates_real_non_active_proposal(prepared_canon):
    from forwin.generation.pipeline_core.acceptance import AcceptanceStage
    from forwin.state.repo import StateRepository

    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)

    class Pipeline(AcceptanceStage):
        _SessionFactory = fixture.Session
        writer = ChapterWriter(BodyModel())
        policy = RuntimePolicy.for_profile("standard")
        canon_admission = CanonAdmissionService(session_factory=fixture.Session)

        def _make_state_helpers(self, session):
            return StateRepository(session), StateUpdater(session), None

    pipeline = Pipeline()
    with pytest.raises(ValueError, match="distinct real revision proposal"):
        pipeline.accept_review(ids[0], 1)
    with fixture.Session.begin() as session:
        save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
    result = pipeline.accept_review(ids[0], 1)
    assert result["status"] == "accepted", result
    assert result.get("canon_commit_id"), result
    with fixture.Session() as session:
        assert [
            session.get(ChapterPlan, id).active_commit_id for id in ids[1:]
        ] != old_ids


@pytest.mark.parametrize("relevant", [True, False])
def test_final_identity_binds_before_images_but_ignores_unrelated_review_append(
    prepared_canon, relevant
):
    from forwin.models.audit import DecisionEvent
    from forwin.models.draft import ChapterReview

    fixture = prepared_canon
    ids, body, old_ids, successor = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    writer = ChapterWriter(BodyModel())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    with fixture.Session.begin() as session:
        if relevant:
            event = session.scalar(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == ids[0],
                    DecisionEvent.event_type == "canon_entity_before_image",
                )
            )
            data = json.loads(event.payload_json)
            data["version"] = 999
            event.payload_json = json.dumps(data)
        else:
            candidate = session.get(CandidateDraftRecord, successor)
            session.add(
                ChapterReview(
                    draft_id=candidate.candidate_draft_id,
                    verdict="warn",
                    review_meta_json='{"unrelated":true}',
                )
            )
            session.add(
                DecisionEvent(
                    project_id=ids[0],
                    event_type="unrelated_review_note",
                    summary="independent reviewer note",
                )
            )
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert outcome.blocked is relevant, outcome
    with fixture.Session() as session:
        pointers = [session.get(ChapterPlan, id).active_commit_id for id in ids[1:]]
        assert (pointers == old_ids) is relevant


def test_non_canon_edit_after_materialization_is_retained_and_revision_rejected(
    prepared_canon,
):
    from sqlalchemy import text

    from forwin.book_state.repository import BookStateRepository

    fixture = prepared_canon
    ids, body, old_ids, _ = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    writer = ChapterWriter(BodyModel())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    wrote = []

    def competing_world_edit(stage):
        if stage != "materialized":
            return
        with fixture.Session.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '200ms'"))
            repo = BookStateRepository(session)
            node = repo.get_world_node("archive-event-one")
            repo.create_world_node(
                node.model_copy(update={"summary": "Concurrent non-Canon world edit"})
            )
        wrote.append(True)

    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan,
        revision_model_identity=revision_model_identity(writer),
        failure_injector=competing_world_edit,
    )
    assert wrote == [True], "no write lock is held during candidate materialization"
    assert outcome.blocked and "baseline changed" in outcome.failure_reason, outcome
    with fixture.Session() as session:
        assert (
            BookStateRepository(session).get_world_node("archive-event-one").summary
            == "Concurrent non-Canon world edit"
        )
        assert [
            session.get(ChapterPlan, id).active_commit_id for id in ids[1:]
        ] == old_ids


@pytest.mark.parametrize("field", ["title", "genre"])
def test_final_fingerprint_binds_project_inputs_used_by_extraction_context(
    prepared_canon, field
):
    f = prepared_canon
    ids, body, _old_ids, _ = _book(f)
    with f.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("安静地", "静静地"),
        )
        candidate_id = proposal.id
    writer = ChapterWriter(BodyModel())
    prepared = RevisionValidationService(
        session_factory=f.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    with f.Session.begin() as session:
        setattr(
            session.get(Project, ids[0]),
            field,
            "Changed input after full body validation",
        )
    outcome = CanonAdmissionService(session_factory=f.Session).commit_plan(
        prepared.plan, revision_model_identity=revision_model_identity(writer)
    )
    assert outcome.blocked, (
        f"{field} used by ChapterContextAssembler changed but revision accepted"
    )
