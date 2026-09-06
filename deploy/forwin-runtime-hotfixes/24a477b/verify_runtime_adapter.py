from __future__ import annotations

import json
import inspect
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

import forwin.canon.review_recovery as recovery
import forwin.api
import forwin.cli
import forwin.mcp.http
from forwin.application.projects import reviews as review_routes
from forwin.candidate_drafts import CandidateTransitionError, candidate_body_hash
from forwin.generation.pipeline import ChapterPipeline
from forwin.generation.pipeline_core.acceptance import AcceptanceStage
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.naming.entity_registrar import writer_output_admission_fingerprint
from forwin.protocol.writer import WriterOutput


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


assert (
    sha256("/app/forwin/naming/entity_registrar.py")
    == "3176f884c4fd1b8ab5ce37c3022c76de227612938387450fb851db6929c25d1a"
)
assert (
    sha256("/app/forwin/protocol/writer.py")
    == "a67e88a636535734ab10839039cd9dcdd339adbe75714c2d47ce6b95935cb748"
)
assert (
    sha256("/app/forwin/models/draft.py")
    == "a4ead827008b57676fa4a2a0ba540ed54360a352737107658e724f784088382c"
)
assert hasattr(CandidateDraftRecord, "writer_artifact_ref")
assert hasattr(ChapterDraft, "llm_raw_response")


