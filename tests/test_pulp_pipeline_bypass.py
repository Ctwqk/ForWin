from __future__ import annotations

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
