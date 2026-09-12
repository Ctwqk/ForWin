"""Persisted semantic review must survive fresh approval only at its original Canon."""
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from forwin.canon.admission import CanonAdmissionService
from forwin.config import InfrastructureConfig
from forwin.models.base import get_engine, get_session_factory
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import ChapterReview
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.proposals.structured_patch import proposal_to_graph_delta
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.retrieval.source_identity import CanonBaselineChanged, CanonReadBaseline
from forwin.review.context_builder import build_review_context_pack
from forwin.runtime.container import RuntimeContainer
from forwin.review.candidate import CandidateReviewRequest
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_world_edit_history_boundary import _proposal


@pytest.fixture
def review_case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline baseline test attempted HTTP")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    url = postgres_test_url("review-baseline")
    engine = get_engine(url)
    factory = get_session_factory(engine)
    policy = RuntimePolicy.for_profile("pulp")
    config = InfrastructureConfig(database_url=url, artifact_root=str(tmp_path),
                                  qdrant_url=":memory:", embedding_backend="hash", minimax_api_key="")
    pipeline = RuntimeContainer.from_config(config, policy=policy, role="generation_worker").build_chapter_pipeline()
    monkeypatch.setattr(pipeline, "_run_phase3_pass", lambda **kwargs: {})
    monkeypatch.setattr(pipeline, "_run_post_canon_order_controls", lambda **kwargs: None)
    with factory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(title="Archive", genre="mystery", premise="Archive entry is permitted", runtime_policy=policy, automation_json='{"primary_publish_platform":"qidian"}')
        arc = updater.create_arc_plan(project.id, "Archive")
        chapter = updater.create_chapter_plan(project_id=project.id, arc_plan_id=arc.id,
            chapter_number=1, title="Entry", one_line="The archive opened", goals=[])
        project_id, chapter_id = project.id, chapter.id
    case = SimpleNamespace(Session=factory, project_id=project_id, chapter_id=chapter_id,
                           pipeline=pipeline, revisions=[], after_review=None)
    class FrozenReviewer:
        def review(self, *, repo, context, **kwargs):
            pack = build_review_context_pack(repo=repo, context=context)
            assert pack.project_id == project_id
            case.revisions.append(CanonReadBaseline.capture(repo.session, project_id, as_of_chapter=0).book_revision)
            if case.after_review:
                case.after_review()
            return ReviewVerdict(verdict="pass")
    pipeline.candidate_review.draft_review = FrozenReviewer()
    yield case
    engine.dispose()


def _world_edit(case):
    proposal_id = _proposal(case, 0)
    with case.Session.begin() as session:
        proposal = session.get(KnowledgeEditProposalRow, proposal_id)
        outcome = CanonAdmissionService().commit_world_edit(session=session, project_id=case.project_id,
            proposal_id=proposal_id, approved_changes=ApprovedGraphDeltaSet(project_id=case.project_id,
                chapter_number=0, graph_deltas=[proposal_to_graph_delta(session, proposal)]), reason="Contradict old premise", trigger="test")
        assert outcome.compile_result.committed


def _evaluate(case, session):
    repo, updater, checker = case.pipeline._make_state_helpers(session)
    context = ChapterContextPack(project_id=case.project_id, chapter_number=1, project_title="Archive", premise="Entry", genre="mystery", setting_summary="Archive", chapter_plan_title="Entry", chapter_plan_one_line="Entry", chapter_goals=[],
        canon_read_baseline=CanonReadBaseline.capture(session, case.project_id, as_of_chapter=0))
    output = WriterOutput(project_id=case.project_id, chapter_number=1, title="Entry",
        body="The archive opened and the register was checked.", end_of_chapter_summary="The archive opened.")
    evaluation = case.pipeline.candidate_review.evaluate(CandidateReviewRequest(session=session, repo=repo,
        checker=checker, project_id=case.project_id, chapter_number=1, context=context, output=output))
    return updater, evaluation


def _persist(case):
    with case.Session.begin() as session:
        updater, evaluation = _evaluate(case, session)
        persisted = case.pipeline.candidate_review.persist(session=session, updater=updater,
            project_id=case.project_id, chapter_plan=session.get(ChapterPlan, case.chapter_id), evaluation=evaluation)
        return persisted.review_row.id


def test_fresh_approval_rejects_review_before_legal_world_edit(review_case):
    case = review_case
    _persist(case)
    _world_edit(case)
    result = case.pipeline.accept_review(case.project_id, 1)
    assert result["status"] == "needs_review", result
    with case.Session() as session:
        assert session.get(Project, case.project_id).book_revision == 1
        assert session.get(ChapterPlan, case.chapter_id).status != "accepted"
        assert session.scalar(select(func.count()).select_from(CanonCommitRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ChapterReview)) == 1
    assert case.revisions == [0]


