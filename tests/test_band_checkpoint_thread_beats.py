from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.generation.pipeline_core.audit_control import AuditControlStage
from forwin.models.base import Base
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.protocol.book_state import GraphDelta
from forwin.review.plan_checks import evaluate_director_imbalance
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater


def test_setup_beats_without_delivery_flag_band() -> None:
    issues = evaluate_director_imbalance(
        review_metas=[
            {"chapter_number": number, "thread_beat_types": ["setup"]}
            for number in (1, 2)
        ],
        band_stall_guard=0,
        reviewer="test",
        target_scope="band",
    )
    assert [(issue.rule_name, issue.severity) for issue in issues] == [
        ("director_setup_without_delivery", "warning"),
    ]


@pytest.mark.parametrize("with_beats", [False, True])
def test_thread_beats_count_as_director_evidence(with_beats):
    issues = evaluate_director_imbalance(
        review_metas=[{
            "chapter_number": number, "planned_reward_tags": ["power"],
            "thread_beat_types": ["advance"] if with_beats else [],
        } for number in (1, 2, 3)],
        band_stall_guard=2, reviewer="test", target_scope="band",
    )
    assert [issue.rule_name for issue in issues] == ([
        "director_payoff_consecutive_missing", "director_payoff_consecutive_missing",
        "director_reward_gap_exceeded",
    ] if with_beats else [])


@pytest.mark.parametrize("metas", [
    [{"thread_beat_types": ["setup"]}],
    [{"issue_types": ["setup", "experience"]}] * 2,
    [{"thread_beat_types": ["advance"]}] * 2,
    [{"thread_beat_types": ["setup"]},
     {"thread_beat_types": ["setup"], "delivered_reward_tags": ["mystery"]}],
])
def test_setup_without_delivery_requires_setup_chapters_and_no_reward(metas) -> None:
    issues = evaluate_director_imbalance(
        review_metas=metas, band_stall_guard=0, reviewer="test", target_scope="band",
    )
    assert "director_setup_without_delivery" not in {issue.rule_name for issue in issues}


@pytest.mark.parametrize("source_type,allowed,status,delivery,expected", [
    ("chapter_extraction", True, "accepted", False, True),
    ("world_engine", True, "accepted", False, False),
    ("chapter_extraction", False, "accepted", False, False),
    ("chapter_extraction", True, "drafted", False, False),
    ("chapter_extraction", True, "accepted", True, False),
])
def test_checkpoint_reads_only_accepted_chapter_extraction_beats(
    monkeypatch, source_type, allowed, status, delivery, expected,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    stage = AuditControlStage()
    monkeypatch.setattr(stage, "_record_decision_event", lambda **kwargs: None)
    try:
        with Session(engine) as session:
            session.add(ArcPlanVersion(id="arc", project_id="p", arc_synopsis="", status="active"))
            session.add(BandExperiencePlan(
                project_id="p", arc_id="arc", band_id="band:2:3", chapter_start=2, chapter_end=3,
                schedule_json=json.dumps({"band_id": "band:2:3", "chapter_start": 2, "chapter_end": 3}),
            ))
            for chapter in (2, 3):
                session.add(ChapterPlan(
                    id=f"plan-{chapter}", project_id="p", arc_plan_id="arc",
                    chapter_number=chapter, status=status,
                ))
                session.add(ChapterDraft(id=f"draft-{chapter}", chapter_plan_id=f"plan-{chapter}", body_text=""))
                session.add(ChapterReview(
                    draft_id=f"draft-{chapter}", verdict="pass",
                    review_meta_json=json.dumps({"delivered_reward_tags": ["mystery"] if delivery else []}),
                ))
                BookStateRepository(session).append_graph_delta(GraphDelta(
                    id=f"delta-{chapter}", project_id="p", chapter_number=chapter,
                    source_type=source_type, allowed_for_canon=allowed,
                    metadata={"thread_beats": [{"thread_id": f"thread-{chapter}", "beat_type": "setup"}]},
                ))
            for chapter in (1, 4):
                BookStateRepository(session).append_graph_delta(GraphDelta(
                    id=f"outside-{chapter}", project_id="p", chapter_number=chapter,
                    source_type="chapter_extraction",
                    metadata={"thread_beats": [{"thread_id": "outside", "beat_type": "setup"}]},
                ))
            session.flush()
            checkpoint = stage._create_auto_band_checkpoint(
                session=session, repo=StateRepository(session), updater=StateUpdater(session),
                project_id="p", chapter_number=3,
            )
            issues = json.loads(checkpoint.issues_json)
            assert any("setup_like_count=2" in issue["detail"] for issue in issues) is expected
            if expected:
                assert checkpoint.status == "warn"
    finally:
        engine.dispose()


@pytest.mark.parametrize("code,expected", [
    ("onstage_unavailable", "fact_conflict"),
    ("location_unreachable", "fact_conflict"),
    ("reveal_too_early", "fact_conflict"),
    ("milestone_uncovered", "director_imbalance"),
    ("intra_band_consistency", "fact_conflict"),
    ("future_constraint", "fact_conflict"),
])
def test_live_plan_issue_groups(code, expected) -> None:
    from forwin.review.issue_groups import issue_group_for_issue

    assert issue_group_for_issue(code=code) == expected
