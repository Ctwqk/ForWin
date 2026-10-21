from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from forwin.book_state.extraction.world_review import review_chapter_extraction
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue

ISSUE_TYPE = "world_consistency"
_WORLD_FIX = "改写相关情节，使人物行动、移动与时间符合本书世界状态。"


class WorldConsistencyReviewer:
    """World gate v6 at draft time, so world errors reach draft repair instead of blocking canon.

    The candidate's chapter delta goes through the same translation and gate that canon
    preparation runs. Translator and gate issues keep their severity; extraction issues
    are warnings, and a candidate with no chapter delta is an error.
    """

    name = "world_consistency"

    def review(
        self,
        *,
        session: Session | None,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        travel_tolerance: float,
    ) -> ReviewVerdict:
        generation_meta = writer_output.generation_meta if isinstance(writer_output.generation_meta, dict) else {}
        issues = [
            self._issue(
                severity="warning",
                code=str(item.get("code") or "chapter_delta_issue"),
                message=str(item.get("message") or ""),
                evidence_ref=_path_ref(item.get("path")),
                source_analyzer="ChapterDeltaExtractor",
            )
            for item in generation_meta.get("chapter_delta_issues") or []
            if isinstance(item, dict)
        ]
        extraction = writer_output.chapter_delta
        if extraction is None:
            issues.append(self._issue(
                severity="error",
                code="chapter_delta_missing",
                message="本章缺少章节 delta 抽取，无法核验世界一致性。",
                evidence_ref=f"chapter:{chapter_number}",
                source_analyzer="ChapterDeltaExtractor",
                suggested_fix="重写正文后会重新抽取章节 delta。",
            ))
            return _verdict(issues)
        admission_plan = _current_admission_plan(writer_output, generation_meta)
        if admission_plan is None:
            # The entity admission signal already fails a candidate without a current plan.
            return _verdict(issues)
        world = review_chapter_extraction(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            extraction=extraction,
            admission_plan=admission_plan,
            chapter_summary=writer_output.end_of_chapter_summary,
            travel_tolerance=travel_tolerance,
        )
        issues.extend(
            self._issue(
                severity=issue.severity,
                code=issue.code,
                message=issue.message,
                evidence_ref=_path_ref(issue.path),
                source_analyzer="ChapterDeltaTranslator",
            )
            for issue in world.translation.issues
        )
        if world.gate is not None:
            issues.extend(
                self._issue(
                    severity=issue.severity,
                    code=issue.code,
                    message=issue.message,
                    evidence_ref=issue.target_ref,
                    source_analyzer="BookStateReviewGate",
                )
                for issue in world.gate.issues
            )
        return _verdict(issues)

    def _issue(
        self,
        *,
        severity: str,
        code: str,
        message: str,
        evidence_ref: str,
        source_analyzer: str,
        suggested_fix: str = "",
    ) -> ContinuityIssue:
        error = severity == "error"
        return ContinuityIssue(
            rule_name=code,
            severity="error" if error else "warning",
            description=message or code,
            reviewer=self.name,
            issue_type=ISSUE_TYPE,
            target_scope="draft",
            issue_group=issue_group_for_issue(issue_type=ISSUE_TYPE),
            evidence_refs=[evidence_ref] if evidence_ref else [],
            suggested_fix=suggested_fix or (_WORLD_FIX if error else ""),
            source_layer="book_state",
            source_analyzer=source_analyzer,
            blocking=error,
        )


def _path_ref(path: Any) -> str:
    text = str(path or "").strip()
    return f"chapter_delta:{text}" if text else ""


def _current_admission_plan(
    writer_output: WriterOutput,
    generation_meta: dict[str, Any],
) -> EntityAdmissionPlan | None:
    payload = generation_meta.get("entity_admission_plan")
    if not isinstance(payload, dict):
        return None
    try:
        plan = EntityAdmissionPlan.model_validate(payload)
    except ValidationError:
        return None
    if plan.candidate_fingerprint != writer_output_admission_fingerprint(writer_output):
        return None
    return plan


def _verdict(issues: list[ContinuityIssue]) -> ReviewVerdict:
    severities = {issue.severity for issue in issues}
    verdict = "fail" if "error" in severities else "warn" if "warning" in severities else "pass"
    return ReviewVerdict(verdict=verdict, issues=issues)


__all__ = ["WorldConsistencyReviewer"]
