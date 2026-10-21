from __future__ import annotations

import pytest

from forwin.book_state.repository import BookStateRepository
from forwin.canon.preparation import BookStateCanonPreparer, CanonPreparationContext
from forwin.canon_quality.gate import evaluate_canon_admission, normalize_gate_mode
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.generation.pipeline_core import quality_gates
from forwin.naming import EntityAdmissionPlan
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.draft_service import DraftReviewService
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models import Project


class DummyChecker:
    def check(self, project_id, writer_output):  # noqa: ANN001
        return ReviewVerdict(verdict="pass", issues=[])


class RecordingReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        return ReviewVerdict(verdict="pass", issues=[])


class RecordingPersonalityReviewer:
    def __init__(self) -> None:
        self.collect_calls = 0
        self.review_calls = 0

    def collect(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.collect_calls += 1
        return []

    def review(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.review_calls += 1
        return ReviewVerdict(verdict="pass", issues=[])


class RecordingExperienceReviewer(RecordingReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.choose_repair_escalation_calls = 0

    def choose_repair_escalation(self, **kwargs):  # noqa: ANN003
        self.choose_repair_escalation_calls += 1
        return RepairInstruction(
            repair_scope="band",
            failure_type="mixed",
            scope_reason="fake escalation",
        )


def context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="project-1",
        project_title="测试",
        premise="测试",
        genre="玄幻",
        setting_summary="测试",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="角色A发现线索。",
        chapter_goals=[],
    )


def writer() -> WriterOutput:
    return WriterOutput(
        chapter_number=1,
        title="第一章",
        body="角色A推开门，看见线索。门外忽然传来密令？",
        char_count=24,
        end_of_chapter_summary="角色A发现线索。",
    )


def test_disabled_reviewers_are_not_called() -> None:
    experience = RecordingReviewer()
    map_movement = RecordingReviewer()
    personality = RecordingReviewer()
    hub = DraftReviewService(
        experience_reviewer=experience,
        map_movement_reviewer=map_movement,
        personality_reviewer=personality,
        experience_review_enabled=False,
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
    )

    verdict = hub.review(
        project_id="project-1",
        repo=None,
        context=context(),
        writer_output=writer(),
        continuity_checker=DummyChecker(),
    )

    assert verdict.verdict == "fail"
    assert [issue.issue_type for issue in verdict.issues] == [
        "entity_admission_plan_invalid"
    ]
    assert experience.calls == 0
    assert map_movement.calls == 0
    assert personality.calls == 0


def test_disabled_personality_reviewer_does_not_collect_or_review() -> None:
    personality = RecordingPersonalityReviewer()
    hub = DraftReviewService(
        personality_reviewer=personality,
        experience_review_enabled=False,
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
    )

    verdict = hub.review(
        project_id="project-1",
        repo=None,
        context=context(),
        writer_output=writer(),
        continuity_checker=DummyChecker(),
    )

    assert verdict.verdict == "fail"
    assert [issue.issue_type for issue in verdict.issues] == [
        "entity_admission_plan_invalid"
    ]
    assert personality.collect_calls == 0
    assert personality.review_calls == 0


def test_disabled_experience_reviewer_does_not_choose_repair_escalation() -> None:
    experience = RecordingExperienceReviewer()
    hub = DraftReviewService(
        experience_reviewer=experience,
        experience_review_enabled=False,
    )

    escalation = hub.choose_repair_escalation(
        context=context(),
        writer_output=writer(),
        review=ReviewVerdict(verdict="fail", issues=[]),
        repair_attempts=[{"attempt": 3}],
    )

    assert experience.choose_repair_escalation_calls == 0
    assert escalation.repair_scope == "scene"


def canon_signal(
    signal_type: str,
    severity: str = "error",
    evidence_refs: list[str] | None = None,
) -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=f"sig-{signal_type}",
        project_id="project-1",
        chapter_number=1,
        signal_type=signal_type,
        severity=severity,
        description="测试信号",
        evidence_refs=["body:证据"] if evidence_refs is None else evidence_refs,
    )


def test_fatal_only_mode_is_normalized() -> None:
    assert normalize_gate_mode("fatal_only") == "fatal_only"


def test_fatal_only_blocks_fatal_signal_with_evidence() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("countdown_non_monotonic")],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.blocking_issue_count == 1
    assert result.deterministic_issue_refs == ["sig-countdown_non_monotonic"]
    assert result.required_repair_scope is None


