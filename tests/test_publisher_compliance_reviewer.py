from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.hub import HistoricalReviewHub
from forwin.reviewer.publisher_compliance import PublisherComplianceReviewer


def _writer(body: str, title: str = "第一章") -> WriterOutput:
    return WriterOutput(
        project_id="project-1",
        chapter_number=1,
        title=title,
        body=body,
        char_count=len(body),
        end_of_chapter_summary="测试章节。",
    )


def _context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="project-1",
        project_title="测试书",
        premise="测试 premise",
        genre="都市",
        setting_summary="",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="测试",
        chapter_goals=[],
    )


class _PassReviewer:
    def review(self, *_args, **_kwargs) -> ReviewVerdict:
        return ReviewVerdict(verdict="pass", issues=[])


class _PassContinuity:
    def check(self, _project_id: str, _writer_output: WriterOutput) -> ReviewVerdict:
        return ReviewVerdict(verdict="pass", issues=[])


def test_publisher_compliance_reviewer_emits_error_for_contact_text() -> None:
    verdict = PublisherComplianceReviewer(platform_ids=["qidian", "fanqie"]).review(
        _context(),
        _writer("主角说：加微信 vx123456 领取番外。"),
    )

    assert verdict.verdict == "fail"
    issue = verdict.issues[0]
    assert issue.reviewer == "publisher_compliance"
    assert issue.issue_type == "publisher_compliance"
    assert issue.rule_name.startswith("publisher_compliance_")
    assert issue.severity == "error"
    assert issue.evidence_refs


def test_publisher_compliance_reviewer_allows_contact_label_in_fictional_record() -> None:
    verdict = PublisherComplianceReviewer().review(
        _context(),
        _writer("档案表格的标题栏写着名字、地址、联系方式，林陈扫过几行。"),
    )

    assert verdict.verdict == "pass"
    assert verdict.issues == []


def test_publisher_compliance_reviewer_emits_warning_for_soft_promotional_text() -> None:
    verdict = PublisherComplianceReviewer().review(
        _context(),
        _writer("作者有话说：求收藏求推荐票，喜欢可以继续追读。"),
    )

    assert verdict.verdict == "warn"
    assert verdict.issues[0].severity == "warning"


def test_historical_review_hub_merges_publisher_compliance_when_enabled() -> None:
    hub = HistoricalReviewHub(
        experience_review_enabled=False,
        lint_review_enabled=False,
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
        governance_reviewer=_PassReviewer(),
        publisher_compliance_review_enabled=True,
    )

    verdict = hub.review(
        project_id="project-1",
        context=_context(),
        writer_output=_writer("这一章结尾写着联系 QQ 群 123456。"),
        continuity_checker=_PassContinuity(),
    )

    assert verdict.verdict == "fail"
    assert any(issue.reviewer == "publisher_compliance" for issue in verdict.issues)


def test_historical_review_hub_unchanged_when_publisher_compliance_disabled() -> None:
    hub = HistoricalReviewHub(
        experience_review_enabled=False,
        lint_review_enabled=False,
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
        governance_reviewer=_PassReviewer(),
        publisher_compliance_review_enabled=False,
    )

    verdict = hub.review(
        project_id="project-1",
        context=_context(),
        writer_output=_writer("这一章结尾写着联系 QQ 群 123456。"),
        continuity_checker=_PassContinuity(),
    )

    assert verdict.verdict == "pass"
    assert not any(issue.reviewer == "publisher_compliance" for issue in verdict.issues)
