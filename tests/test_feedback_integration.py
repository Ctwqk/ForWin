from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from forwin.audience.actions import ActionMapper
from forwin.audience.feedback import FeedbackCooldown
from forwin.context.providers.feedback_provider import FeedbackContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.models import ChapterPlan, FeedbackActionRecord, Project
from forwin.models.canon import CanonCommitRecord
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from tests.test_comment_source_identity import _published_chapter
from tests.test_feedback_action_lifecycle import aggregate_view
from tests.test_feedback_consumer_transaction import _service

pytest_plugins = [
    "tests.test_feedback_action_lifecycle",
    "tests.test_comment_consumption_contract",
    "tests.test_feedback_body_observation",
]


def test_context_provider_consumes_selected_canonical_hint_only(action_session):
    mapper = ActionMapper()
    rows = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions(
            [aggregate_view(signal_type="prediction", direction="predicts")]
        ),
        cooldown=FeedbackCooldown(),
    )
    request = ContextRequest(
        project_id="book",
        chapter_plan=SimpleNamespace(chapter_number=5),
        repo=StateRepository(action_session),
    )
    before = ContextDraft()
    FeedbackContextProvider().contribute(request, before)
    assert before.data["audience_hints"] is None
    mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        action_ids=[rows[0].id],
        cooldown=FeedbackCooldown(),
    )
    selected = ContextDraft()
    FeedbackContextProvider().contribute(request, selected)
    assert selected.data["audience_hints"].items[0].action_id == rows[0].id
    assert selected.data["audience_hints"].prediction_hints
    assert selected.data["reader_feedback"] is None


class RiskModel:
    def chat(self, messages, **kwargs):
        comments = json.loads(messages[-1]["content"].split("评论列表：", 1)[1])
        return json.dumps(
            {
                "signals": [
                    {
                        "comment_index": c["comment_index"],
                        "signal_type": "risk",
                        "direction": "concern",
                        "target_type": "plot",
                        "target_name": "规则",
                        "severity": 3,
                        "confidence": 0.95,
                        "evidence_span": c["body"],
                    }
                    for c in comments
                ]
            },
            ensure_ascii=False,
        )


def test_post_canon_consumes_comments_selects_and_applies_only_future_plan(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    plan_id, _, commit_id, _ = _published_chapter(runtime, project_id)
    with runtime.session_factory.begin() as session:
        previous = session.get(ChapterPlan, plan_id)
        project = session.get(Project, project_id)
        project.target_total_chapters = 10
        project.runtime_policy_json = RuntimePolicy.for_profile(
            "standard"
        ).model_dump_json()
        project.runtime_policy_version = 1
        session.add(
            ChapterPlan(
                id="future-plan",
                project_id=project_id,
                arc_plan_id=previous.arc_plan_id,
                chapter_number=3,
                title="既定第三章",
                one_line="调查继续",
                goals_json='["既定目标"]',
                experience_plan_json=ChapterExperiencePlan(
                    rule_anchors=["历史规则"]
                ).model_dump_json(),
            )
        )
        original = previous.experience_plan_json
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "work_id": "remote-work",
                "chapter_id": "remote-chapter",
                "remote_comment_id": f"risk-{i}",
                "author_id": f"reader-{i}",
                "body": "规则代价没有讲清楚",
            }
            for i in range(3)
        ],
    )
    with runtime.session_factory.begin() as session:
        result = _service(runtime, RiskModel())._run_feedback_step(
            session, session.get(CanonCommitRecord, commit_id)
        )
    with runtime.session_factory() as session:
        rows = session.scalars(
            select(FeedbackActionRecord).where(
                FeedbackActionRecord.project_id == project_id
            )
        ).all()
        assert len(rows) == 1 and rows[0].status == "selected"
        row = rows[0]
        application = json.loads(row.plan_application_json)["applications"][0]
        assert application["status"] == "applied" and application["chapter_number"] == 3
        assert application["before_plan_revision"] != application["after_plan_revision"]
        assert application["source"]["aggregate_id"] == row.aggregate_id
        assert session.get(ChapterPlan, plan_id).experience_plan_json == original
        future = session.get(ChapterPlan, "future-plan")
        assert future.goals_json == '["既定目标"]'
        assert len(json.loads(future.experience_plan_json)["rule_anchors"]) == 2
        assert json.loads(row.prompt_inclusions_json) == []
        assert json.loads(row.body_observation_json) == {}
        observed = json.loads(row.effect_observation_json)["observations"]
        assert observed[-1]["outcome"] == "insufficient_data"
        assert "body_application_unobserved" in observed[-1]["reasons"]
        assert result["plan_applications"][0]["status"] == "applied"


