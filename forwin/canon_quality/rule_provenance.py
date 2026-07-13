from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.checker.reference_classifier import reference_rule_catalog
from forwin.models.audit import DecisionEvent
from forwin.models.canon_quality import CanonQualitySignalRow
from forwin.models.project import ChapterPlan, Project

from .active_rule_store import (
    ActiveRule,
    ActiveRuleStatus,
    CanonQualityActiveRuleStore,
)

RuleScope = Literal["global", "genre_candidate", "project"]
RecommendationAction = Literal[
    "activate",
    "suspend",
    "retire",
    "global_promotion_recommended",
]


class RuleProvenanceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_key: str
    summary: str = ""
    scope: RuleScope
    status: ActiveRuleStatus
    origin_project_id: str = ""
    origin_event_id: str = ""
    valid_from_chapter: int = 0
    valid_until_chapter: int | None = None
    promotion_evidence: list[str] = Field(default_factory=list)
    code_owner: str = ""


class RuleRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_key: str
    action: RecommendationAction
    project_ids: list[str] = Field(default_factory=list)
    current_status: ActiveRuleStatus | None = None
    recommended_status: ActiveRuleStatus | None = None
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)


class RuleProvenanceReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    project_id: str = ""
    project_count: int = 0
    global_code_backed_rules: list[RuleProvenanceEntry] = Field(
        default_factory=list
    )
    genre_rule_candidates: list[RuleProvenanceEntry] = Field(default_factory=list)
    project_rules: list[RuleProvenanceEntry] = Field(default_factory=list)
    recommendations: list[RuleRecommendation] = Field(default_factory=list)


@dataclass(slots=True)
class _RuleGateStats:
    evaluations: int = 0
    fires: int = 0
    overrides: int = 0
    event_ids: list[str] = field(default_factory=list)
    true_positive_chapters: list[int] = field(default_factory=list)

    @property
    def override_rate(self) -> float | None:
        if self.evaluations <= 0:
            return None
        return self.overrides / self.evaluations


