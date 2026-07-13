from __future__ import annotations

import json
from datetime import datetime, timedelta

from forwin.application.project_control.support import build_audit_insights
from forwin.audit.events import DecisionEventType
from forwin.audit.gate_ledger import GateLedgerService
from forwin.audit.gate_ledger_report import render_gate_ledger_markdown
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_control import BandCheckpoint
from forwin.models.project import ArcPlanVersion, Project
from tests.postgres import postgres_test_url


def _event(
    *,
    project_id: str,
    event_type: str,
    created_at: datetime,
    outcome: GateOutcome | None = None,
    chapter_number: int = 0,
    band_id: str = "",
    payload: dict | None = None,
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
    causal_root_id: str = "",
) -> DecisionEvent:
    event_payload = dict(payload or {})
    if outcome is not None:
        event_payload = attach_gate_outcome(event_payload, outcome)
    return DecisionEvent(
        id=new_id(),
        project_id=project_id,
        band_id=band_id,
        chapter_number=chapter_number,
        scope="chapter" if chapter_number else "project",
        event_family="evaluation_verdict",
        event_type=event_type,
        actor_type="system",
        payload_json=json.dumps(event_payload, ensure_ascii=False),
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        parent_event_id=parent_event_id,
        causal_root_id=causal_root_id,
        created_at=created_at,
    )


def _metric(report, gate_id: str):
    return next(item for item in report.metrics if item.gate_id == gate_id)


