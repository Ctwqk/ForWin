from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from pydantic import ValidationError
from sqlalchemy.orm import Session

from forwin.book_state.extraction.world_review import review_chapter_extraction
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue
from forwin.writer.prompt_core.world_repair import world_repair_fix

ISSUE_TYPE = "world_consistency"
# Gate and translator messages separate refs with spaces, commas, semicolons, parens and quotes.
_REF_TOKEN = re.compile(r"[^\s,;()'\"]+")


class _Finding(NamedTuple):
    severity: str
    code: str
    message: str
    evidence_ref: str
    source_analyzer: str


class WorldConsistencyReviewer:
    """World gate v6 at draft time, so world errors reach draft repair instead of blocking canon.

    The candidate's chapter delta goes through the same translation and gate that canon
    preparation runs. Translator and gate issues keep their severity; extraction issues
    are warnings, and a candidate with no chapter delta is an error. Issue text names
    entities, sites and facts the way the writer knows them, not by their ids.
    """

    name = "world_consistency"

    def review(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        travel_tolerance: float,
    ) -> ReviewVerdict:
        generation_meta = writer_output.generation_meta if isinstance(writer_output.generation_meta, dict) else {}
        findings = [
            _Finding(
                "warning",
                str(item.get("code") or "chapter_delta_issue"),
                str(item.get("message") or ""),
                _path_ref(item.get("path")),
                "ChapterDeltaExtractor",
            )
            for item in generation_meta.get("chapter_delta_issues") or []
            if isinstance(item, dict)
        ]
        names: Mapping[str, str] = {}
        extraction = writer_output.chapter_delta
        if extraction is None:
            findings.append(_Finding(
                "error",
                "chapter_delta_missing",
                "本章缺少章节 delta 抽取，无法核验世界一致性。",
                f"chapter:{chapter_number}",
                "ChapterDeltaExtractor",
            ))
        # Without a current admission plan the entity admission signal already fails the candidate.
        elif (admission_plan := _current_admission_plan(writer_output, generation_meta)) is not None:
            world = review_chapter_extraction(
                session,
                project_id=project_id,
                chapter_number=chapter_number,
                extraction=extraction,
                admission_plan=admission_plan,
                chapter_summary=writer_output.end_of_chapter_summary,
                travel_tolerance=travel_tolerance,
            )
            names = world.names
            findings.extend(
                _Finding(issue.severity, issue.code, issue.message, _path_ref(issue.path), "ChapterDeltaTranslator")
                for issue in world.translation.issues
            )
            if world.gate is not None:
                findings.extend(
                    _Finding(issue.severity, issue.code, issue.message, issue.target_ref, "BookStateReviewGate")
                    for issue in world.gate.issues
                )
        return _verdict([self._issue(finding, names) for finding in findings])

    def _issue(self, finding: _Finding, names: Mapping[str, str]) -> ContinuityIssue:
        error = finding.severity == "error"
        description, entity_names = _named(finding.message or finding.code, names)
        return ContinuityIssue(
            rule_name=finding.code,
            severity="error" if error else "warning",
            description=description,
            entity_names=entity_names,
            reviewer=self.name,
            issue_type=ISSUE_TYPE,
            target_scope="draft",
            issue_group=issue_group_for_issue(issue_type=ISSUE_TYPE),
            evidence_refs=[finding.evidence_ref] if finding.evidence_ref else [],
            suggested_fix=world_repair_fix(finding.code) if error else "",
            source_layer="book_state",
            source_analyzer=finding.source_analyzer,
            blocking=error,
            # The raw message keeps the ids for audit.
            original_result={"code": finding.code, "message": finding.message, "ref": finding.evidence_ref},
        )


def _named(text: str, names: Mapping[str, str]) -> tuple[str, list[str]]:
    """``text`` with every known ref replaced by its display name, and the entity names it used."""
    entity_names: list[str] = []

    def name_of(match: re.Match[str]) -> str:
        ref = match.group(0)
        name = names.get(ref)
        if not name:
            return ref
        if ref.startswith(FACT_REF_PREFIX):
            return f"「{name}」"
        entity_names.append(name)
        return name

    return _REF_TOKEN.sub(name_of, text), list(dict.fromkeys(entity_names))


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