class RuleProvenanceService:
    def __init__(self, session) -> None:  # noqa: ANN001
        self.session = session

    def report(self, *, project_id: str = "") -> RuleProvenanceReport:
        project_filter = str(project_id or "").strip()
        project_rows = list(
            self.session.execute(
                select(Project.id, Project.target_total_chapters)
            ).all()
        )
        all_project_ids = sorted(
            str(row.id)
            for row in project_rows
            if str(row.id or "").strip()
        )
        project_targets = {
            str(row.id): max(0, int(row.target_total_chapters or 0))
            for row in project_rows
        }
        accepted_chapters = {
            str(row.project_id): int(row.latest_chapter or 0)
            for row in self.session.execute(
                select(
                    ChapterPlan.project_id,
                    func.max(ChapterPlan.chapter_number).label("latest_chapter"),
                )
                .where(ChapterPlan.status == "accepted")
                .group_by(ChapterPlan.project_id)
            ).all()
        }
        selected_project_ids = [
            value
            for value in all_project_ids
            if not project_filter or value == project_filter
        ]
        store = CanonQualityActiveRuleStore(self.session)
        rules_by_project = {
            value: store.list_rules(project_id=value) for value in all_project_ids
        }
        all_rule_rows = [
            (row_project_id, rule)
            for row_project_id, rules in rules_by_project.items()
            for rule in rules
        ]
        selected_rule_rows = [
            (row_project_id, rule)
            for row_project_id, rule in all_rule_rows
            if not project_filter or row_project_id == project_filter
        ]
        events = list(
            self.session.scalars(
                select(DecisionEvent).order_by(
                    DecisionEvent.created_at.asc(), DecisionEvent.id.asc()
                )
            ).all()
        )
        event_by_id = {str(row.id or ""): row for row in events}
        evidence_by_rule = self._validated_evidence(
            all_rule_rows,
            event_by_id=event_by_id,
        )
        gate_stats_by_rule = self._rule_gate_stats(all_rule_rows, events=events)
        suspended_at_by_rule = self._suspended_at_by_rule()
        recommendations = self._recommendations(
            selected_rule_rows=selected_rule_rows,
            all_rule_rows=all_rule_rows,
            evidence_by_rule=evidence_by_rule,
            gate_stats_by_rule=gate_stats_by_rule,
            accepted_chapters=accepted_chapters,
            project_targets=project_targets,
            suspended_at_by_rule=suspended_at_by_rule,
            include_all_global_promotions=not bool(project_filter),
        )
        catalog = reference_rule_catalog()
        return RuleProvenanceReport(
            project_id=project_filter,
            project_count=len(selected_project_ids),
            global_code_backed_rules=[
                RuleProvenanceEntry(
                    rule_key=item.rule_key,
                    summary=item.summary,
                    scope="global",
                    status="active",
                    code_owner="forwin.checker.reference_classifier",
                )
                for item in catalog
                if item.scope == "global"
            ],
            genre_rule_candidates=[
                RuleProvenanceEntry(
                    rule_key=item.rule_key,
                    summary=item.summary,
                    scope="genre_candidate",
                    status="observing",
                    code_owner="forwin.checker.reference_classifier",
                )
                for item in catalog
                if item.scope == "genre_candidate"
            ],
            project_rules=[
                _project_entry(row_project_id, rule)
                for row_project_id, rule in selected_rule_rows
            ],
            recommendations=recommendations,
        )

    @staticmethod
    def _validated_evidence(
        rule_rows: list[tuple[str, ActiveRule]],
        *,
        event_by_id: dict[str, DecisionEvent],
    ) -> dict[tuple[str, str], list[tuple[str, str]]]:
        validated: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for project_id, rule in rule_rows:
            expected_keys = {
                rule.rule_key,
                str(rule.payload.get("issue_key") or "").strip(),
                str(rule.payload.get("issue_group") or "").strip(),
            }
            expected_keys.discard("")
            for raw_ref in rule.promotion_evidence:
                event_id = _event_id(raw_ref)
                event = event_by_id.get(event_id)
                if event is None or str(event.project_id or "") != project_id:
                    continue
                outcome = parse_gate_outcome(_json_object(event.payload_json))
                if (
                    outcome is None
                    or not outcome.evaluated
                    or not outcome.fired
                    or bool(outcome.overridden_by)
                ):
                    continue
                outcome_keys = {
                    *[str(value).strip() for value in outcome.issue_keys],
                    *[str(value).strip() for value in outcome.issue_groups],
                }
                if expected_keys.isdisjoint(outcome_keys):
                    continue
                validated[(project_id, rule.rule_key)].append(
                    (event_id, outcome.gate_id)
                )
        return validated

    @staticmethod
    def _rule_gate_stats(
        rule_rows: list[tuple[str, ActiveRule]],
        *,
        events: list[DecisionEvent],
    ) -> dict[tuple[str, str], _RuleGateStats]:
        rules_by_evidence_key: dict[tuple[str, str], set[str]] = defaultdict(set)
        for project_id, rule in rule_rows:
            for evidence_key in (
                rule.rule_key,
                str(rule.payload.get("issue_key") or "").strip(),
                str(rule.payload.get("issue_group") or "").strip(),
            ):
                if evidence_key:
                    rules_by_evidence_key[(project_id, evidence_key)].add(
                        rule.rule_key
                    )
        stats: dict[tuple[str, str], _RuleGateStats] = defaultdict(_RuleGateStats)
        for event in events:
            project_id = str(event.project_id or "")
            outcome = parse_gate_outcome(_json_object(event.payload_json))
            if outcome is None:
                continue
            outcome_keys = {
                *[str(value).strip() for value in outcome.issue_keys],
                *[str(value).strip() for value in outcome.issue_groups],
            }
            matched_rule_keys = {
                rule_key
                for outcome_key in outcome_keys
                for rule_key in rules_by_evidence_key.get(
                    (project_id, outcome_key),
                    set(),
                )
            }
            for rule_key in matched_rule_keys:
                metric = stats[(project_id, rule_key)]
                metric.evaluations += int(outcome.evaluated)
                metric.fires += int(outcome.fired)
                metric.overrides += int(bool(outcome.overridden_by))
                metric.event_ids.append(str(event.id or ""))
                if outcome.evaluated and outcome.fired and not outcome.overridden_by:
                    metric.true_positive_chapters.append(
                        int(outcome.chapter_number or event.chapter_number or 0)
                    )
        return stats

    def _suspended_at_by_rule(self) -> dict[tuple[str, str], int]:
        rows = list(
            self.session.scalars(
                select(CanonQualitySignalRow)
                .where(
                    CanonQualitySignalRow.signal_type
                    == CanonQualityActiveRuleStore.STATUS_CHANGED
                )
                .order_by(
                    CanonQualitySignalRow.chapter_number.asc(),
                    CanonQualitySignalRow.created_at.asc(),
                    CanonQualitySignalRow.id.asc(),
                )
            ).all()
        )
        suspended_at: dict[tuple[str, str], int] = {}
        for row in rows:
            payload = _json_object(row.payload_json)
            key = (str(row.project_id or ""), str(row.subject_key or ""))
            if payload.get("to_status") == "suspended":
                suspended_at[key] = int(row.chapter_number or 0)
            elif payload.get("to_status") == "active":
                suspended_at.pop(key, None)
        return suspended_at

    def _recommendations(
        self,
        *,
        selected_rule_rows: list[tuple[str, ActiveRule]],
        all_rule_rows: list[tuple[str, ActiveRule]],
        evidence_by_rule: dict[tuple[str, str], list[tuple[str, str]]],
        gate_stats_by_rule: dict[tuple[str, str], _RuleGateStats],
        accepted_chapters: dict[str, int],
        project_targets: dict[str, int],
        suspended_at_by_rule: dict[tuple[str, str], int],
        include_all_global_promotions: bool,
    ) -> list[RuleRecommendation]:
        recommendations: list[RuleRecommendation] = []

        for project_id, rule in selected_rule_rows:
            evidence = evidence_by_rule.get((project_id, rule.rule_key), [])
            gate_stats = gate_stats_by_rule.get((project_id, rule.rule_key))
            override_rate = gate_stats.override_rate if gate_stats is not None else None
            evidence_refs = [event_id for event_id, _gate_id in evidence]
            latest_accepted = accepted_chapters.get(project_id, 0)
            latest_true_positive = max(
                [
                    int(rule.valid_from_chapter or 0),
                    *(gate_stats.true_positive_chapters if gate_stats else []),
                ]
            )
            if (
                rule.status == "observing"
                and evidence
                and override_rate is not None
                and override_rate < 0.5
            ):
                recommendations.append(
                    RuleRecommendation(
                        rule_key=rule.rule_key,
                        action="activate",
                        project_ids=[project_id],
                        current_status=rule.status,
                        recommended_status="active",
                        reason="validated true positive with override rate below 50%",
                        evidence_refs=evidence_refs,
                        required_actions=["owner_review"],
                    )
                )
            elif (
                rule.status == "active"
                and override_rate is not None
                and override_rate > 0.7
            ):
                recommendations.append(
                    RuleRecommendation(
                        rule_key=rule.rule_key,
                        action="suspend",
                        project_ids=[project_id],
                        current_status=rule.status,
                        recommended_status="suspended",
                        reason="matched gate override rate exceeds 70%",
                        evidence_refs=evidence_refs,
                        required_actions=["owner_review"],
                    )
                )
            elif (
                rule.status == "active"
                and latest_accepted - latest_true_positive >= 300
            ):
                recommendations.append(
                    RuleRecommendation(
                        rule_key=rule.rule_key,
                        action="suspend",
                        project_ids=[project_id],
                        current_status=rule.status,
                        recommended_status="suspended",
                        reason="no matched true positive in 300 accepted chapters",
                        evidence_refs=evidence_refs,
                        required_actions=["owner_review"],
                    )
                )
            elif rule.status == "suspended":
                suspended_at = suspended_at_by_rule.get(
                    (project_id, rule.rule_key)
                )
                book_length = project_targets.get(project_id, 0)
                if (
                    suspended_at is not None
                    and book_length > 0
                    and latest_accepted - suspended_at >= book_length
                ):
                    recommendations.append(
                        RuleRecommendation(
                            rule_key=rule.rule_key,
                            action="retire",
                            project_ids=[project_id],
                            current_status=rule.status,
                            recommended_status="retired",
                            reason=(
                                "rule remained suspended for one configured book length"
                            ),
                            evidence_refs=evidence_refs,
                            required_actions=[
                                "freeze_lesson_as_fixture",
                                "owner_review",
                            ],
                        )
                    )

        evidence_projects: dict[tuple[str, str], set[str]] = defaultdict(set)
        evidence_refs_by_identity: dict[tuple[str, str], list[str]] = defaultdict(
            list
        )
        for project_id, rule in all_rule_rows:
            evidence = evidence_by_rule.get((project_id, rule.rule_key), [])
            if not evidence:
                continue
            identity = (rule.rule_key, _rule_semantic_fingerprint(rule))
            evidence_projects[identity].add(project_id)
            evidence_refs_by_identity[identity].extend(
                event_id for event_id, _gate_id in evidence
            )
        selected_rule_keys = {rule.rule_key for _project_id, rule in selected_rule_rows}
        for (rule_key, fingerprint), project_ids in sorted(evidence_projects.items()):
            if len(project_ids) < 2:
                continue
            if (
                not include_all_global_promotions
                and rule_key not in selected_rule_keys
            ):
                continue
            recommendations.append(
                RuleRecommendation(
                    rule_key=rule_key,
                    action="global_promotion_recommended",
                    project_ids=sorted(project_ids),
                    reason="validated true positives exist in at least two projects",
                    evidence_refs=list(
                        dict.fromkeys(
                            evidence_refs_by_identity[(rule_key, fingerprint)]
                        )
                    ),
                    required_actions=[
                        "frozen_fixture_per_project",
                        "static_registry_change",
                        "owner_review",
                        "full_suite",
                    ],
                )
            )
        return sorted(
            recommendations,
            key=lambda item: (item.rule_key, item.action, item.project_ids),
        )