class ArtifactStoreProbe:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.paths: list[str] = []

    def read_json(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        return self.payload


def load_exact_compat_writer_output(payload: dict[str, object]) -> WriterOutput:
    pipeline = object.__new__(ChapterPipeline)
    store = ArtifactStoreProbe(payload)
    pipeline.artifact_store = store
    result = pipeline._load_writer_output_from_meta("artifacts/chapter-12.json")
    assert store.paths == ["artifacts/chapter-12.json"]
    return result


writer_output = WriterOutput(
    project_id="project-12",
    chapter_number=12,
    title="Compatibility recovery",
    body="immutable body",
    end_of_chapter_summary="immutable summary",
)
loaded = load_exact_compat_writer_output(writer_output.model_dump(mode="json"))
expected = writer_output_admission_fingerprint(loaded)
assert expected == writer_output_admission_fingerprint(writer_output)
accept_parameters = inspect.signature(AcceptanceStage.accept_review).parameters
assert accept_parameters["actor_type"].default == "compat_direct"
assert accept_parameters["source"].default == "compat_direct"
acceptance_source = inspect.getsource(AcceptanceStage.accept_review)
assert "artifact_path = latest_draft.llm_raw_response" in acceptance_source
assert "self._load_writer_output_from_meta(\n                artifact_path" in acceptance_source
assert "artifact_path=artifact_path" in acceptance_source
review_route_source = inspect.getsource(review_routes)
assert 'accept_kwargs["actor_type"] = "api"' in review_route_source
assert 'accept_kwargs["source"] = "chapter_review_approve_api"' in review_route_source


def candidate(*, metadata_json: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="candidate-12",
        project_id="project-12",
        chapter_plan_id="chapter-plan-12",
        chapter_number=12,
        candidate_draft_id="draft-12",
        review_id="review-12",
        version=12,
        status="failed",
        canon_status="candidate",
        canon_commit_id="",
        failure_reason="prior atomic write failed",
        idempotency_key="idempotency-12",
        body_hash=candidate_body_hash(writer_output.body),
        plan_revision="",
        policy_version=1,
        eligibility_decision_json="{}",
        canon_commit_plan_json="{}",
        review_result_json='{"verdict":"pass"}',
        metadata_json=metadata_json,
        writer_artifact_ref="artifacts/chapter-12.json",
        created_at=datetime(2026, 9, 6, 19, 0, tzinfo=timezone.utc),
    )


draft = SimpleNamespace(
    id="draft-12",
    body_text=writer_output.body,
    llm_raw_response="artifacts/chapter-12.json",
    version=12,
)
assert recovery._stable_writer_output_binds_recovery(
    candidate(metadata_json='{"title":"Compatibility recovery"}'),
    draft,
    loaded,
    project_id="project-12",
    chapter_number=12,
)
assert not recovery._stable_writer_output_binds_recovery(
    candidate(metadata_json='{"title":"wrong title"}'),
    draft,
    loaded,
    project_id="project-12",
    chapter_number=12,
)
assert not recovery._stable_writer_output_binds_recovery(
    candidate(metadata_json='{"title":"Compatibility recovery"}'),
    SimpleNamespace(id="draft-12", body_text="modified body", version=12),
    loaded,
    project_id="project-12",
    chapter_number=12,
)
assert not recovery._stable_writer_output_binds_recovery(
    candidate(metadata_json="{}"),
    draft,
    loaded,
    project_id="project-12",
    chapter_number=12,
)

matches, backfill = recovery._stable_recovery_fingerprint_state(
    candidate(metadata_json='{"title":"Compatibility recovery"}'),
    expected_writer_output_fingerprint=expected,
    expected_plan_fingerprint=expected,
)
assert matches
assert backfill == {
    "title": "Compatibility recovery",
    "writer_output_admission_fingerprint": expected,
}
matches, backfill = recovery._stable_recovery_fingerprint_state(
    candidate(
        metadata_json=(
            '{"writer_output_admission_fingerprint":"' + expected + '"}'
        )
    ),
    expected_writer_output_fingerprint=expected,
    expected_plan_fingerprint=expected,
)
assert matches and backfill is None
for bad_metadata in (
    '{"writer_output_admission_fingerprint":"different"}',
    '{"writer_output_admission_fingerprint":null}',
    '{"writer_output_admission_fingerprint":12}',
    "not-json",
):
    matches, backfill = recovery._stable_recovery_fingerprint_state(
        candidate(metadata_json=bad_metadata),
        expected_writer_output_fingerprint=expected,
        expected_plan_fingerprint=expected,
    )
    assert not matches and backfill is None
try:
    load_exact_compat_writer_output({"chapter_number": 12})
except ValidationError:
    pass
else:
    raise AssertionError("unparseable WriterOutput must fail closed")


def run_recovery(
    *,
    metadata_json: str,
    marker_time: datetime,
    candidate_artifact_ref: str = "artifacts/chapter-12.json",
    draft_artifact_ref: str = "artifacts/chapter-12.json",
    passed_artifact_ref: str = "artifacts/chapter-12.json",
    plan_mode: str = "normal",
    actor_type: str = "api",
    source: str = "chapter_review_approve_api",
    passed_writer_output: WriterOutput = loaded,
) -> tuple[object, object, Exception | None]:
    current_candidate = candidate(metadata_json=metadata_json)
    current_candidate.writer_artifact_ref = candidate_artifact_ref
    locked_draft = SimpleNamespace(
        id="draft-12",
        body_text=writer_output.body,
        llm_raw_response=draft_artifact_ref,
        version=12,
    )
    chapter = SimpleNamespace(
        id="chapter-plan-12",
        project_id="project-12",
        chapter_number=12,
        status="needs_review",
        arc_plan_id="arc-12",
        title="Compatibility recovery",
        one_line="",
        goals_json="[]",
        task_contract_json="[]",
        experience_plan_json="{}",
    )
    current_candidate.plan_revision = recovery.candidate_plan_revision(chapter)
    current_candidate.eligibility_decision_json = json.dumps(
        {
            "eligible": True,
            "candidate_id": current_candidate.id,
            "body_hash": current_candidate.body_hash,
            "plan_revision": current_candidate.plan_revision,
        }
    )
    project = SimpleNamespace(id="project-12", runtime_policy_version=1)
    review = SimpleNamespace(
        id="review-12", review_meta_json=current_candidate.review_result_json, verdict="pass"
    )
    plan = SimpleNamespace(
        project_id="project-12",
        chapter_number=12,
        candidate_id="candidate-12",
        acceptance_mode=plan_mode,
        idempotency_key="idempotency-12",
        candidate_body_hash=current_candidate.body_hash,
        plan_revision=current_candidate.plan_revision,
        policy_version=1,
        entity_admission_plan=SimpleNamespace(candidate_fingerprint=expected),
        outbox_events=(),
    )
    current_candidate.canon_commit_plan_json = json.dumps(
        {"acceptance_mode": plan_mode}
    )
    predecessor = SimpleNamespace(
        status="accepted",
        canon_commit_id="prior-commit-12",
        chapter_plan_id="chapter-plan-12",
        version=11,
    )
    replacement = SimpleNamespace(
        previous=SimpleNamespace(id="prior-commit-12", candidate_id="candidate-11"),
        marker=SimpleNamespace(created_at=marker_time),
    )

    class SessionProbe:
        def __init__(self) -> None:
            self.values = iter(
                [project, chapter, current_candidate, locked_draft, review, None]
            )
            self.flushes = 0
            self.scalar_calls = 0

        def scalar(self, _statement: object) -> object:
            self.scalar_calls += 1
            return next(self.values, None)

        def get(self, _model: object, candidate_id: str) -> object:
            assert candidate_id == "candidate-11"
            return predecessor

        def flush(self) -> None:
            self.flushes += 1

    class RepositoryProbe:
        def __init__(self, _session: object) -> None:
            pass

        def latest_for_chapter(self, **_kwargs: object) -> object:
            return current_candidate

    class PlanProbe:
        @classmethod
        def model_validate_json(cls, _payload: str) -> object:
            return plan

    class HistoricalServiceProbe:
        def __init__(self, _session: object) -> None:
            pass

        def prepare_replacement(self, received_plan: object, **kwargs: object) -> object:
            assert received_plan is plan
            assert kwargs == {"persist_marker_backfill": False}
            return replacement

    session = SessionProbe()
    originals = {
        "CandidateDraftRepository": recovery.CandidateDraftRepository,
        "CanonCommitPlan": recovery.CanonCommitPlan,
        "HistoricalCanonRewriteService": recovery.HistoricalCanonRewriteService,
        "any_outbox_events_exist": recovery.any_outbox_events_exist,
    }
    recovery.CandidateDraftRepository = RepositoryProbe
    recovery.CanonCommitPlan = PlanProbe
    recovery.HistoricalCanonRewriteService = HistoricalServiceProbe
    recovery.any_outbox_events_exist = lambda _session, **_kwargs: False
    failure: Exception | None = None
    try:
        recovery.reopen_failed_historical_candidate_for_review(
            session,
            project_id="project-12",
            chapter_number=12,
            candidate_id="candidate-12",
            draft_id="draft-12",
            review_id="review-12",
            actor_type=actor_type,
            source=source,
            writer_output=passed_writer_output,
            artifact_path=passed_artifact_ref,
        )
    except CandidateTransitionError as exc:
        failure = exc
    finally:
        for name, original in originals.items():
            setattr(recovery, name, original)
    return current_candidate, session, failure


recovered, recovered_session, recovered_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
)
assert recovered_failure is None
assert recovered.status == "needs_review" and recovered_session.flushes == 1
assert json.loads(recovered.metadata_json) == {
    "title": "Compatibility recovery",
    "writer_output_admission_fingerprint": expected,
}

