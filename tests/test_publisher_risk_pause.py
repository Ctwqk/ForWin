from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.models.publisher import PublisherOperatorAction, PublisherUploadAttempt
from forwin.publisher_runtime.attempts import PublisherInvalidTransitionError
from tests.test_canon_publisher_jobs import _fixture, _materialize


NOW = datetime(2026, 7, 21, 16, 0, tzinfo=timezone.utc)


def _execute_claim(fixture, *, client_id: str) -> tuple[dict, dict]:
    created = _materialize(fixture)
    released = fixture.runtime.canon_jobs.release(
        project_id=fixture.project_id,
        job_ids=[created[0]["job_id"]],
        publish=False,
        actor_type="scheduler",
    )[0]
    claim = fixture.runtime.attempts.claim(
        client_id=client_id,
        connected_platforms=["qidian"],
        lease_seconds=60,
        now=NOW,
    )
    assert claim is not None
    return released, claim


@pytest.mark.parametrize("risk_reason", ["captcha", "mfa", "account_risk"])
def test_each_pre_mutation_risk_pauses_and_operator_resume_returns_pending(
    risk_reason: str,
) -> None:
    fixture = _fixture(f"publisher-risk-pre-{risk_reason}")
    client_id = f"extension-{risk_reason}"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)

        paused = fixture.runtime.attempts.pause(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            risk_reason=risk_reason,
            current_url="https://write.qq.com/portal/dashboard",
            evidence={"detector": "publisher-risk-v1", "boundary": "pre-mutation"},
            client_observed_at="2026-07-21T16:00:01Z",
            now=NOW + timedelta(seconds=1),
        )

        assert paused["status"] == "paused"
        assert paused["attempt_status"] == "paused"
        assert paused["pause_reason"] == risk_reason
        assert paused["pause_disposition"] == "applied"
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-next",
                connected_platforms=["qidian"],
                now=NOW + timedelta(seconds=2),
            )
            is None
        )

        resumed = fixture.runtime.attempts.resume(
            job_id=job["job_id"],
            expected_pause_reason=risk_reason,
            expected_pause_token=claim["attempt_id"],
            operator_reason="平台挑战已由值班人员手工完成",
            operator_actor_id="basic:release-operator",
            operator_auth_method="basic",
            now=NOW + timedelta(seconds=3),
        )
        next_claim = fixture.runtime.attempts.claim(
            client_id="extension-after-resume",
            connected_platforms=["qidian"],
            lease_seconds=60,
            now=NOW + timedelta(seconds=4),
        )
        assert next_claim is not None
        replay = fixture.runtime.attempts.resume(
            job_id=job["job_id"],
            expected_pause_reason=risk_reason,
            expected_pause_token=claim["attempt_id"],
            operator_reason="平台挑战已由值班人员手工完成",
            operator_actor_id="basic:release-operator",
            operator_auth_method="basic",
            now=NOW + timedelta(seconds=5),
        )

        assert resumed["disposition"] == "applied"
        assert resumed["job"]["status"] == "pending"
        assert resumed["job"]["pause_token"] == ""
        assert resumed["transition"]["old_state"] == "paused"
        assert resumed["transition"]["new_state"] == "pending"
        assert resumed["transition"]["actor"] == "basic:release-operator"
        assert replay["disposition"] == "idempotent"
        assert replay["job"]["status"] == "running"
        assert replay["transition"] == resumed["transition"]

        with fixture.runtime.session_factory() as session:
            events = (
                session.execute(
                    select(DecisionEvent)
                    .where(
                        DecisionEvent.related_object_id == job["job_id"],
                        DecisionEvent.event_type
                        == DecisionEventType.UPLOAD_JOB_RESUMED,
                    )
                    .order_by(DecisionEvent.created_at.asc())
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
            assert events[0].actor_type == "manual_ui"
            assert events[0].actor_id == "basic:release-operator"
            assert events[0].reason == "平台挑战已由值班人员手工完成"
            payload = json.loads(events[0].payload_json)
            assert payload["old_state"] == "paused"
            assert payload["new_state"] == "pending"
            assert payload["pause_reason"] == risk_reason
            assert payload["transitioned_at"].endswith("+00:00")
    finally:
        fixture.engine.dispose()


def test_post_mutation_resume_enters_reconciling_and_wrong_reason_is_rejected() -> None:
    fixture = _fixture("publisher-risk-post-mutation")
    client_id = "extension-post-mutation"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        fixture.runtime.attempts.pause(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            risk_reason="captcha",
            current_url="https://write.qq.com/chaptertmp/1",
            evidence={"boundary": "post-save"},
            now=NOW + timedelta(seconds=2),
        )

        with pytest.raises(PublisherInvalidTransitionError, match="pause reason"):
            fixture.runtime.attempts.resume(
                job_id=job["job_id"],
                expected_pause_reason="mfa",
                expected_pause_token=claim["attempt_id"],
                operator_reason="wrong expected state",
                operator_actor_id="basic:operator",
                operator_auth_method="basic",
                now=NOW + timedelta(seconds=3),
            )

        resumed = fixture.runtime.attempts.resume(
            job_id=job["job_id"],
            expected_pause_reason="captcha",
            expected_pause_token=claim["attempt_id"],
            operator_reason="captcha completed manually",
            operator_actor_id="basic:operator",
            operator_auth_method="basic",
            now=NOW + timedelta(seconds=4),
        )

        assert resumed["job"]["status"] == "reconciling"
        assert resumed["transition"]["attempt_phase"] == "mutation_started"
        with fixture.runtime.session_factory() as session:
            attempt = session.get(PublisherUploadAttempt, claim["attempt_id"])
            assert attempt is not None
            assert attempt.status == "paused"
    finally:
        fixture.engine.dispose()


def test_pause_report_is_idempotent_but_a_stale_attempt_cannot_pause_again() -> None:
    fixture = _fixture("publisher-risk-stale-pause")
    client_id = "extension-risk-replay"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)
        kwargs = {
            "job_id": job["job_id"],
            "attempt_id": claim["attempt_id"],
            "worker_id": client_id,
            "lease_epoch": claim["lease_epoch"],
            "risk_reason": "account_risk",
            "current_url": "https://write.qq.com/portal/dashboard",
            "evidence": {"matched_text": "账号存在风险"},
        }
        first = fixture.runtime.attempts.pause(**kwargs, now=NOW + timedelta(seconds=1))
        replay = fixture.runtime.attempts.pause(
            **kwargs, now=NOW + timedelta(seconds=2)
        )
        assert first["pause_disposition"] == "applied"
        assert replay["pause_disposition"] == "idempotent"

        fixture.runtime.attempts.resume(
            job_id=job["job_id"],
            expected_pause_reason="account_risk",
            expected_pause_token=claim["attempt_id"],
            operator_reason="account review completed",
            operator_actor_id="proxy:oncall@example.com",
            operator_auth_method="trusted_proxy",
            now=NOW + timedelta(seconds=3),
        )
        with pytest.raises(ValueError, match="fence"):
            fixture.runtime.attempts.pause(**kwargs, now=NOW + timedelta(seconds=4))
    finally:
        fixture.engine.dispose()


def test_projectless_resume_still_has_append_only_operator_audit() -> None:
    fixture = _fixture("publisher-risk-projectless-audit")
    try:
        created = fixture.runtime.upload_jobs.create_upload_job(
            project_id="",
            platform="qidian",
            book_name="Unbound Publisher Job",
            chapter_title="Chapter 1",
            body="publisher body",
            upload_url=None,
            publish=False,
            cover_generation_enabled=False,
        )
        claim = fixture.runtime.attempts.claim(
            client_id="extension-projectless",
            connected_platforms=["qidian"],
            lease_seconds=60,
            now=NOW,
        )
        assert claim is not None
        assert claim["job_id"] == created["job_id"]
        fixture.runtime.attempts.pause(
            job_id=created["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="extension-projectless",
            lease_epoch=claim["lease_epoch"],
            risk_reason="mfa",
            evidence={"boundary": "pre-mutation"},
            now=NOW + timedelta(seconds=1),
        )
        fixture.runtime.attempts.resume(
            job_id=created["job_id"],
            expected_pause_reason="mfa",
            expected_pause_token=claim["attempt_id"],
            operator_reason="MFA completed by the on-call operator",
            operator_actor_id="proxy:oncall@example.com",
            operator_auth_method="trusted_proxy",
            now=NOW + timedelta(seconds=2),
        )

        with fixture.runtime.session_factory() as session:
            actions = (
                session.execute(
                    select(PublisherOperatorAction).where(
                        PublisherOperatorAction.upload_job_id == created["job_id"]
                    )
                )
                .scalars()
                .all()
            )
            mirrored_events = (
                session.execute(
                    select(DecisionEvent).where(
                        DecisionEvent.related_object_id == created["job_id"],
                        DecisionEvent.event_type
                        == DecisionEventType.UPLOAD_JOB_RESUMED,
                    )
                )
                .scalars()
                .all()
            )
            assert len(actions) == 1
            assert actions[0].action == "resume"
            assert actions[0].pause_token == claim["attempt_id"]
            assert actions[0].actor_id == "proxy:oncall@example.com"
            assert actions[0].auth_method == "trusted_proxy"
            assert json.loads(actions[0].old_state_json)["status"] == "paused"
            assert json.loads(actions[0].new_state_json)["status"] == "pending"
            assert mirrored_events == []
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize(
    ("phase", "expected_status"),
    [("claimed", "cancelled"), ("mutation_started", "reconciling")],
)
def test_terminating_a_risk_pause_converges_without_operator_deadlock(
    phase: str,
    expected_status: str,
) -> None:
    fixture = _fixture(f"publisher-risk-terminate-{phase}")
    client_id = f"extension-terminate-{phase}"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)
        if phase == "mutation_started":
            fixture.runtime.attempts.transition(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                worker_id=client_id,
                lease_epoch=claim["lease_epoch"],
                phase=phase,
                now=NOW + timedelta(seconds=1),
            )
        fixture.runtime.attempts.pause(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            risk_reason="captcha",
            evidence={"boundary": phase},
            now=NOW + timedelta(seconds=2),
        )

        terminated = fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])

        assert terminated["status"] == expected_status
        assert terminated["abort_requested"] is True
        assert terminated["pause_reason"] == ""
        assert terminated["pause_token"] == ""
        assert terminated["resumable"] is False
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize(
    ("phase", "expected_job_status", "expected_attempt_status"),
    [
        ("claimed", "cancelled", "cancelled"),
        ("mutation_started", "reconciling", "indeterminate"),
    ],
)
def test_risk_report_after_abort_converges_without_reentering_paused(
    phase: str,
    expected_job_status: str,
    expected_attempt_status: str,
) -> None:
    fixture = _fixture(f"publisher-risk-after-abort-{phase}")
    client_id = f"extension-after-abort-{phase}"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)
        if phase == "mutation_started":
            fixture.runtime.attempts.transition(
                job_id=job["job_id"],
                attempt_id=claim["attempt_id"],
                worker_id=client_id,
                lease_epoch=claim["lease_epoch"],
                phase=phase,
                now=NOW + timedelta(seconds=1),
            )
        fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])

        pause_kwargs = {
            "job_id": job["job_id"],
            "attempt_id": claim["attempt_id"],
            "worker_id": client_id,
            "lease_epoch": claim["lease_epoch"],
            "risk_reason": "captcha",
            "evidence": {"boundary": phase},
        }
        converged = fixture.runtime.attempts.pause(
            **pause_kwargs,
            now=NOW + timedelta(seconds=2),
        )
        replay = fixture.runtime.attempts.pause(
            **pause_kwargs,
            now=NOW + timedelta(seconds=3),
        )

        assert converged["status"] == expected_job_status
        assert converged["attempt_status"] == expected_attempt_status
        assert converged["pause_disposition"] == "abort_converged"
        assert converged["pause_token"] == ""
        assert converged["pause_reason"] == ""
        assert converged["abort_requested"] is True
        assert replay["pause_disposition"] == "idempotent"
        assert replay["status"] == expected_job_status
    finally:
        fixture.engine.dispose()