def build_rule_handoff_summary(session, *, project_id: str) -> dict[str, object]:
    report = RuleProvenanceService(session).report()
    current_rules = [
        item for item in report.project_rules if item.origin_project_id == project_id
    ]
    by_status = {
        status: [
            item.rule_key for item in current_rules if item.status == status
        ]
        for status in ("observing", "active", "suspended", "retired")
    }
    return {
        "global_code_backed_rules": [
            item.rule_key for item in report.global_code_backed_rules
        ],
        "genre_rule_candidates": [
            item.rule_key for item in report.genre_rule_candidates
        ],
        "project_observing_rules": by_status["observing"],
        "project_active_rules": by_status["active"],
        "project_suspended_rules": by_status["suspended"],
        "project_retired_rules": by_status["retired"],
        "external_project_rules": [
            {
                "rule_key": item.rule_key,
                "origin_project_id": item.origin_project_id,
                "source_status": item.status,
                "effective_status": "observing_for_target_project",
            }
            for item in report.project_rules
            if item.origin_project_id != project_id and item.status != "retired"
        ],
        "promotion_recommendations": [
            item.model_dump(mode="json")
            for item in report.recommendations
            if item.action == "global_promotion_recommended"
        ],
    }


def render_rule_provenance_markdown(report: RuleProvenanceReport) -> str:
    lines = [
        "# Rule Provenance Report",
        "",
        f"- Project: `{report.project_id or 'all'}`",
        f"- Projects: {report.project_count}",
        "",
        "| rule | scope | status | origin project | origin event |",
        "|---|---|---|---|---|",
    ]
    for item in [
        *report.global_code_backed_rules,
        *report.genre_rule_candidates,
        *report.project_rules,
    ]:
        lines.append(
            f"| {item.rule_key} | {item.scope} | {item.status} | "
            f"{item.origin_project_id} | {item.origin_event_id} |"
        )
    lines.extend(
        [
            "",
            "| rule | recommendation | projects | reason | required actions |",
            "|---|---|---|---|---|",
        ]
    )
    for item in report.recommendations:
        lines.append(
            f"| {item.rule_key} | {item.action} | {', '.join(item.project_ids)} | "
            f"{item.reason} | {', '.join(item.required_actions)} |"
        )
    return "\n".join(lines) + "\n"


def _project_entry(project_id: str, rule: ActiveRule) -> RuleProvenanceEntry:
    return RuleProvenanceEntry(
        rule_key=rule.rule_key,
        summary=rule.summary,
        scope="project",
        status=rule.status,
        origin_project_id=project_id,
        origin_event_id=rule.origin_event_id,
        valid_from_chapter=rule.valid_from_chapter,
        valid_until_chapter=rule.valid_until_chapter,
        promotion_evidence=rule.promotion_evidence,
    )


def _json_object(raw: str | None) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_id(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("decision_event:", "event:"):
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def _rule_semantic_fingerprint(rule: ActiveRule) -> str:
    payload = json.dumps(
        {
            "rule_key": rule.rule_key,
            "summary": str(rule.summary or "").strip(),
            "payload": rule.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "RuleProvenanceEntry",
    "RuleProvenanceReport",
    "RuleProvenanceService",
    "RuleRecommendation",
    "build_rule_handoff_summary",
    "render_rule_provenance_markdown",
]
