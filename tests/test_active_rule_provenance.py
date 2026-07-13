from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.canon_quality.active_rule_store import (
    ActiveRule,
    CanonQualityActiveRuleStore,
    TriggerQuote,
)
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.rule_provenance import (
    RuleProvenanceService,
    build_rule_handoff_summary,
    render_rule_provenance_markdown,
)
from forwin.models import ChapterPlan, DecisionEvent, Project
from forwin.models.base import Base
from forwin.models.canon_quality import CanonQualitySignalRow


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_active_rule_serializes_project_provenance_and_observing_default() -> None:
    rule = ActiveRule(
        rule_key="reference.archivist",
        summary="馆员仅在本项目观察",
        origin_event_id="event-1",
        origin_project_id="project-1",
        promotion_evidence=["gate-event-1"],
    )

    assert rule.model_dump(mode="json") == {
        "rule_key": "reference.archivist",
        "summary": "馆员仅在本项目观察",
        "valid_from_chapter": 0,
        "valid_until_chapter": None,
        "payload": {},
        "origin_event_id": "event-1",
        "origin_project_id": "project-1",
        "status": "observing",
        "promotion_evidence": ["gate-event-1"],
    }


def test_store_filters_status_and_enforces_explicit_lifecycle() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则生命周期", premise="p", genre="g")
        session.add(project)
        session.flush()
        store = CanonQualityActiveRuleStore(session)
        result = store.register_rule(
            project_id=project.id,
            rule=ActiveRule(
                rule_key="reference.archivist",
                origin_event_id="event-1",
                origin_project_id=project.id,
                valid_from_chapter=3,
            ),
            trigger_quote=TriggerQuote(chapter_number=3, quote="馆员出现。"),
        )

        assert result.applied is True
        assert {
            row.status
            for row in session.query(CanonQualitySignalRow).all()
        } == {"resolved"}
        assert CanonQualityRepository(session).list_open_signals(project.id) == []
        assert store.query_active_as_of(
            project_id=project.id, chapter_number=3
        ) == []
        activated = store.transition_status(
            project_id=project.id,
            rule_key="reference.archivist",
            chapter_number=3,
            status="active",
            reason="owner approved true positive",
            evidence_refs=["gate-event-1"],
        )
        assert activated.applied is True
        assert [
            rule.rule_key
            for rule in store.query_active_as_of(
                project_id=project.id, chapter_number=3
            )
        ] == ["reference.archivist"]
        invalid = store.transition_status(
            project_id=project.id,
            rule_key="reference.archivist",
            chapter_number=4,
            status="retired",
            reason="cannot skip suspension",
        )
        assert invalid.applied is False
        assert invalid.reason == "invalid_status_transition"
        assert (
            store.query_active_as_of(project_id="another-project", chapter_number=3)
            == []
        )
    finally:
        session.close()
        engine.dispose()


def test_store_rejects_cross_project_origin() -> None:
    engine, session = _session()
    try:
        project = Project(title="规则来源", premise="p", genre="g")
        session.add(project)
        session.flush()

        result = CanonQualityActiveRuleStore(session).register_rule(
            project_id=project.id,
            rule=ActiveRule(
                rule_key="wrong-origin",
                origin_project_id="another-project",
            ),
            trigger_quote=TriggerQuote(chapter_number=1, quote="证据"),
        )

        assert result.applied is False
        assert result.reason == "origin_project_mismatch"
    finally:
        session.close()
        engine.dispose()


def test_rule_provenance_recommends_but_never_applies_global_promotion() -> None:
    engine, session = _session()
    try:
        projects = [
            Project(title="规则证据一", premise="p", genre="g"),
            Project(title="规则证据二", premise="p", genre="g"),
        ]
        session.add_all(projects)
        session.flush()
        for index, project in enumerate(projects, start=1):
            event = DecisionEvent(
                id=f"gate-evidence-{index}",
                project_id=project.id,
                chapter_number=5,
                scope="chapter",
                event_family="evaluation_verdict",
                event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                actor_type="system",
                payload_json=json.dumps(
                    attach_gate_outcome(
                        {},
                        GateOutcome(
                            gate_id="hard_floor",
                            responsibility_domain="draft_quality",
                            scope="chapter",
                            candidate_id=f"candidate-{index}",
                            chapter_number=5,
                            evaluated=True,
                            fired=True,
                            decision="block",
                            blocked=True,
                            issue_keys=["reference.archivist"],
                        ),
                    )
                ),
            )
            session.add(event)
            CanonQualityActiveRuleStore(session).register_rule(
                project_id=project.id,
                rule=ActiveRule(
                    rule_key="reference.archivist",
                    origin_event_id=f"origin-{index}",
                    origin_project_id=project.id,
                    promotion_evidence=[event.id],
                ),
                trigger_quote=TriggerQuote(
                    chapter_number=4,
                    quote="馆员是本项目泛称。",
                    source_ref=f"origin-{index}",
                ),
            )
        session.flush()

        report = RuleProvenanceService(session).report()

        assert report.project_count == 2
        assert len(report.project_rules) == 2
        assert all(item.status == "observing" for item in report.project_rules)
        actions = [item.action for item in report.recommendations]
        assert actions.count("activate") == 2
        assert actions.count("global_promotion_recommended") == 1
        promotion = next(
            item
            for item in report.recommendations
            if item.action == "global_promotion_recommended"
        )
        assert set(promotion.project_ids) == {project.id for project in projects}
        assert promotion.required_actions == [
            "frozen_fixture_per_project",
            "static_registry_change",
            "owner_review",
            "full_suite",
        ]
        assert all(
            rule.status == "observing"
            for project in projects
            for rule in CanonQualityActiveRuleStore(session).query_rules_as_of(
                project_id=project.id,
                chapter_number=10,
            )
        )
        markdown = render_rule_provenance_markdown(report)
        assert "global_promotion_recommended" in markdown
        assert "reference.archivist" in markdown
        handoff = build_rule_handoff_summary(
            session,
            project_id=projects[0].id,
        )
        assert handoff["project_observing_rules"] == ["reference.archivist"]
        assert handoff["external_project_rules"] == [
            {
                "rule_key": "reference.archivist",
                "origin_project_id": projects[1].id,
                "source_status": "observing",
                "effective_status": "observing_for_target_project",
            }
        ]
        external_store = CanonQualityActiveRuleStore(session)
        external_store.transition_status(
            project_id=projects[1].id,
            rule_key="reference.archivist",
            chapter_number=6,
            status="active",
            reason="owner activation",
        )
        external_store.transition_status(
            project_id=projects[1].id,
            rule_key="reference.archivist",
            chapter_number=7,
            status="suspended",
            reason="owner suspension",
        )
        external_store.transition_status(
            project_id=projects[1].id,
            rule_key="reference.archivist",
            chapter_number=8,
            status="retired",
            reason="lesson frozen as fixture",
        )
        assert build_rule_handoff_summary(
            session,
            project_id=projects[0].id,
        )["external_project_rules"] == []
    finally:
        session.close()
        engine.dispose()


