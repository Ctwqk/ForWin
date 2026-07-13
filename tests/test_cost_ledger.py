from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from tests.postgres import postgres_test_url


def _dimension(report, dimension: str, value: str):
    return next(
        item
        for item in report.dimensions
        if item.dimension == dimension and item.value == value
    )


def test_review_acceptance_propagates_transport_actor_taxonomy() -> None:
    source = (
        Path(__file__).parents[1] / "forwin/generation/pipeline_core/acceptance.py"
    ).read_text(encoding="utf-8")
    application_source = (
        Path(__file__).parents[1] / "forwin/application/projects/reviews.py"
    ).read_text(encoding="utf-8")
    marker = "event_type=DecisionEventType.REVIEW_APPROVED"
    call_site = source[source.index(marker) : source.index(marker) + 500]

    assert "actor_type=actor_type" in call_site
    assert 'actor_type="api"' in application_source


class TestCostLedger:
    def setup_method(self) -> None:
        self.engine = get_engine(postgres_test_url("cost-ledger"))
        init_db(self.engine)
        self.session = get_session_factory(self.engine)()

    def teardown_method(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _project(self) -> Project:
        project = Project(title="Cost Book", premise="p", genre="g")
        self.session.add(project)
        self.session.flush()
        return project

    def test_report_aggregates_attempts_dimensions_and_gate_cost(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        started = datetime(2026, 6, 1, 12, 0, 0)
        gate = DecisionEvent(
            id="gate-event",
            project_id=project.id,
            band_id="band-1",
            chapter_number=3,
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
                        candidate_id="candidate-3",
                        chapter_number=3,
                        band_id="band-1",
                        decision="pass",
                    ),
                )
            ),
            created_at=started,
        )
        child = DecisionEvent(
            id="trace-source",
            project_id=project.id,
            band_id="band-1",
            chapter_number=3,
            scope="chapter",
            event_family="runtime_observation",
            event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
            actor_type="system",
            parent_event_id=gate.id,
            payload_json="{}",
            created_at=started + timedelta(seconds=1),
        )
        trace = PromptTrace(
            id="trace-3",
            project_id=project.id,
            decision_event_id=child.id,
            trace_scope="writer",
            stage_key="chapter_draft",
            input_snapshot_json=json.dumps({"chapter_number": 3}),
            model_profile_json=json.dumps({"provider": "openai_compatible"}),
            attempts_json=json.dumps(
                [
                    {
                        "attempt_group_id": "group-3",
                        "attempt_no": 1,
                        "model": "model-a",
                        "provider": "openai_compatible",
                        "task_family": "writer",
                        "stage_key": "chapter_draft",
                        "http_status": 529,
                        "input_chars": 100,
                        "output_chars": 0,
                        "prompt_tokens": 25,
                        "completion_tokens": 0,
                        "total_tokens": 25,
                        "usage_source": "provider",
                        "duration_ms": 50,
                        "error_class": "HTTPStatusError",
                        "retryable": True,
                    },
                    {
                        "attempt_group_id": "group-3",
                        "attempt_no": 2,
                        "model": "model-b",
                        "provider": "openai_compatible",
                        "task_family": "writer",
                        "stage_key": "chapter_draft",
                        "http_status": 200,
                        "input_chars": 100,
                        "output_chars": 50,
                        "prompt_tokens": 25,
                        "completion_tokens": 10,
                        "total_tokens": 35,
                        "usage_source": "provider",
                        "duration_ms": 100,
                    },
                ]
            ),
            fallback_used=True,
            created_at=started + timedelta(seconds=2),
        )
        self.session.add_all([gate, child, trace])
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert report.trace_count == 1
        assert report.totals.attempts == 2
        assert report.totals.successes == 1
        assert report.totals.retries == 1
        assert report.totals.fallbacks == 1
        assert report.totals.input_chars == 200
        assert report.totals.output_chars == 50
        assert report.totals.prompt_tokens == 50
        assert report.totals.completion_tokens == 10
        assert report.totals.total_tokens == 60
        assert report.totals.duration_ms == 150
        assert _dimension(report, "chapter", "3").metrics.attempts == 2
        assert _dimension(report, "band", "band-1").metrics.total_tokens == 60
        assert _dimension(report, "candidate", "candidate-3").metrics.retries == 1
        assert _dimension(report, "model", "model-b").metrics.successes == 1
        assert report.gate_costs[0].gate_id == "hard_floor"
        assert report.gate_costs[0].metrics.total_tokens == 60

    def test_report_counts_manual_actions_without_inventing_duration(self) -> None:
        from forwin.observability.cost_ledger import (
            CostLedgerService,
            render_cost_ledger_markdown,
        )

        project = self._project()
        started = datetime(2026, 6, 2, 12, 0, 0)
        rows = [
            DecisionEvent(
                id=new_id(),
                project_id=project.id,
                chapter_number=4,
                scope="chapter",
                event_type=DecisionEventType.REVIEW_APPROVED,
                actor_type="manual_ui",
                actor_id="owner-1",
                payload_json=json.dumps(
                    {"duration_ms": 1200, "source": "review_panel"}
                ),
                created_at=started,
            ),
            DecisionEvent(
                id=new_id(),
                project_id=project.id,
                chapter_number=4,
                scope="chapter",
                event_family="audit_action",
                event_type=DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
                actor_type="api",
                actor_id="operator-api",
                payload_json=json.dumps(
                    {
                        "started_at": "2026-06-02T12:00:01Z",
                        "finished_at": "2026-06-02T12:00:02Z",
                        "source": "mcp",
                    }
                ),
                created_at=started + timedelta(minutes=10),
            ),
            DecisionEvent(
                id=new_id(),
                project_id=project.id,
                chapter_number=4,
                scope="chapter",
                event_type="comment_resolved",
                actor_type="extension",
                actor_id="browser-extension",
                payload_json=json.dumps({"source": "publisher", "manual_action": True}),
                created_at=started + timedelta(minutes=20),
            ),
            DecisionEvent(
                id=new_id(),
                project_id=project.id,
                chapter_number=4,
                scope="chapter",
                event_type=DecisionEventType.UPLOAD_JOB_CLAIMED,
                actor_type="extension",
                actor_id="browser-extension",
                payload_json=json.dumps({"duration_ms": 5000, "source": "publisher"}),
                created_at=started + timedelta(minutes=25),
            ),
            DecisionEvent(
                id=new_id(),
                project_id=project.id,
                chapter_number=4,
                scope="chapter",
                event_type="automatic_retry",
                actor_type="worker",
                payload_json=json.dumps({"duration_ms": 9999}),
                created_at=started + timedelta(minutes=30),
            ),
        ]
        self.session.add_all(rows)
        self.session.commit()

        report = CostLedgerService(self.session).report(
            project_id=project.id, chapter_number=4
        )

        assert report.manual_action_count == 3
        assert report.manual_action_duration_ms == 2200
        assert report.unknown_manual_duration_count == 1
        assert {item.actor_type for item in report.manual_actions} == {
            "manual_ui",
            "api",
            "extension",
        }
        extension = next(
            item for item in report.manual_actions if item.actor_type == "extension"
        )
        assert extension.actor_id == "browser-extension"
        assert extension.source == "publisher"
        assert extension.unknown_duration_count == 1
        markdown = render_cost_ledger_markdown(report)
        assert "| action type | actor type | actor id | source |" in markdown
        assert "review_approved" in markdown
        assert "unknown duration" in markdown

    def test_band_filter_includes_manual_event_derived_from_chapter_range(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        arc = ArcPlanVersion(
            project_id=project.id,
            arc_synopsis="arc",
            chapter_start=1,
            chapter_end=5,
        )
        self.session.add(arc)
        self.session.flush()
        self.session.add_all(
            [
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="band-manual",
                    chapter_start=1,
                    chapter_end=5,
                ),
                DecisionEvent(
                    project_id=project.id,
                    chapter_number=4,
                    scope="chapter",
                    event_type=DecisionEventType.REVIEW_APPROVED,
                    actor_type="manual_ui",
                    actor_id="owner",
                    payload_json="{}",
                ),
            ]
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(
            project_id=project.id,
            band_id="band-manual",
        )

        assert report.event_count == 1
        assert report.manual_action_count == 1

    def test_cross_backend_fallback_is_retry_attributed_to_destination(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        self.session.add(
            PromptTrace(
                project_id=project.id,
                trace_scope="writer",
                stage_key="chapter_draft",
                attempts_json=json.dumps(
                    [
                        {
                            "attempt_group_id": "ordinary-group",
                            "attempt_no": 1,
                            "provider": "openai_compatible",
                            "model": "ordinary-model",
                            "http_status": 503,
                            "error_class": "HTTPStatusError",
                            "usage_source": "missing",
                        },
                        {
                            "attempt_group_id": "codex-group",
                            "attempt_no": 1,
                            "provider": "codex_bridge",
                            "model": "codex-model",
                            "http_status": 200,
                            "output_chars": 20,
                            "usage_source": "missing",
                        },
                    ]
                ),
                fallback_used=True,
            )
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert report.totals.retries == 1
        assert report.totals.fallbacks == 1
        assert _dimension(report, "provider", "openai_compatible").metrics.fallbacks == 0
        destination = _dimension(report, "provider", "codex_bridge").metrics
        assert destination.retries == 1
        assert destination.fallbacks == 1

    def test_parse_retry_counts_across_distinct_route_groups(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        self.session.add(
            PromptTrace(
                project_id=project.id,
                trace_scope="canon_quality",
                stage_key="chapter_review_form",
                attempts_json=json.dumps(
                    [
                        {
                            "attempt_group_id": "schema-call-1",
                            "parse_error": "invalid schema",
                            "http_status": 200,
                            "output_chars": 20,
                        },
                        {
                            "attempt_group_id": "schema-call-2",
                            "http_status": 200,
                            "output_chars": 30,
                        },
                    ]
                ),
            )
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert report.totals.attempts == 2
        assert report.totals.successes == 1
        assert report.totals.retries == 1

    def test_report_exposes_missing_usage_without_estimating_provider_tokens(
        self,
    ) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        self.session.add(
            PromptTrace(
                project_id=project.id,
                trace_scope="genesis",
                stage_key="world",
                attempts_json=json.dumps(
                    [
                        {
                            "attempt_group_id": "missing-usage",
                            "attempt_no": 1,
                            "model": "provider-model",
                            "provider": "openai_compatible",
                            "input_chars": 80,
                            "output_chars": 20,
                            "duration_ms": 70,
                            "usage_source": "missing",
                        }
                    ]
                ),
            )
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert report.totals.attempts == 1
        assert report.totals.input_chars == 80
        assert report.totals.total_tokens == 0
        assert report.totals.missing_usage_attempts == 1

    def test_report_uses_explicit_trace_refs_and_chapter_band_membership(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        arc = ArcPlanVersion(
            project_id=project.id,
            arc_synopsis="arc",
            chapter_start=1,
            chapter_end=10,
        )
        self.session.add(arc)
        self.session.flush()
        self.session.add(
            BandExperiencePlan(
                project_id=project.id,
                arc_id=arc.id,
                band_id="band-derived",
                chapter_start=5,
                chapter_end=8,
            )
        )
        trace = PromptTrace(
            id="trace-explicit",
            project_id=project.id,
            trace_scope="review",
            stage_key="chapter_review",
            input_snapshot_json=json.dumps({"chapter_number": 6}),
            attempts_json=json.dumps(
                [
                    {
                        "attempt_group_id": "explicit-ref",
                        "attempt_no": 1,
                        "model": "review-model",
                        "provider": "openai_compatible",
                        "output_chars": 12,
                        "total_tokens": 9,
                        "usage_source": "provider",
                    }
                ]
            ),
        )
        gate = DecisionEvent(
            id="explicit-gate",
            project_id=project.id,
            chapter_number=6,
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
                        candidate_id="candidate-explicit",
                        chapter_number=6,
                        decision="pass",
                        trace_ids=[trace.id],
                    ),
                )
            ),
        )
        generic_trace_event = DecisionEvent(
            id="generic-trace-event",
            project_id=project.id,
            chapter_number=6,
            scope="chapter",
            event_family="runtime_observation",
            event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
            actor_type="system",
            related_object_type="prompt_trace",
            related_object_id=trace.id,
            payload_json="{}",
        )
        self.session.add_all([trace, gate, generic_trace_event])
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert _dimension(report, "band", "band-derived").metrics.attempts == 1
        assert (
            _dimension(report, "candidate", "candidate-explicit").metrics.attempts == 1
        )
        assert report.gate_costs[0].gate_id == "hard_floor"

    def test_child_trace_inherits_gate_context_from_parent_trace(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        gate = DecisionEvent(
            id="parent-gate",
            project_id=project.id,
            chapter_number=9,
            scope="chapter",
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            actor_type="system",
            payload_json=json.dumps(
                attach_gate_outcome(
                    {},
                    GateOutcome(
                        gate_id="canon_quality",
                        responsibility_domain="canon_admission",
                        scope="chapter",
                        candidate_id="candidate-parent",
                        chapter_number=9,
                        decision="warn",
                    ),
                )
            ),
        )
        parent = PromptTrace(
            id="parent-trace",
            project_id=project.id,
            decision_event_id=gate.id,
            trace_scope="review",
            stage_key="chapter_review",
            attempts_json="[]",
        )
        child = PromptTrace(
            id="child-trace",
            project_id=project.id,
            parent_trace_id=parent.id,
            trace_scope="repair",
            stage_key="chapter_rewrite",
            attempts_json=json.dumps(
                [
                    {
                        "attempt_group_id": "child-cost",
                        "attempt_no": 1,
                        "model": "repair-model",
                        "provider": "openai_compatible",
                        "output_chars": 20,
                        "duration_ms": 30,
                        "usage_source": "missing",
                    }
                ]
            ),
        )
        self.session.add_all([gate, parent, child])
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert _dimension(report, "candidate", "candidate-parent").metrics.attempts == 1
        assert report.gate_costs[0].gate_id == "canon_quality"

    def test_review_event_resolves_candidate_by_review_id(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        arc = ArcPlanVersion(
            project_id=project.id,
            arc_synopsis="arc",
            chapter_start=1,
            chapter_end=3,
        )
        self.session.add(arc)
        self.session.flush()
        plan = ChapterPlan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=2,
        )
        self.session.add(plan)
        self.session.flush()
        draft = ChapterDraft(chapter_plan_id=plan.id, body_text="draft")
        self.session.add(draft)
        self.session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="warn")
        self.session.add(review)
        self.session.flush()
        candidate = CandidateDraftRecord(
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=2,
            candidate_draft_id=draft.id,
            review_id=review.id,
        )
        event = DecisionEvent(
            project_id=project.id,
            chapter_number=2,
            scope="chapter",
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            actor_type="system",
            related_object_type="chapter_review",
            related_object_id=review.id,
            payload_json="{}",
        )
        self.session.add_all([candidate, event])
        self.session.flush()
        self.session.add(
            PromptTrace(
                project_id=project.id,
                decision_event_id=event.id,
                trace_scope="review",
                stage_key="chapter_review",
                attempts_json=json.dumps(
                    [
                        {
                            "attempt_group_id": "review-candidate",
                            "attempt_no": 1,
                            "model": "review-model",
                            "provider": "openai_compatible",
                            "output_chars": 10,
                            "usage_source": "missing",
                        }
                    ]
                ),
            )
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert _dimension(report, "candidate", candidate.id).metrics.attempts == 1

    def test_review_child_propagates_candidate_to_unique_writer_parent(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
        self.session.add(arc)
        self.session.flush()
        plan = ChapterPlan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=2,
        )
        self.session.add(plan)
        self.session.flush()
        draft = ChapterDraft(chapter_plan_id=plan.id, body_text="draft")
        self.session.add(draft)
        self.session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="warn")
        self.session.add(review)
        self.session.flush()
        candidate = CandidateDraftRecord(
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=2,
            candidate_draft_id=draft.id,
            review_id=review.id,
        )
        event = DecisionEvent(
            project_id=project.id,
            chapter_number=2,
            scope="chapter",
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            actor_type="system",
            related_object_type="chapter_review",
            related_object_id=review.id,
            payload_json="{}",
        )
        writer_trace = PromptTrace(
            project_id=project.id,
            trace_scope="writer",
            stage_key="chapter_draft",
            attempts_json=json.dumps(
                [{"attempt_group_id": "writer", "http_status": 200}]
            ),
        )
        self.session.add_all([candidate, event, writer_trace])
        self.session.flush()
        review_trace = PromptTrace(
            project_id=project.id,
            parent_trace_id=writer_trace.id,
            decision_event_id=event.id,
            trace_scope="review",
            stage_key="chapter_review",
            attempts_json=json.dumps(
                [{"attempt_group_id": "review", "http_status": 200}]
            ),
        )
        self.session.add(review_trace)
        self.session.commit()

        report = CostLedgerService(self.session).report(
            project_id=project.id,
            candidate_id=candidate.id,
        )

        assert report.trace_count == 2
        assert report.totals.attempts == 2

    def test_band_derivation_uses_the_chapter_plan_arc_after_replan(self) -> None:
        from forwin.observability.cost_ledger import CostLedgerService

        project = self._project()
        old_arc = ArcPlanVersion(project_id=project.id, arc_synopsis="old")
        active_arc = ArcPlanVersion(project_id=project.id, arc_synopsis="active")
        self.session.add_all([old_arc, active_arc])
        self.session.flush()
        self.session.add_all(
            [
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=old_arc.id,
                    band_id="old-band",
                    chapter_start=1,
                    chapter_end=5,
                ),
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=active_arc.id,
                    band_id="active-band",
                    chapter_start=1,
                    chapter_end=5,
                ),
                ChapterPlan(
                    project_id=project.id,
                    arc_plan_id=active_arc.id,
                    chapter_number=3,
                    status="accepted",
                ),
                PromptTrace(
                    project_id=project.id,
                    trace_scope="writer",
                    stage_key="chapter_draft",
                    input_snapshot_json=json.dumps({"chapter_number": 3}),
                    attempts_json=json.dumps(
                        [{"attempt_group_id": "writer", "http_status": 200}]
                    ),
                ),
            ]
        )
        self.session.commit()

        report = CostLedgerService(self.session).report(project_id=project.id)

        assert _dimension(report, "band", "active-band").metrics.attempts == 1
        assert not any(
            item.dimension == "band" and item.value == "old-band"
            for item in report.dimensions
        )