manual, manual_session, manual_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
    actor_type="manual_ui",
)
assert isinstance(manual_failure, CandidateTransitionError)
assert manual.status == "failed" and manual_session.flushes == 0
assert manual_session.scalar_calls == 0

unrecognized, unrecognized_session, unrecognized_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
    plan_mode="gate_approved",
)
assert isinstance(unrecognized_failure, CandidateTransitionError)
assert unrecognized.status == "failed" and unrecognized_session.flushes == 0

original_metadata = '{"writer_output_admission_fingerprint":"different"}'
rejected, rejected_session, rejected_failure = run_recovery(
    metadata_json=original_metadata,
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
)
assert isinstance(rejected_failure, CandidateTransitionError)
assert rejected.status == "failed"
assert rejected.metadata_json == original_metadata
assert rejected_session.flushes == 0

late, late_session, late_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 19, 0, tzinfo=timezone.utc),
)
assert isinstance(late_failure, CandidateTransitionError)
assert late.status == "failed"
assert late.metadata_json == '{"title":"Compatibility recovery"}'
assert late_session.flushes == 0

artifact_mismatch, artifact_session, artifact_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
    candidate_artifact_ref="artifacts/other.json",
)
assert isinstance(artifact_failure, CandidateTransitionError)
assert artifact_mismatch.status == "failed"
assert artifact_mismatch.metadata_json == '{"title":"Compatibility recovery"}'
assert artifact_session.flushes == 0

changed_artifact, changed_artifact_session, changed_artifact_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
    passed_writer_output=loaded.model_copy(
        update={"prompt_revision_hash": "unreviewed-nonbody-field"}
    ),
)
assert isinstance(changed_artifact_failure, CandidateTransitionError)
assert changed_artifact.status == "failed"
assert changed_artifact.metadata_json == '{"title":"Compatibility recovery"}'
assert changed_artifact_session.flushes == 0

post_read_retarget, retarget_session, retarget_failure = run_recovery(
    metadata_json='{"title":"Compatibility recovery"}',
    marker_time=datetime(2026, 9, 6, 18, 59, tzinfo=timezone.utc),
    draft_artifact_ref="artifacts/post-read-retarget.json",
)
assert isinstance(retarget_failure, CandidateTransitionError)
assert post_read_retarget.status == "failed"
assert post_read_retarget.metadata_json == '{"title":"Compatibility recovery"}'
assert retarget_session.flushes == 0

print("compat_writer_output_fingerprint_adapter=ok")