def test_global_promotion_requires_matching_rule_semantics() -> None:
    engine, session = _session()
    try:
        projects = [
            Project(title="同名规则一", premise="p", genre="g"),
            Project(title="同名规则二", premise="p", genre="g"),
        ]
        session.add_all(projects)
        session.flush()
        for index, project in enumerate(projects, start=1):
            event = DecisionEvent(
                id=f"semantic-evidence-{index}",
                project_id=project.id,
                chapter_number=5,
                scope="chapter",
                event_family="evaluation_verdict",
                event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                actor_type="system",
                payload_json=json.dumps(
                    attach_gate_outcome(
                        {},
                        GateOutcome(
                            gate_id="hard_floor",
                            responsibility_domain="draft_quality",
                            scope="chapter",
                            chapter_number=5,
                            evaluated=True,
                            fired=True,
                            decision="block",
                            blocked=True,
                            issue_keys=["reference.same_key"],
                        ),
                    )
                ),
            )
            session.add(event)
            CanonQualityActiveRuleStore(session).register_rule(
                project_id=project.id,
                rule=ActiveRule(
                    rule_key="reference.same_key",
                    summary="不同语义规则",
                    payload={"meaning": index},
                    origin_project_id=project.id,
                    promotion_evidence=[event.id],
                ),
                trigger_quote=TriggerQuote(chapter_number=4, quote="项目证据"),
            )
        session.flush()

        report = RuleProvenanceService(session).report()

        assert not any(
            item.action == "global_promotion_recommended"
            for item in report.recommendations
        )
    finally:
        session.close()
        engine.dispose()


def test_rule_provenance_recommends_suspension_after_300_chapters_without_hit() -> None:
    engine, session = _session()
    try:
        project = Project(
            title="长期无命中规则",
            premise="p",
            genre="g",
            target_total_chapters=500,
        )
        session.add(project)
        session.flush()
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id=project.id,
            rule=ActiveRule(
                rule_key="reference.never_fires",
                origin_project_id=project.id,
                valid_from_chapter=1,
            ),
            trigger_quote=TriggerQuote(chapter_number=1, quote="证据"),
        )
        store.transition_status(
            project_id=project.id,
            rule_key="reference.never_fires",
            chapter_number=1,
            status="active",
            reason="owner activation",
        )
        session.add(
            ChapterPlan(
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=301,
                status="accepted",
            )
        )
        session.flush()

        report = RuleProvenanceService(session).report(project_id=project.id)

        recommendation = next(
            item
            for item in report.recommendations
            if item.rule_key == "reference.never_fires"
        )
        assert recommendation.action == "suspend"
        assert recommendation.recommended_status == "suspended"
        assert store.list_rules(project_id=project.id)[0].status == "active"
    finally:
        session.close()
        engine.dispose()


def test_rule_provenance_recommends_retirement_after_suspended_book_length() -> None:
    engine, session = _session()
    try:
        project = Project(
            title="整书未复活规则",
            premise="p",
            genre="g",
            target_total_chapters=20,
        )
        session.add(project)
        session.flush()
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id=project.id,
            rule=ActiveRule(
                rule_key="reference.retire_candidate",
                origin_project_id=project.id,
                valid_from_chapter=1,
            ),
            trigger_quote=TriggerQuote(chapter_number=1, quote="证据"),
        )
        store.transition_status(
            project_id=project.id,
            rule_key="reference.retire_candidate",
            chapter_number=1,
            status="active",
            reason="owner activation",
        )
        store.transition_status(
            project_id=project.id,
            rule_key="reference.retire_candidate",
            chapter_number=2,
            status="suspended",
            reason="owner suspension",
        )
        session.add(
            ChapterPlan(
                project_id=project.id,
                arc_plan_id="arc-1",
                chapter_number=22,
                status="accepted",
            )
        )
        session.flush()

        report = RuleProvenanceService(session).report(project_id=project.id)

        recommendation = next(
            item
            for item in report.recommendations
            if item.rule_key == "reference.retire_candidate"
        )
        assert recommendation.action == "retire"
        assert recommendation.recommended_status == "retired"
        assert store.list_rules(project_id=project.id)[0].status == "suspended"
    finally:
        session.close()
        engine.dispose()
