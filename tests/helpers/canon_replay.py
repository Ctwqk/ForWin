from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.models import ArcPlanVersion, CandidateDraftRecord, ChapterDraft, ChapterPlan, ChapterReview, Project


def seed_project_with_accepted_chapter(session, *, chapter_number: int = 1, body: str = "主倒计时还有59分钟。"):
    project = Project(title="Canon Replay Test", premise="测试", genre="悬疑", target_total_chapters=3)
    session.add(project)
    session.flush()
    arc = ArcPlanVersion(project_id=project.id, arc_synopsis="测试", status="active", chapter_start=1, chapter_end=3)
    session.add(arc)
    session.flush()
    plan, draft = seed_accepted_chapter(
        session,
        project=project,
        arc=arc,
        chapter_number=chapter_number,
        body=body,
    )
    return project, arc, plan, draft


def seed_accepted_chapter(session, *, project: Project, arc: ArcPlanVersion, chapter_number: int, body: str = "主倒计时还有59分钟。"):
    plan = ChapterPlan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        one_line="测试",
        status="accepted",
    )
    session.add(plan)
    session.flush()
    draft = ChapterDraft(
        id=f"draft-{chapter_number}",
        chapter_plan_id=plan.id,
        version=1,
        body_text=body,
        summary=f"第{chapter_number}章摘要",
        char_count=len(body),
        llm_raw_response="{}",
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(id=f"review-{chapter_number}", draft_id=draft.id, verdict="pass")
    session.add(review)
    session.flush()
    session.add(
        CandidateDraftRecord(
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=chapter_number,
            candidate_draft_id=draft.id,
            review_id=review.id,
            status="canon_committed",
            canon_status="canon",
        )
    )
    session.flush()
    return plan, draft


class FakeCountdownClient:
    def __init__(self, project_id: str, chapter_number: int) -> None:
        self.project_id = project_id
        self.chapter_number = chapter_number
        self.llm_attempt_events = [
            {
                "status": "succeeded",
                "input_text": "Replay prompt: 主倒计时还有59分钟。",
                "output_text": "Replay answer: 主倒计时还有59分钟。",
                "duration_ms": 10,
            }
        ]

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "主倒计时还有59分钟。"
        return {
            "project_id": self.project_id,
            "chapter_number": self.chapter_number,
            "form_schema_version": FORM_SCHEMA_VERSION,
            "characters": [],
            "countdowns": [
                {
                    "key": "main",
                    "mentioned_in_chapter": True,
                    "status_in_this_chapter": {
                        "value": "active",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.95,
                    },
                    "new_value_minutes": 59,
                    "new_value_evidence": {
                        "value": "59",
                        "evidence_quote": quote,
                        "subject_of_quote": "主倒计时",
                        "confidence": 0.95,
                    },
                    "consistent_with_prior": {"value": "true", "confidence": 0.95},
                    "inconsistency_kind": "",
                }
            ],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "主倒计时继续。",
        }