class TestGateLedger:
    def setup_method(self) -> None:
        self.engine = get_engine(postgres_test_url("gate-ledger"))
        init_db(self.engine)
        self.session = get_session_factory(self.engine)()

    def teardown_method(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _project_with_bands(self, title: str) -> tuple[Project, ArcPlanVersion]:
        project = Project(title=title, premise="p", genre="g")
        self.session.add(project)
        self.session.flush()
        arc = ArcPlanVersion(
            project_id=project.id,
            arc_synopsis="arc",
            chapter_start=1,
            chapter_end=20,
        )
        self.session.add(arc)
        self.session.flush()
        self.session.add_all(
            [
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band-1",
                    chapter_start=1,
                    chapter_end=10,
                ),
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band-2",
                    chapter_start=11,
                    chapter_end=20,
                ),
            ]
        )
        self.session.flush()
        return project, arc

    def test_project_report_scans_beyond_old_thousand_event_window(self) -> None:
        project, _arc = self._project_with_bands("full history")
        started = datetime(2026, 1, 1)
        outcome = GateOutcome(
            gate_id="hard_floor",
            responsibility_domain="draft_quality",
            scope="chapter",
            candidate_id="candidate-1-v1",
            chapter_number=1,
            decision="pass",
        )
        self.session.add(
            _event(
                project_id=project.id,
                event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                created_at=started,
                outcome=outcome,
                chapter_number=1,
            )
        )
        self.session.add_all(
            [
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.STAGE_EXITED,
                    created_at=started + timedelta(seconds=index + 1),
                )
                for index in range(1001)
            ]
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )

        hard_floor = _metric(report, "hard_floor")
        assert report.event_count == 1002
        assert len(report.metrics) == 8
        assert hard_floor.opportunities == 1
        assert hard_floor.evaluations == 1
        assert hard_floor.fires == 0

    def test_checkpoint_denominators_are_not_truncated_to_twenty(self) -> None:
        project, arc = self._project_with_bands("checkpoint history")
        self.session.add_all(
            [
                BandCheckpoint(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id=f"auto-{index}",
                    trigger_source="auto_band_end",
                    status="pass",
                )
                for index in range(21)
            ]
            + [
                BandCheckpoint(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id=f"manual-{index}",
                    trigger_source="manual_boundary",
                    boundary_kind="chapter_start",
                    status="pending",
                )
                for index in range(4)
            ]
        )
        duplicate_checkpoint = BandCheckpoint(
            project_id=project.id,
            arc_id=arc.id,
            band_id="auto-0",
            trigger_source="auto_band_end",
            status="pass",
        )
        self.session.add(duplicate_checkpoint)
        self.session.flush()
        self.session.add(
            _event(
                project_id=project.id,
                event_type=DecisionEventType.BAND_CHECKPOINT_HIT,
                created_at=datetime(2026, 1, 2),
                band_id="auto-0",
                related_object_type="band_checkpoint",
                related_object_id=duplicate_checkpoint.id,
                payload={"status": "fail"},
            )
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )

        assert report.checkpoint_count == 26
        band_metric = _metric(report, "band_checkpoint")
        assert band_metric.opportunities == 21
        assert band_metric.unknown_legacy_count == 1
        assert band_metric.fire_rate is None
        assert _metric(report, "manual_checkpoint").opportunities == 4

    def test_each_delegation_request_is_a_distinct_opportunity(self) -> None:
        project, _arc = self._project_with_bands("delegation requests")
        started = datetime(2026, 2, 15)
        events: list[DecisionEvent] = []
        for index in range(2):
            request_id = f"delegation-request-{index}"
            request = _event(
                project_id=project.id,
                event_type=DecisionEventType.GATE_DELEGATION_REQUESTED,
                created_at=started + timedelta(seconds=index * 2),
                outcome=GateOutcome(
                    gate_id="delegation",
                    responsibility_domain="band_checkpoint",
                    scope="band",
                    candidate_id="same-checkpoint",
                    band_id="band-1",
                    evaluated=False,
                    fired=True,
                    decision="reject",
                ),
                band_id="band-1",
                related_object_type="band_checkpoint",
                related_object_id="same-checkpoint",
                causal_root_id="shared-checkpoint-root",
            )
            request.id = request_id
            final = _event(
                project_id=project.id,
                event_type=DecisionEventType.GATE_DELEGATION_DECIDED,
                created_at=started + timedelta(seconds=index * 2 + 1),
                outcome=GateOutcome(
                    gate_id="delegation",
                    responsibility_domain="band_checkpoint",
                    scope="band",
                    candidate_id="same-checkpoint",
                    band_id="band-1",
                    fired=True,
                    decision="approve",
                    overridden_by="spark",
                ),
                band_id="band-1",
                parent_event_id=request_id,
                causal_root_id="shared-checkpoint-root",
            )
            events.extend([request, final])
        self.session.add_all(events)
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        delegation = _metric(report, "delegation")

        assert delegation.opportunities == 2
        assert delegation.evaluations == 2
        assert delegation.approvals == 2

    def test_project_band_and_cross_project_scopes_share_one_query_contract(self) -> None:
        project_a, _arc_a = self._project_with_bands("project a")
        project_b, _arc_b = self._project_with_bands("project b")
        started = datetime(2026, 2, 1)
        rows = [
            (project_a.id, "a-3", 3),
            (project_a.id, "a-13", 13),
            (project_b.id, "b-3", 3),
        ]
        self.session.add_all(
            [
                _event(
                    project_id=project_id,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    created_at=started + timedelta(seconds=index),
                    chapter_number=chapter_number,
                    outcome=GateOutcome(
                        gate_id="hard_floor",
                        responsibility_domain="draft_quality",
                        scope="chapter",
                        candidate_id=candidate_id,
                        chapter_number=chapter_number,
                        decision="pass",
                    ),
                )
                for index, (project_id, candidate_id, chapter_number) in enumerate(rows)
            ]
        )
        self.session.commit()

        service = GateLedgerService(self.session)
        project_report = service.report(scope="project", project_id=project_a.id)
        band_report = service.report(
            scope="band", project_id=project_a.id, band_id="band-1"
        )
        cross_report = service.report(scope="cross_project")

        assert _metric(project_report, "hard_floor").opportunities == 2
        assert _metric(band_report, "hard_floor").opportunities == 1
        assert _metric(cross_report, "hard_floor").opportunities == 3
        assert cross_report.project_count == 2

    def test_unreconstructable_legacy_denominator_is_unknown(self) -> None:
        project, _arc = self._project_with_bands("legacy")
        self.session.add(
            _event(
                project_id=project.id,
                event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
                created_at=datetime(2026, 3, 1),
                chapter_number=7,
                related_object_type="canon_admission_run",
                related_object_id="legacy-run",
                payload={"blocking_reasons": ["legacy signal"]},
            )
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        canon = _metric(report, "canon_quality")

        assert canon.opportunities == "unknown"
        assert canon.unknown_legacy_count == 1
        assert canon.evaluations == 0

    def test_hundred_chapter_replay_counts_legacy_payloads_as_unknown(self) -> None:
        project, _arc = self._project_with_bands("hundred chapter replay")
        started = datetime(2026, 3, 2)
        events: list[DecisionEvent] = []
        for chapter_number in range(1, 101):
            outcome = None
            if chapter_number % 10:
                outcome = GateOutcome(
                    gate_id="hard_floor",
                    responsibility_domain="draft_quality",
                    scope="chapter",
                    candidate_id=f"candidate-{chapter_number}",
                    chapter_number=chapter_number,
                    decision="pass",
                )
            events.append(
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    created_at=started + timedelta(seconds=chapter_number),
                    chapter_number=chapter_number,
                    outcome=outcome,
                )
            )
        self.session.add_all(events)
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        hard_floor = _metric(report, "hard_floor")

        assert hard_floor.opportunities == 100
        assert hard_floor.evaluations == 90
        assert hard_floor.unknown_legacy_count == 10
        assert hard_floor.fire_rate is None

    def test_proxy_metrics_require_identity_and_domain_or_issue_group_match(self) -> None:
        project, arc = self._project_with_bands("proxy matching")
        started = datetime(2026, 4, 1)
        pass_match = GateOutcome(
            gate_id="hard_floor",
            responsibility_domain="draft_quality",
            scope="chapter",
            candidate_id="candidate-match",
            chapter_number=1,
            decision="pass",
            issue_groups=["pacing"],
        )
        pass_mismatch = pass_match.model_copy(
            update={
                "candidate_id": "candidate-mismatch",
                "chapter_number": 2,
                "issue_groups": ["fact_conflict"],
            }
        )
        checkpoint = BandCheckpoint(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band-1",
            trigger_source="auto_band_end",
            boundary_kind="band_end",
            boundary_chapter=10,
            status="overridden",
        )
        self.session.add(checkpoint)
        self.session.flush()
        override = GateOutcome(
            gate_id="band_checkpoint",
            responsibility_domain="band_integrity",
            scope="band",
            candidate_id=checkpoint.id,
            chapter_number=10,
            band_id="band-1",
            fired=True,
            decision="approve",
            overridden_by="manual",
            issue_groups=["fact_conflict"],
        )
        checkpoint_incident = GateOutcome(
            gate_id="band_checkpoint",
            responsibility_domain="band_integrity",
            scope="band",
            candidate_id=checkpoint.id,
            chapter_number=10,
            band_id="band-1",
            fired=True,
            decision="error",
            blocked=True,
            issue_groups=["fact_conflict"],
        )
        self.session.add_all(
            [
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    created_at=started,
                    chapter_number=1,
                    outcome=pass_match,
                ),
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.REPAIR_STARTED,
                    created_at=started + timedelta(seconds=1),
                    chapter_number=1,
                    related_object_id="candidate-match",
                    payload={
                        "candidate_id": "candidate-match",
                        "responsibility_domain": "draft_quality",
                        "issue_groups": ["pacing"],
                    },
                ),
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    created_at=started + timedelta(seconds=2),
                    chapter_number=2,
                    outcome=pass_mismatch,
                ),
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.REPAIR_STARTED,
                    created_at=started + timedelta(seconds=3),
                    chapter_number=2,
                    related_object_id="candidate-mismatch",
                    payload={
                        "candidate_id": "candidate-mismatch",
                        "responsibility_domain": "canon_admission",
                        "issue_groups": ["director_imbalance"],
                    },
                ),
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
                    created_at=started + timedelta(seconds=4),
                    chapter_number=10,
                    band_id="band-1",
                    outcome=override,
                    related_object_type="band_checkpoint",
                    related_object_id=checkpoint.id,
                ),
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                    created_at=started + timedelta(seconds=5),
                    chapter_number=10,
                    band_id="band-1",
                    outcome=checkpoint_incident,
                    related_object_type="band_checkpoint",
                    related_object_id=checkpoint.id,
                ),
            ]
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )

        hard_floor = _metric(report, "hard_floor")
        band = _metric(report, "band_checkpoint")
        assert hard_floor.post_pass_incident_proxy == 1
        assert band.post_override_incident_proxy == 1

    def test_candidate_less_incident_is_not_attributed_to_multiple_candidates(
        self,
    ) -> None:
        project, _arc = self._project_with_bands("ambiguous incident")
        started = datetime(2026, 4, 2)
        for index, candidate_id in enumerate(("candidate-a", "candidate-b")):
            self.session.add(
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    created_at=started + timedelta(seconds=index),
                    chapter_number=3,
                    outcome=GateOutcome(
                        gate_id="hard_floor",
                        responsibility_domain="draft_quality",
                        scope="chapter",
                        candidate_id=candidate_id,
                        chapter_number=3,
                        decision="pass",
                    ),
                )
            )
        self.session.add(
            _event(
                project_id=project.id,
                event_type=DecisionEventType.REPAIR_STARTED,
                created_at=started + timedelta(seconds=3),
                chapter_number=3,
                payload={"responsibility_domain": "draft_quality"},
            )
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )

        assert _metric(report, "hard_floor").post_pass_incident_proxy == 0

    def test_same_timestamp_requires_explicit_causal_order_for_post_proxy(self) -> None:
        project, _arc = self._project_with_bands("same transaction")
        created_at = datetime(2026, 4, 3)
        origin = _event(
            project_id=project.id,
            event_type=DecisionEventType.PULP_BEAT_EVALUATED,
            created_at=created_at,
            chapter_number=4,
            outcome=GateOutcome(
                gate_id="hard_floor",
                responsibility_domain="draft_quality",
                scope="chapter",
                candidate_id="same-transaction-candidate",
                chapter_number=4,
                decision="pass",
            ),
        )
        origin.id = "a-origin"
        incident = _event(
            project_id=project.id,
            event_type=DecisionEventType.REPAIR_STARTED,
            created_at=created_at,
            chapter_number=4,
            payload={
                "candidate_id": "same-transaction-candidate",
                "responsibility_domain": "draft_quality",
            },
        )
        incident.id = "z-incident"
        self.session.add_all([origin, incident])
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        assert _metric(report, "hard_floor").post_pass_incident_proxy == 0

        incident.parent_event_id = origin.id
        self.session.add(incident)
        self.session.commit()
        causal_report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        assert _metric(causal_report, "hard_floor").post_pass_incident_proxy == 1

    def test_markdown_and_compatibility_view_are_derived_from_ledger_history(self) -> None:
        project, _arc = self._project_with_bands("report")
        started = datetime(2026, 5, 1)
        self.session.add(
            _event(
                project_id=project.id,
                event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                created_at=started,
                chapter_number=1,
                payload={"reason": "owner decision"},
            )
        )
        self.session.add_all(
            [
                _event(
                    project_id=project.id,
                    event_type=DecisionEventType.STAGE_EXITED,
                    created_at=started + timedelta(seconds=index + 1),
                )
                for index in range(1001)
            ]
        )
        self.session.commit()

        report = GateLedgerService(self.session).report(
            scope="project", project_id=project.id
        )
        markdown = render_gate_ledger_markdown(report)
        insights = build_audit_insights(self.session, project_id=project.id)

        assert "# Gate Ledger" in markdown
        assert "post_pass_incident_proxy" in markdown
        assert insights.forced_accept_frequency == 1
        assert insights.top_override_rule_types[0] == {
            "name": "forced_accept",
            "count": 1,
        }
