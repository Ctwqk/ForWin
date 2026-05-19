from __future__ import annotations

import pytest

from forwin.canon_quality.gate import evaluate_canon_admission, normalize_gate_mode
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.extractor.book_state_graph_delta import _filter_graph_delta_layers
from forwin.orchestrator_loop_core import quality_gates
from forwin.protocol.book_state import (
    CognitionPatch,
    GraphDelta,
    GraphDeltaType,
    MapPatch,
    NarrativePatch,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.hub import HistoricalReviewHub


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
    hub = HistoricalReviewHub(
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

    assert verdict.verdict == "pass"
    assert experience.calls == 0
    assert map_movement.calls == 0
    assert personality.calls == 0


def test_disabled_personality_reviewer_does_not_collect_or_review() -> None:
    personality = RecordingPersonalityReviewer()
    hub = HistoricalReviewHub(
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

    assert verdict.verdict == "pass"
    assert personality.collect_calls == 0
    assert personality.review_calls == 0


def test_disabled_experience_reviewer_does_not_choose_repair_escalation() -> None:
    experience = RecordingExperienceReviewer()
    hub = HistoricalReviewHub(
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
    ("gate_mode", "passes_none"),
    [
        ("off", True),
        ("fatal_only", True),
        ("shadow", False),
        ("strict", False),
    ],
)
def test_apply_canon_quality_gate_llm_client_by_gate_mode(
    monkeypatch,
    gate_mode: str,
    passes_none: bool,
) -> None:
    class StopAfterAnalysis(Exception):
        pass

    class Draft:
        id = "draft-1"

    class Review:
        id = "review-1"

    class Config:
        canon_quality_gate = gate_mode
        chapter_review_form_mode = "primary"

    sentinel_llm_client = object()

    class Orchestrator:
        config = Config()
        llm_client = sentinel_llm_client

        def _latest_draft_and_review_for_chapter(self, **kwargs):  # noqa: ANN003
            return Draft(), Review()

    captured: dict[str, object | None] = {}

    def fake_analyze_writer_output_quality(**kwargs):  # noqa: ANN003
        captured["llm_client"] = kwargs.get("llm_client")
        raise StopAfterAnalysis

    monkeypatch.setattr(
        quality_gates,
        "analyze_writer_output_quality",
        fake_analyze_writer_output_quality,
    )

    with pytest.raises(StopAfterAnalysis):
        quality_gates._apply_canon_quality_gate(
            Orchestrator(),
            session=None,
            repo=None,
            updater=None,
            project_id="project-1",
            chapter_number=1,
            writer_output=writer(),
            verdict=ReviewVerdict(verdict="pass", issues=[]),
        )

    assert captured["llm_client"] is (None if passes_none else sentinel_llm_client)


def test_world_only_layer_filter_removes_non_world_patches() -> None:
    delta = GraphDelta(
        id="delta-1",
        project_id="project-1",
        chapter_number=1,
        delta_type=GraphDeltaType.WORLD_STATE,
        summary="测试 delta",
        map_patches=[
            MapPatch(
                target_type="location",
                target_id="loc-a",
                op="set",
                field_path="x",
                new_value="y",
            )
        ],
        cognition_patches=[
            CognitionPatch(
                observer_type="character",
                observer_id="a",
                op="set",
                field_path="belief",
                new_value="b",
            )
        ],
        narrative_patches=[
            NarrativePatch(
                target_ref="thread:a",
                op="set",
                field_path="status",
                new_value="active",
            )
        ],
    )

    filtered = _filter_graph_delta_layers([delta], {"world"})

    assert filtered[0].map_patches == []
    assert filtered[0].cognition_patches == []
    assert filtered[0].narrative_patches == []
    assert filtered[0].metadata["requested_book_state_layers"] == ["world"]
    assert filtered[0].metadata["filtered_patch_counts"]["map"] == 1
