from __future__ import annotations

import importlib.util
import json

import pytest

from forwin.book_state.extraction.chapter_delta_schema import (
    ChapterDeltaExtraction,
    PlanFulfillment,
    ThreadBeat,
)
from forwin.book_state.extraction.chapter_input import _fulfillment_targets
from forwin.models.project import ChapterPlan
from forwin.planning.constraints import NarrativeConstraintInfo
from forwin.review.plan_reviewer import PlanContractReviewer
from forwin.runtime.policy import RuntimePolicy


from tests.chapter_delta_helpers import plan_review_context as _context, plan_review_output as _output


def test_missing_goal_is_error_partial_is_warning() -> None:
    context = _context(["Deliver the packet", "Identify the sender"])
    review = PlanContractReviewer().review(
        context, _output(("goal:1", "missing"), ("goal:2", "partial")),
    )

    assert review.verdict == "fail"
    assert [(issue.rule_name, issue.severity) for issue in review.issues] == [
        ("plan_goal_missing", "error"), ("plan_goal_partial", "warning"),
    ]
    for goal, issue in zip(context.chapter_goals, review.issues):
        assert issue.issue_type == "plan_task_fulfillment"
        assert issue.target_scope == "chapter"
        assert goal in issue.suggested_fix
    assert [issue.blocking for issue in review.issues] == [True, False]


@pytest.mark.parametrize("profile,rewrites", [("standard", 3), ("pulp", 1)])
def test_missing_goal_gets_the_profile_rewrites_and_leaves_other_budgets_alone(profile, rewrites) -> None:
    review = PlanContractReviewer().review(_context(["Deliver the packet"]), _output(("goal:1", "missing")))
    policy = RuntimePolicy.for_profile(profile).review

    assert policy.effective_rewrite_limit(has_blocking_issue=any(issue.blocking for issue in review.issues)) == rewrites
    # Standard does not pause any sooner; a chapter without a blocking issue keeps max_rewrites.
    assert policy.effective_rewrite_limit(has_blocking_issue=False) == policy.max_rewrites
    assert rewrites >= policy.max_rewrites


def test_goal_absent_from_extraction_counts_as_missing() -> None:
    review = PlanContractReviewer().review(
        _context(["Deliver the packet", "Identify the sender"]),
        _output(("goal:1", "done")),
    )

    assert review.verdict == "fail"
    assert len(review.issues) == 1
    assert review.issues[0].rule_name == "plan_goal_missing"
    assert review.issues[0].severity == "error"
    assert "Identify the sender" in review.issues[0].suggested_fix


@pytest.mark.parametrize("goals", [["Find the ledger"], []])
def test_no_extraction_leaves_the_missing_delta_to_world_consistency(goals) -> None:
    # world_consistency reports chapter_delta_missing; the plan reviewer has nothing to judge.
    review = PlanContractReviewer().review(_context(goals), _output(extracted=False))

    assert review.verdict == "pass"
    assert review.issues == []


def test_keyword_module_is_gone() -> None:
    assert importlib.util.find_spec("forwin.review.constraint_keywords") is None


def test_done_goals_ignore_wording_and_other_target_types() -> None:
    context = _context(["A description absent from the prose"])
    context.active_future_constraints = [NarrativeConstraintInfo(
        id="legacy", constraint_type="secret_withhold", level="hard",
        subject_name="packet", payload={"forbidden_keywords": ["packet"]},
    )]
    review = PlanContractReviewer().review(
        context,
        _output(
            ("goal:1", "done"), ("goal:999", "missing"),
            ("obligation:open", "missing"), ("reward:power", "partial"),
        ),
    )

    assert review.verdict == "pass"
    assert review.issues == []


def test_partial_goal_warns_without_failing() -> None:
    review = PlanContractReviewer().review(
        _context(["Deliver the packet"]), _output(("goal:1", "partial")),
    )

    assert review.verdict == "warn"
    assert review.issues[0].rule_name == "plan_goal_partial"


@pytest.mark.parametrize(("statuses", "verdict", "expected_issues"), [
    (("done", "partial", "missing"), "pass", []),
    (("missing", "partial", "done"), "pass", []),
    (("partial", "done", "missing"), "pass", []),
    (("partial", "missing"), "warn", [("plan_goal_partial", "warning")]),
    (("missing", "partial"), "warn", [("plan_goal_partial", "warning")]),
    (("missing", "missing"), "fail", [("plan_goal_missing", "error")]),
])
def test_duplicate_fulfillment_uses_best_status(statuses, verdict, expected_issues) -> None:
    review = PlanContractReviewer().review(
        _context(["Deliver the packet"]),
        _output(*(("goal:1", status) for status in statuses)),
    )

    assert review.verdict == verdict
    assert [(issue.rule_name, issue.severity) for issue in review.issues] == expected_issues


def test_structured_thread_keep_open_constraint_remains_enforced() -> None:
    context = _context([])
    context.active_future_constraints = [NarrativeConstraintInfo(
        id="thread-contract", constraint_type="thread_keep_open", level="hard",
        subject_name="thread-1",
    )]
    output = _output()
    output.chapter_delta.thread_beats = [ThreadBeat(
        thread_ref="thread-1", beat_type="resolution", evidence=output.body,
    )]

    review = PlanContractReviewer().review(context, output)

    assert review.verdict == "fail"
    assert [(issue.rule_name, issue.severity) for issue in review.issues] == [
        ("future_constraint_violation", "error"),
    ]
    output.chapter_delta.thread_beats[0].beat_type = "advance"
    assert PlanContractReviewer().review(context, output).issues == []


def test_goal_numbering_preserves_blank_and_non_string_positions() -> None:
    goals = ["First goal", " ", None, 42, "Last goal"]
    plan = ChapterPlan(goals_json=json.dumps(goals))
    targets = _fulfillment_targets(None, "test", plan)
    assert [(target.target_id, target.description) for target in targets if target.target_id.startswith("goal:")] == [
        ("goal:1", "First goal"), ("goal:5", "Last goal"),
    ]
    # Exercise numbering on the raw list; normal context validation requires strings.
    context = _context([]).model_copy(update={"chapter_goals": goals})
    review = PlanContractReviewer().review(
        context, _output(("goal:1", "done"), ("goal:5", "partial")),
    )

    assert [(issue.rule_name, issue.severity) for issue in review.issues] == [
        ("plan_goal_partial", "warning"),
    ]
    assert "Last goal" in review.issues[0].suggested_fix