def test_fatal_only_routes_chapter_plan_fatal_signal_repair_scope() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("terminal_state_active_conflict")],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.deterministic_issue_refs == ["sig-terminal_state_active_conflict"]
    assert result.required_repair_scope == "chapter_plan"


def test_strict_mode_does_not_route_operator_signal_to_draft_repair() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("form_llm_unavailable", evidence_refs=[])],
        mode="strict",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.blocking_issue_count == 1
    assert result.deterministic_issue_refs == ["sig-form_llm_unavailable"]
    assert result.required_repair_scope is None


def test_fatal_only_does_not_block_warning_signal() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("countdown_non_monotonic", severity="warning")],
        mode="fatal_only",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.blocking_issue_count == 0
    assert result.deterministic_issue_refs == []
    assert result.required_repair_scope is None


def test_fatal_only_does_not_block_error_signal_with_no_evidence() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("countdown_non_monotonic", evidence_refs=[])],
        mode="fatal_only",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
    assert result.blocking_issue_count == 0
    assert result.deterministic_issue_refs == []
    assert result.residual_issue_refs == ["sig-countdown_non_monotonic"]
    assert result.required_repair_scope is None


def test_fatal_only_does_not_block_non_fatal_form_analyzer_issue() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        analyzer_results=[
            {
                "analyzer": "ChapterReviewForm",
                "blocking": True,
                "confidence": 1.0,
                "issues": [
                    {
                        "issue_id": "form-1",
                        "type": "form_obligation_unresolved",
                        "severity": "error",
                        "evidence_quote": "义务-1没有被完成。",
                    }
                ],
            }
        ],
        mode="fatal_only",
    )

    assert result.commit_allowed is True
    assert result.verdict == "pass"
    assert result.llm_issue_refs == []
    assert result.required_repair_scope is None


def test_fatal_only_does_not_block_mixed_form_result_with_fatal_warning() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        analyzer_results=[
            {
                "analyzer": "ChapterReviewForm",
                "blocking": True,
                "confidence": 1.0,
                "issues": [
                    {
                        "issue_id": "form-obligation",
                        "type": "form_obligation_unresolved",
                        "severity": "error",
                        "evidence_quote": "义务-1没有被完成。",
                    },
                    {
                        "issue_id": "form-countdown-warning",
                        "type": "form_countdown_inconsistency",
                        "severity": "warning",
                        "evidence_quote": "倒计时描述存在不确定性。",
                    },
                ],
            }
        ],
        mode="fatal_only",
    )

    assert result.commit_allowed is True
    assert result.verdict == "pass"
    assert result.llm_issue_refs == []


def test_fatal_only_blocks_fatal_form_analyzer_error_with_evidence() -> None:
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        analyzer_results=[
            {
                "analyzer": "ChapterReviewForm",
                "blocking": True,
                "confidence": 1.0,
                "issues": [
                    {
                        "issue_id": "form-countdown-error",
                        "type": "form_countdown_inconsistency",
                        "severity": "error",
                        "evidence_quote": "倒计时从三分钟回到六十八分钟。",
                    }
                ],
            }
        ],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.llm_issue_refs == ["ChapterReviewForm:form-countdown-error"]