def test_post_canon_with_no_comments_keeps_existing_plan(comments_runtime):
    runtime, project_id = comments_runtime
    plan_id, _, commit_id, _ = _published_chapter(runtime, project_id)
    with runtime.session_factory.begin() as session:
        previous = session.get(ChapterPlan, plan_id)
        original = previous.experience_plan_json
        result = _service(runtime, RiskModel())._run_feedback_step(
            session, session.get(CanonCommitRecord, commit_id)
        )
        assert previous.experience_plan_json == original
        assert result["plan_applications"] == []
        assert session.scalars(select(FeedbackActionRecord)).all() == []


def test_post_canon_records_body_identity_but_does_not_invent_assessment(body_case):
    case = body_case
    runtime = SimpleNamespace(session_factory=sessionmaker(bind=case.session.bind))
    result = _service(runtime, RiskModel())._run_feedback_step(case.session, case.canon)
    observation = result["body_observations"][0]
    assert observation["canon_commit_id"] == case.canon.id
    assert observation["assessment"] == "unknown"
    assert observation["content_sha256"] == case.digest
    assert observation["prompt_input_ids"] == [case.event["input_id"]]
    assert "unassessed" in observation["reasons"]
    assert json.loads(case.action.body_observation_json)["observations"] == [
        observation
    ]


def test_invalid_body_observation_does_not_become_new_production_gate(body_case):
    case = body_case
    case.candidate.body_hash = "incorrect"
    case.session.commit()
    runtime = SimpleNamespace(session_factory=sessionmaker(bind=case.session.bind))
    result = _service(runtime, RiskModel())._run_feedback_step(case.session, case.canon)
    observation = result["body_observations"][0]
    assert observation["assessment"] == "unknown"
    assert observation["reasons"] == ["body_observation_unavailable"]
    assert observation["detail"] == "BODY changed after acceptance"
    assert json.loads(case.action.body_observation_json) == {}


@pytest.mark.parametrize("invalid", [[None], {"unexpected": "object"}, "invalid"])
def test_malformed_observation_history_stays_passive_and_is_preserved(
    body_case, invalid
):
    case = body_case
    raw = json.dumps({"version": 1, "observations": invalid})
    case.action.body_observation_json = raw
    case.session.commit()
    runtime = SimpleNamespace(session_factory=sessionmaker(bind=case.session.bind))
    result = _service(runtime, RiskModel())._run_feedback_step(case.session, case.canon)
    assert result["body_observations"][0]["assessment"] == "unknown"
    assert result["effect_observations"][0]["outcome"] == "insufficient_data"
    assert (
        "invalid_body_observation_history"
        in result["effect_observations"][0]["reasons"]
    )
    assert case.action.body_observation_json == raw


def test_stale_effect_session_preserves_other_committed_history(comments_runtime):
    from forwin.audience.effects import record_signal_change_observations
    from tests.test_feedback_plan_service import _selected

    runtime, project_id = comments_runtime
    with runtime.session_factory.begin() as setup:
        _selected(setup, project_id=project_id)
    with runtime.session_factory() as stale:
        retained = stale.get(FeedbackActionRecord, "action")
        assert retained.effect_observation_json == "{}"
        with runtime.session_factory.begin() as other:
            first = record_signal_change_observations(
                other, project_id=project_id, chapter_number=3, aggregate_views=[]
            )[0]
        second = record_signal_change_observations(
            stale, project_id=project_id, chapter_number=4, aggregate_views=[]
        )[0]
        stale.commit()
    with runtime.session_factory() as check:
        saved = json.loads(
            check.get(FeedbackActionRecord, "action").effect_observation_json
        )
        assert saved["observations"] == [first, second]


def test_effect_history_refresh_does_not_discard_unflushed_action_edit(body_case):
    from forwin.audience.effects import record_signal_change_observations

    body_case.action.notes = "Caller pending work"
    with pytest.raises(ValueError, match="flushed action inputs"):
        record_signal_change_observations(
            body_case.session, project_id="book", chapter_number=3, aggregate_views=[]
        )
    assert body_case.action.notes == "Caller pending work"
    assert body_case.action in body_case.session.dirty