def test_review_rejects_canon_change_during_semantic_work(review_case):
    case = review_case
    case.after_review = lambda: _world_edit(case)
    with case.Session() as session, pytest.raises(CanonBaselineChanged):
        _evaluate(case, session)
    assert case.revisions == [0]


def test_fresh_approval_accepts_current_review_through_real_admission(review_case):
    case = review_case
    _persist(case)
    result = case.pipeline.accept_review(case.project_id, 1)
    assert result["status"] in {"accepted", "maintenance_pending"}, result
    with case.Session() as session:
        assert session.get(ChapterPlan, case.chapter_id).status == "accepted"
        assert session.scalar(select(CanonCommitRecord)).base_book_revision == 0


@pytest.mark.parametrize("change", ["missing", "malformed", "project", "as_of", "revision"])
def test_fresh_approval_rejects_unknown_or_mutated_review_proof(review_case, change):
    import json

    case = review_case
    review_id = _persist(case)
    with case.Session.begin() as session:
        review = session.get(ChapterReview, review_id)
        metadata = json.loads(review.review_meta_json)
        if change == "missing":
            metadata.pop("canon_read_baseline", None)
        elif change == "malformed":
            metadata["canon_read_baseline"] = "not a baseline"
        else:
            key, value = {"project": ("project_id", "another-book"),
                          "as_of": ("as_of_chapter", 1), "revision": ("book_revision", 1)}[change]
            metadata["canon_read_baseline"][key] = value
        review.review_meta_json = json.dumps(metadata)
    assert case.pipeline.accept_review(case.project_id, 1)["status"] == "needs_review"
    with case.Session() as session:
        assert session.scalar(select(func.count()).select_from(CanonCommitRecord)) == 0
        # Diagnostic history remains available even without an acceptance certificate.
        assert session.get(ChapterReview, review_id).verdict == "pass"


def test_admission_rechecks_review_proof_after_preparation(review_case, monkeypatch):
    case = review_case
    review_id = _persist(case)
    commit = case.pipeline.canon_admission.commit_plan
    outcomes = []

    def mutate_then_commit(plan):
        assert plan.review_evidence.review_id == review_id
        with case.Session.begin() as session:
            session.get(ChapterReview, review_id).review_meta_json = "{}"
        outcome = commit(plan)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(case.pipeline.canon_admission, "commit_plan", mutate_then_commit)
    assert case.pipeline.accept_review(case.project_id, 1)["status"] == "needs_review"
    assert outcomes[0].stale
    with case.Session() as session:
        assert session.scalar(select(func.count()).select_from(CanonCommitRecord)) == 0


def test_persistence_does_not_recapture_newer_baseline(review_case):
    case = review_case
    with case.Session() as session:
        updater, evaluation = _evaluate(case, session)
        session.rollback()
        _world_edit(case)
        with pytest.raises(CanonBaselineChanged):
            case.pipeline.candidate_review.persist(session=session, updater=updater,
                project_id=case.project_id, chapter_plan=session.get(ChapterPlan, case.chapter_id),
                evaluation=evaluation)
    with case.Session() as session:
        assert session.scalar(select(func.count()).select_from(ChapterReview)) == 0


def test_fresh_approval_cannot_substitute_another_review_for_same_draft(review_case):
    case = review_case
    review_id = _persist(case)
    with case.Session.begin() as session:
        original = session.get(ChapterReview, review_id)
        session.add(ChapterReview(draft_id=original.draft_id, verdict="pass"))
    with pytest.raises(ValueError, match="review association"):
        case.pipeline.accept_review(case.project_id, 1)


def test_fresh_recovery_rejects_plan_without_original_review_evidence(review_case):
    from forwin.canon.plan import CanonCommitPlan
    from forwin.models.draft import CandidateDraftRecord

    case = review_case
    _persist(case)
    # Prepare via the real approval flow, intercepting only the dispatch so a
    # restarted admission owner can load the durable prepared plan itself.
    commit = case.pipeline.canon_admission.commit_plan
    captured = []
    def recover(plan):
        with case.Session.begin() as session:
            candidate = session.get(CandidateDraftRecord, plan.candidate_id)
            payload = __import__("json").loads(candidate.canon_commit_plan_json)
            payload.pop("review_evidence")
            candidate.canon_commit_plan_json = __import__("json").dumps(payload)
        with case.Session() as session:
            candidate = session.get(CandidateDraftRecord, plan.candidate_id)
            restored = CanonCommitPlan.model_validate_json(candidate.canon_commit_plan_json)
        outcome = commit(restored)
        captured.append(outcome)
        return outcome
    case.pipeline.canon_admission.commit_plan = recover
    assert case.pipeline.accept_review(case.project_id, 1)["status"] == "needs_review"
    assert captured[0].stale
    assert "review Canon evidence changed" in captured[0].blocked_path