@pytest.mark.parametrize(
    ("quality_gate", "passes_none", "expected_analysis_mode"),
    [
        ("pulp_fatal", True, "off"),
        ("strict", False, "primary"),
    ],
)
def test_apply_canon_quality_gate_llm_client_by_gate_mode(
    monkeypatch,
    quality_gate: str,
    passes_none: bool,
    expected_analysis_mode: str,
) -> None:
    class StopAfterAnalysis(Exception):
        pass

    class Draft:
        id = "draft-1"

    class Review:
        id = "review-1"

    class CanonPolicy:
        pass

    canon_policy = CanonPolicy()
    canon_policy.quality_gate = quality_gate

    class Policy:
        canon = canon_policy

    class AttemptClient:
        def drain_llm_attempt_events(self):
            captured["drained"] = True
            return [
                {
                    "attempt_group_id": "canon-gate",
                    "stage_key": "chapter_review_form",
                    "http_status": 200,
                }
            ]

    captured: dict[str, object | None] = {}
    sentinel_llm_client = AttemptClient()

    def save_prompt_trace(**kwargs):  # noqa: ANN003
        captured["prompt_trace"] = kwargs["prompt_trace"]
        return "canon-gate-trace"

    context = CanonPreparationContext(
        policy=Policy(),  # type: ignore[arg-type]
        llm_client=sentinel_llm_client,  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
        _record_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
        _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
        save_prompt_trace=save_prompt_trace,
    )

    def fake_analyze_writer_output_quality(**kwargs):  # noqa: ANN003
        captured["llm_client"] = kwargs.get("llm_client")
        captured["mode"] = kwargs.get("mode")
        raise StopAfterAnalysis

    monkeypatch.setattr(
        quality_gates,
        "analyze_writer_output_quality",
        fake_analyze_writer_output_quality,
    )
    monkeypatch.setattr(
        quality_gates,
        "_latest_draft_and_review_for_chapter",
        lambda **_kwargs: (Draft(), Review()),
    )

    with pytest.raises(StopAfterAnalysis):
        quality_gates._apply_canon_quality_gate(
            context,
            session=None,
            repo=None,
            updater=None,
            project_id="project-1",
            chapter_number=1,
            writer_output=writer(),
            verdict=ReviewVerdict(verdict="pass", issues=[]),
        )

    assert captured["llm_client"] is (None if passes_none else sentinel_llm_client)
    assert captured["mode"] == expected_analysis_mode
    if passes_none:
        assert "drained" not in captured
        assert "prompt_trace" not in captured
    else:
        assert captured["drained"] is True
        assert captured["prompt_trace"]["input_snapshot"] == {
            "project_id": "project-1",
            "chapter_number": 1,
            "candidate_id": "",
            "gate_id": "canon_quality",
        }


def test_deferred_single_writer_extraction_failure_blocks_canon() -> None:
    """A deferred single-pass writer gets no regex fallback: an unusable extraction blocks canon."""

    class EmptyResponseClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN003
            self.calls += 1
            return ""

    client = EmptyResponseClient()
    recorded: list[dict[str, object]] = []
    engine = get_engine(postgres_test_url("pulp_empty_extraction_blocks"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(title="Pulp empty", premise="前提", genre="玄幻")
            session.add(project)
            session.flush()
            outcome = BookStateCanonPreparer().prepare(
                context=CanonPreparationContext(
                    policy=RuntimePolicy.for_profile("pulp"),
                    llm_client=client,  # type: ignore[arg-type]
                    artifact_store=object(),  # type: ignore[arg-type]
                    _record_decision_event=lambda **payload: recorded.append(payload),  # type: ignore[arg-type]
                    _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
                ),
                session=session,
                candidate_id="candidate-1",
                project_id=project.id,
                chapter_number=1,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="第一章",
                    body="林夜收下玄铁令，决定进入问心阁。",
                    char_count=18,
                    end_of_chapter_summary="林夜获得玄铁令并进入问心阁。",
                    generation_meta={"mode": "single"},
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
                entity_admission_plan=EntityAdmissionPlan(
                    project_id=project.id, chapter_number=1, candidate_fingerprint="",
                ),
            )
            nodes = BookStateRepository(session).list_world_nodes(project.id, as_of_chapter=1)

        assert outcome.blocked is True
        assert outcome.blocked_path == "chapter-delta-extraction-blocked"
        assert client.calls == 3
        assert nodes == []
        payload = recorded[-1]["payload"]
        assert isinstance(payload, dict)
        assert [issue["code"] for issue in payload["issues"]] == ["extraction_failed"]
    finally:
        engine.dispose()
