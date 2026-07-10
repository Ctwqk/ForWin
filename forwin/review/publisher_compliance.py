from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict


@dataclass(frozen=True)
class PublisherComplianceRule:
    rule_name: str
    severity: str
    pattern: re.Pattern[str]
    description: str
    suggested_fix: str


class PublisherComplianceReviewer:
    def __init__(self, *, platform_ids: list[str] | None = None) -> None:
        self.platform_ids = [
            str(item or "").strip()
            for item in (platform_ids or ["qidian", "fanqie"])
            if str(item or "").strip()
        ]
        self.rules = (
            PublisherComplianceRule(
                rule_name="publisher_compliance_external_contact",
                severity="error",
                pattern=re.compile(
                    r"(加\s*微\s*信|微\s*信|vx\s*[:：]?\s*[A-Za-z0-9_-]{4,}|"
                    r"Q\s*Q\s*群|QQ\s*[:：]?\s*\d{5,}|加\s*群|读者群)",
                    re.IGNORECASE,
                ),
                description="正文包含外部联系方式或引流群信息，平台发布前需要删除或改写。",
                suggested_fix="删除外部联系方式、读者群、微信/VX/QQ 等引流信息。",
            ),
            PublisherComplianceRule(
                rule_name="publisher_compliance_external_link",
                severity="error",
                pattern=re.compile(r"(https?://|www\.)", re.IGNORECASE),
                description="正文包含外部链接，平台发布前需要删除或改写。",
                suggested_fix="删除外部链接，必要信息改成站内允许的普通叙述。",
            ),
            PublisherComplianceRule(
                rule_name="publisher_compliance_phone_number",
                severity="error",
                pattern=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
                description="正文包含疑似手机号，平台发布前需要删除或改写。",
                suggested_fix="删除手机号或改成非真实联系方式的剧情表达。",
            ),
            PublisherComplianceRule(
                rule_name="publisher_compliance_promotional_cta",
                severity="warning",
                pattern=re.compile(r"(求收藏|求推荐票|求追读|求打赏|点个关注)"),
                description="正文包含作者运营话术，建议移出章节正文，避免影响平台审核或读者体验。",
                suggested_fix="将运营话术移出正文，或改为平台允许的作者有话说区域。",
            ),
        )

    def review(self, _context, writer_output, **_kwargs) -> ReviewVerdict:
        text_parts = [
            str(getattr(writer_output, "title", "") or ""),
            str(getattr(writer_output, "body", "") or ""),
        ]
        generation_meta = getattr(writer_output, "generation_meta", {}) or {}
        if isinstance(generation_meta, dict):
            for key in ("intro", "book_intro", "publisher_intro"):
                value = str(generation_meta.get(key) or "").strip()
                if value:
                    text_parts.append(value)
        text = "\n".join(text_parts)
        issues = list(self._scan(text))
        verdict = (
            "fail"
            if any(issue.severity == "error" for issue in issues)
            else "warn" if issues else "pass"
        )
        return ReviewVerdict(
            verdict=verdict,
            issues=issues,
            recommended_action=(
                "rewrite"
                if verdict == "fail"
                else "pause_for_review" if verdict == "warn" else "continue"
            ),
            review_summary="平台发布合规检查发现风险。" if issues else "",
            repair_instruction=(
                RepairInstruction(
                    repair_scope="draft",
                    failure_type="mixed",
                    must_fix=[issue.description for issue in issues if issue.severity == "error"],
                    scope_reason="平台发布合规问题应通过正文改写修复。",
                    evidence_refs=[
                        ref
                        for issue in issues
                        for ref in issue.evidence_refs
                    ],
                )
                if any(issue.severity == "error" for issue in issues)
                else None
            ),
        )

    def _scan(self, text: str) -> Iterable[ContinuityIssue]:
        for rule in self.rules:
            match = rule.pattern.search(text)
            if not match:
                continue
            matched_text = match.group(0)
            yield ContinuityIssue(
                rule_name=rule.rule_name,
                severity=rule.severity,  # type: ignore[arg-type]
                description=rule.description,
                reviewer="publisher_compliance",
                issue_type="publisher_compliance",
                target_scope="draft",
                issue_group="publisher_compliance",
                evidence_refs=[
                    f"publisher:{platform}:body:{rule.rule_name}"
                    for platform in self.platform_ids
                ],
                suggested_fix=rule.suggested_fix,
                source_layer="reviewer",
                source_analyzer="publisher_compliance",
                original_verdict="fail" if rule.severity == "error" else "warn",
                blocking=rule.severity == "error",
                original_result={"matched_text": matched_text},
            )