def test_aborted_reconciliation_risk_stays_reconciling() -> None:
    fixture = _fixture("publisher-risk-aborted-reconciliation")
    client_id = "extension-before-termination"
    try:
        job, claim = _execute_claim(fixture, client_id=client_id)
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        fixture.runtime.attempts.pause(
            job_id=job["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id=client_id,
            lease_epoch=claim["lease_epoch"],
            risk_reason="captcha",
            evidence={"boundary": "post-save"},
            now=NOW + timedelta(seconds=2),
        )
        terminated = fixture.runtime.upload_jobs.terminate_upload_job(job["job_id"])
        assert terminated["status"] == "reconciling"

        reconcile_client = "extension-after-termination"
        reconcile_at = NOW + timedelta(days=1)
        reconcile_claim = fixture.runtime.attempts.claim(
            client_id=reconcile_client,
            connected_platforms=["qidian"],
            lease_seconds=60,
            now=reconcile_at,
        )
        assert reconcile_claim is not None
        assert reconcile_claim["execution_mode"] == "reconcile"
        fixture.runtime.attempts.transition(
            job_id=job["job_id"],
            attempt_id=reconcile_claim["attempt_id"],
            worker_id=reconcile_client,
            lease_epoch=reconcile_claim["lease_epoch"],
            phase="observation_started",
            now=reconcile_at + timedelta(seconds=1),
        )

        result = fixture.runtime.upload_jobs.reconcile_upload_job(
            job_id=job["job_id"],
            client_id=reconcile_client,
            attempt_id=reconcile_claim["attempt_id"],
            lease_epoch=reconcile_claim["lease_epoch"],
            outcome="risk_pause",
            receipt=None,
            evidence={"risk_reason": "captcha", "reason": "challenge visible"},
            now=reconcile_at + timedelta(seconds=2),
        )

        assert result["status"] == "reconciling"
        assert result["attempt_status"] == "indeterminate"
        assert result["abort_requested"] is True
        assert result["pause_reason"] == ""
        assert result["pause_token"] == ""
        assert result["resumable"] is False
        assert "risk_pause" not in result["result_payload"]
        assert result["result_payload"]["risk_after_abort"]["risk_reason"] == "captcha"
    finally:
        fixture.engine.dispose()
