"""Finite offline evidence: real owners, frozen model responses/publication facts.

Review approval and remote publication are declared fixtures. No external model,
publication, readership, or causal benefit is claimed by this replay.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forwin.audience.body_observation import FeedbackBodyObservationService
from forwin.candidate_drafts import CandidateDraftRepository, candidate_plan_revision
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.preparation import CanonPreparationService
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.models.publisher import (
    FeedbackActionRecord,
    PublisherChapterBinding,
    PublisherRawComment,
    PublisherWorkBinding,
)
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.observability.pipeline_trace import (
    PipelineAuditContext,
    PipelineTraceRecorder,
)
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_canon_atomic_transaction as atomic
from tests.test_feedback_consumer_transaction import _service
from tests.test_publisher_runtime_comment_sync import _runtime

# Independent body label: this literal sentence states the cost; the reviewer
# transport never supplies the BODY observation label.
RULE = "门卫解释：只有把铜钥匙交还给他，石门才会打开；带走钥匙的人无法离开。"
BODY = (
    RULE
    + "\n"
    + "她停在门前，重新掂量手中的铜钥匙，沿着石缝查看铰链，最后把钥匙放回桌上。" * 9
)


class FrozenWriterModel:
    def __init__(self):
        self.inputs = []

    def chat(self, messages, **kwargs):
        self.inputs.append(messages)
        return f"<<FORWIN_TITLE>>\n归还钥匙\n<<FORWIN_BODY>>\n{BODY}\n<<FORWIN_SUMMARY>>\n归还钥匙后开门。"


class FrozenCommentModel:
    def __init__(self, *, confidence=0.95, conflict=False):
        self.confidence = confidence
        self.conflict = conflict
        self.calls = []

    def chat(self, messages, **kwargs):
        comments = json.loads(messages[-1]["content"].split("评论列表：", 1)[1])
        self.calls.append(comments)
        return json.dumps(
            {
                "signals": [
                    {
                        "comment_index": c["comment_index"],
                        "signal_type": "pacing" if self.conflict else "risk",
                        "direction": ("too_fast" if "太快" in c["body"] else "too_slow")
                        if self.conflict
                        else "concern",
                        "target_type": "plot",
                        "target_name": "规则",
                        "severity": 3,
                        "confidence": self.confidence,
                        "evidence_span": c["body"],
                    }
                    for c in comments
                    if "担忧" in c["body"] or self.conflict
                ]
            },
            ensure_ascii=False,
        )


def _accept(runtime, project_id, chapter_id, output):
    with runtime.session_factory.begin() as session:
        chapter = session.get(ChapterPlan, chapter_id)
        updater = StateUpdater(session)
        recorder = PipelineTraceRecorder(
            audit=PipelineAuditContext(task_id="offline-feedback-fixture"),
            artifact_store=SimpleNamespace(
                save_observability_diagnostic=lambda **kwargs: {}
            ),
            observability=SimpleNamespace(),
        )
        trace_id = recorder.save_prompt_trace(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=output.generation_meta.get("prompt_trace"),
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id,
            version=1,
            body_text=output.body,
            summary=output.end_of_chapter_summary,
            char_count=len(output.body),
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass","source":"frozen approved test evidence"}',
        )
        session.add(review)
        session.flush()
        candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=project_id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision=candidate_plan_revision(chapter),
            policy_version=1,
        )
        prepared = CanonPreparationService().prepare_from_approved(
            session=session,
            candidate_id=candidate.id,
            approved_book_state_changes=ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=chapter.chapter_number,
                graph_deltas=[
                    GraphDelta(
                        id=f"loop-delta-{chapter.id}",
                        project_id=project_id,
                        chapter_number=chapter.chapter_number,
                        summary="Frozen approved no-state-change fixture",
                    )
                ],
                approved_by=["frozen_review_fixture"],
                review_verdict_id=review.id,
            ),
            entity_admission_plan=EntityAdmissionPlan(
                project_id=project_id,
                chapter_number=chapter.chapter_number,
                candidate_fingerprint=writer_output_admission_fingerprint(output),
            ),
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
        )
        assert prepared.plan is not None, prepared
    result = CanonAdmissionService(session_factory=runtime.session_factory).commit_plan(
        prepared.plan
    )
    assert not result.blocked, result
    with runtime.session_factory.begin() as session:
        atomic._complete_post_canon_barrier(
            session,
            commit_id=result.commit_id,
            project_id=project_id,
            chapter_number=prepared.plan.chapter_number,
            candidate_id=prepared.plan.candidate_id,
        )
    return result.commit_id, trace_id


def _publication_fixture(runtime, project_id, commit_id):
    """Install a frozen known-public fact in test DB; no remote action runs."""
    with runtime.session_factory.begin() as session:
        commit = session.get(CanonCommitRecord, commit_id)
        candidate = session.get(CandidateDraftRecord, commit.candidate_id)
        work = session.scalar(
            select(PublisherWorkBinding).where(
                PublisherWorkBinding.project_id == project_id
            )
        )
        if work is None:
            work = PublisherWorkBinding(
                project_id=project_id,
                platform_id="qidian",
                remote_book_id="offline-book",
            )
            session.add(work)
            session.flush()
        remote_id = f"offline-chapter-{commit.chapter_number}"
        session.add(
            PublisherChapterBinding(
                project_id=project_id,
                platform_id="qidian",
                work_binding_id=work.id,
                chapter_number=commit.chapter_number,
                remote_chapter_id=remote_id,
            )
        )
        session.add(
            CanonPublicationProtection(
                project_id=project_id,
                chapter_plan_id=commit.chapter_plan_id,
                chapter_number=commit.chapter_number,
                canon_commit_id=commit.id,
                upload_job_id=f"fixture-job-{commit.id}",
                platform_id="qidian",
                content_sha256=candidate.body_hash,
                state="published",
                remote_book_id="offline-book",
                remote_chapter_id=remote_id,
            )
        )
    return remote_id


def _ingest(runtime, project_id, remote_id, bodies, *, prefix, single_author=False):
    # Canon timestamps come from PostgreSQL. Give this simulated platform and
    # ingestion the same clock, independent of host/VM millisecond clock skew.
    # Real late/pre-body feedback remains rejected by the unchanged effect owner.
    with runtime.session_factory() as session:
        now = session.scalar(select(func.clock_timestamp()))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("forwin.publisher_runtime.comment_sync.utc_now", lambda: now)
        runtime.comment_sync.ingest_comments_batch(
            client_id="offline-fixture",
            platform="qidian",
            comments=[
                {
                    "project_id": project_id,
                    "work_id": "offline-book",
                    "chapter_id": remote_id,
                    "remote_comment_id": f"{prefix}-{i}",
                    "author_id": "same-reader" if single_author else f"{prefix}-reader-{i}",
                    "body": body,
                    "created_at": now.isoformat(),
                    "observed_at": now.isoformat(),
                }
                for i, body in enumerate(bodies)
            ],
        )


@pytest.fixture
def finite_book():
    engine, runtime = _runtime("feedback-finite-loop")
    with runtime.session_factory.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="有限反馈链",
            premise="根设定保持不变",
            genre="玄幻",
            runtime_policy=RuntimePolicy.for_profile("standard"),
            automation_json='{"primary_publish_platform":"qidian"}',
        )
        project.target_total_chapters = 100
        arc = updater.create_arc_plan(project.id, "固定卷计划")
        chapter_ids = []
        for number in range(1, 7):
            chapter = updater.create_chapter_plan(
                project.id, arc.id, number, f"第{number}章", "固定目标", ["保留目标"]
            )
            chapter.experience_plan_json = ChapterExperiencePlan(
                rule_anchors=["旧规则"]
            ).model_dump_json()
            chapter_ids.append(chapter.id)
        project_id = project.id
    commits = []
    for number in (1, 2):
        output = WriterOutput(
            project_id=project_id,
            chapter_number=number,
            title=f"第{number}章",
            body="她来到石门前，握着钥匙观察锁孔。",
            char_count=19,
            end_of_chapter_summary="来到石门。",
        )
        commit_id, _ = _accept(runtime, project_id, chapter_ids[number - 1], output)
        commits.append(commit_id)
    remote = _publication_fixture(runtime, project_id, commits[-1])
    yield SimpleNamespace(
        runtime=runtime,
        project_id=project_id,
        chapter_ids=chapter_ids,
        commits=commits,
        remote=remote,
    )
    engine.dispose()


def test_finite_loop_real_writer_and_sole_canon_owner_then_grounded_association(
    finite_book,
):
    c = finite_book
    model = FrozenCommentModel()
    _ingest(
        c.runtime, c.project_id, c.remote, ["担忧规则没有说明代价"] * 3, prefix="before"
    )
    with c.runtime.session_factory.begin() as session:
        first = _service(c.runtime, model)._run_feedback_step(
            session, session.get(CanonCommitRecord, c.commits[-1])
        )
        assert first["plan_applications"][0]["status"] == "applied"
    with c.runtime.session_factory() as session:
        action = session.scalars(
            select(FeedbackActionRecord).where(
                FeedbackActionRecord.project_id == c.project_id,
                FeedbackActionRecord.status == "selected",
            )
        ).one()
        action_id = action.id
        application = json.loads(action.plan_application_json)["applications"][0]
        chapter = session.get(ChapterPlan, c.chapter_ids[2])
        context = ChapterContextPack(
            project_id=c.project_id,
            project_title="有限反馈链",
            premise="根设定保持不变",
            genre="玄幻",
            setting_summary="石门",
            chapter_number=3,
            chapter_plan_title=chapter.title,
            chapter_plan_one_line=chapter.one_line,
            chapter_goals=["保留目标"],
            chapter_experience_plan=ChapterExperiencePlan.model_validate_json(
                chapter.experience_plan_json
            ),
            audience_hints=StateRepository(session).get_audience_hints(c.project_id, 3),
        )
    writer_model = FrozenWriterModel()
    output = ChapterWriter(
        writer_model, writer_mode="single", min_chapter_chars=200
    ).write_chapter(context)
    commit_id, trace_id = _accept(c.runtime, c.project_id, c.chapter_ids[2], output)
    with c.runtime.session_factory.begin() as session:
        action = session.get(FeedbackActionRecord, action_id)
        observed = FeedbackBodyObservationService().record(
            session=session,
            project_id=c.project_id,
            action_id=action_id,
            canon_commit_id=commit_id,
            assessment={
                "assessment": "observed",
                "observer": {
                    "kind": "frozen_observer",
                    "source_ref": "tests:test_feedback_finite_loop:RULE-v1",
                },
                "explanation": "独立规则标签：正文明确说归还钥匙是开门条件。只记录关联，不声称提示导致这一句。",
                "reviewed_content_sha256": session.get(
                    CandidateDraftRecord,
                    session.get(CanonCommitRecord, commit_id).candidate_id,
                ).body_hash,
                "quotes": [
                    {
                        "start": output.body.index(RULE),
                        "end": output.body.index(RULE) + len(RULE),
                        "text": RULE,
                    }
                ],
            },
        )
        assert observed["plan_revision"] == application["after_plan_revision"]
        assert observed["prompt_trace_ids"] == [trace_id]
        assert len(observed["prompt_input_ids"]) == len(writer_model.inputs) == 1
    after_remote = _publication_fixture(c.runtime, c.project_id, commit_id)
    _ingest(
        c.runtime,
        c.project_id,
        after_remote,
        ["担忧规则仍不够清楚"] * 2 + ["这一章继续读下去"] * 2,
        prefix="after",
    )
    # Existing short-window owner spans three chapters. Advance ordinary Canon
    # to 5 so [3,5] is disjoint from the frozen baseline [1,2].
    for number in (4, 5):
        output = WriterOutput(
            project_id=c.project_id,
            chapter_number=number,
            title=f"第{number}章",
            body="她沿石廊走向前方，回望已经敞开的门。",
            char_count=20,
            end_of_chapter_summary="继续前进。",
        )
        latest, _ = _accept(c.runtime, c.project_id, c.chapter_ids[number - 1], output)
    with c.runtime.session_factory.begin() as session:
        result = _service(c.runtime, model)._run_feedback_step(
            session, session.get(CanonCommitRecord, latest)
        )
        action = session.get(FeedbackActionRecord, action_id)
        effects = json.loads(action.effect_observation_json)["observations"][-1]
        assert effects["outcome"] == "associated_desired_change", effects
        assert effects["before_rate"] == 1.0 and effects["after_rate"] == 0.5
        assert effects["causal_claim"] is False
        assert effects["body_canon_commit_id"] == commit_id
        assert (
            len(
                session.scalars(
                    select(CanonCommitRecord).where(
                        CanonCommitRecord.project_id == c.project_id
                    )
                ).all()
            )
            == 5
        )
        assert result["analysis"]["selected_count"] == 4
        assert all(
            row["status"] == "completed" for row in result["analysis"]["analyses"]
        )


@pytest.mark.parametrize("reason", ["low_confidence", "single_author"])
def test_weak_or_single_author_feedback_does_not_modify_future_plan(
    finite_book, reason
):
    c = finite_book
    with c.runtime.session_factory() as session:
        before = session.get(ChapterPlan, c.chapter_ids[2]).experience_plan_json
    _ingest(
        c.runtime,
        c.project_id,
        c.remote,
        ["担忧规则没有说明代价"] * 3,
        prefix="weak",
        single_author=reason == "single_author",
    )
    model = FrozenCommentModel(confidence=0.3 if reason == "low_confidence" else 0.95)
    with c.runtime.session_factory.begin() as session:
        result = _service(c.runtime, model)._run_feedback_step(
            session, session.get(CanonCommitRecord, c.commits[-1])
        )
        assert session.get(ChapterPlan, c.chapter_ids[2]).experience_plan_json == before
        rows = session.scalars(select(FeedbackActionRecord)).all()
        if reason == "low_confidence":
            assert result["plan_applications"] == []
            assert rows == []
        else:
            assert len(rows) == 1
            observation_payload = json.loads(rows[0].action_payload_json)
            assert "plan_hint" not in observation_payload
            assert observation_payload["decision_reason"] == "watchlist_observation"
            assert "沿既定计划" in observation_payload["hint"]["text"]
            assert (
                json.loads(rows[0].aggregate_evidence_json)["known_author_count"] == 1
            )
            assert all(
                item["status"] == "refused" for item in result["plan_applications"]
            )
        assert result["analysis"]["selected_count"] == 3


def test_conflicting_strong_directions_preserve_plan_and_remain_observation_only(
    finite_book,
):
    c = finite_book
    with c.runtime.session_factory() as session:
        before = session.get(ChapterPlan, c.chapter_ids[2]).experience_plan_json
    first_remote = _publication_fixture(c.runtime, c.project_id, c.commits[0])
    _ingest(
        c.runtime,
        c.project_id,
        first_remote,
        ["推进太快"] * 2 + ["推进太慢"] * 2,
        prefix="conflict-first",
    )
    _ingest(
        c.runtime,
        c.project_id,
        c.remote,
        ["推进太快"] * 2 + ["推进太慢"] * 2,
        prefix="conflict-second",
    )
    with c.runtime.session_factory.begin() as session:
        result = _service(
            c.runtime, FrozenCommentModel(conflict=True)
        )._run_feedback_step(session, session.get(CanonCommitRecord, c.commits[-1]))
        rows = session.scalars(select(FeedbackActionRecord)).all()
        assert len(rows) == 2
        assert all(
            "plan_hint" not in json.loads(row.action_payload_json) for row in rows
        )
        assert {
            json.loads(row.action_payload_json)["decision_reason"] for row in rows
        } == {"conflicting_directions"}
        assert {item["reason"] for item in result["plan_applications"]} == {
            "action_has_no_plan_hint"
        }
        assert session.get(ChapterPlan, c.chapter_ids[2]).experience_plan_json == before


def test_late_comments_keep_source_chapter_and_never_rewrite_accepted_body(finite_book):
    c = finite_book
    _ingest(
        c.runtime,
        c.project_id,
        c.remote,
        ["担忧规则没有说明代价"] * 3,
        prefix="original",
    )
    model = FrozenCommentModel()
    with c.runtime.session_factory.begin() as session:
        _service(c.runtime, model)._run_feedback_step(
            session, session.get(CanonCommitRecord, c.commits[-1])
        )
        action = session.scalars(select(FeedbackActionRecord)).one()
        frozen_source = action.aggregate_evidence_json
        action_id = action.id
    output = WriterOutput(
        project_id=c.project_id,
        chapter_number=3,
        title="第3章",
        body="当前正文保持原样，不因迟到评论被改写。",
        char_count=23,
        end_of_chapter_summary="继续。",
    )
    third, _ = _accept(c.runtime, c.project_id, c.chapter_ids[2], output)
    with c.runtime.session_factory() as session:
        before = candidate_plan_revision(session.get(ChapterPlan, c.chapter_ids[2]))
    _ingest(
        c.runtime, c.project_id, c.remote, ["担忧上一章的旧规则"] * 3, prefix="late"
    )
    with c.runtime.session_factory.begin() as session:
        result = _service(c.runtime, model)._run_feedback_step(
            session, session.get(CanonCommitRecord, third)
        )
        late = session.scalars(
            select(PublisherRawComment).where(
                PublisherRawComment.remote_comment_id.like("late-%")
            )
        ).all()
        assert len(late) == 3
        assert {row.source_chapter_number for row in late} == {2}
        assert {row.source_canon_commit_id for row in late} == {c.commits[-1]}
        assert (
            session.get(FeedbackActionRecord, action_id).aggregate_evidence_json
            == frozen_source
        )
        assert (
            candidate_plan_revision(session.get(ChapterPlan, c.chapter_ids[2]))
            == before
        )
        accepted = session.get(
            CandidateDraftRecord, session.get(CanonCommitRecord, third).candidate_id
        )
        assert (
            session.get(ChapterDraft, accepted.candidate_draft_id).body_text
            == output.body
        )
        assert all(item["chapter_number"] > 3 for item in result["plan_applications"])
