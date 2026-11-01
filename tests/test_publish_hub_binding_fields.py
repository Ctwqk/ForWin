from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from forwin.api_schema.project import ProjectAutomationPublishSettings
from forwin.canon.outbox_events import (
    CANON_PUBLISHER_REQUESTED,
    CanonPublisherBindingSnapshot,
    build_canon_recovery_events,
    canon_commit_id,
    parse_canon_event_payload,
    publisher_binding_snapshot,
)
from forwin.http.runtime import UPLOAD_TERMINAL_STATUSES
from forwin.models.publisher import UPLOAD_JOB_TERMINAL_STATUSES, PublisherUploadJob
from tests.test_canon_publisher_jobs import _fixture, _materialize

DESTINATION = "3b3c8f1e-0000-4000-8000-000000000002"


def test_publish_settings_default_to_the_legacy_route() -> None:
    settings = ProjectAutomationPublishSettings()

    assert settings.route == "legacy"
    assert settings.hub_destination_id is None
    dumped = settings.model_dump(mode="json")
    assert dumped["route"] == "legacy" and dumped["hub_destination_id"] is None


def test_publish_settings_accept_the_hub_route_and_a_destination() -> None:
    settings = ProjectAutomationPublishSettings(
        platform="fanqie", route="hub", hub_destination_id=f"  {DESTINATION} "
    )

    assert settings.route == "hub"
    assert settings.hub_destination_id == DESTINATION
    assert ProjectAutomationPublishSettings(hub_destination_id="  ").hub_destination_id is None


def test_publish_settings_reject_an_unknown_route() -> None:
    with pytest.raises(ValidationError):
        ProjectAutomationPublishSettings(route="both")


def test_old_stored_settings_without_the_new_fields_still_load() -> None:
    settings = ProjectAutomationPublishSettings.model_validate(
        {"platform": "qidian", "book_name": "旧书", "create_if_missing": True}
    )

    assert (settings.route, settings.hub_destination_id) == ("legacy", None)


def test_canon_snapshot_carries_the_route_and_destination() -> None:
    snapshot = CanonPublisherBindingSnapshot.model_validate(
        {
            "platform": "fanqie",
            "book_name": "雾港",
            "route": "hub",
            "hub_destination_id": DESTINATION,
        }
    )

    assert snapshot.route == "hub" and snapshot.hub_destination_id == DESTINATION
    legacy = CanonPublisherBindingSnapshot.model_validate(
        {"platform": "fanqie", "book_name": "雾港"}
    )
    assert (legacy.route, legacy.hub_destination_id) == ("legacy", None)


def test_canon_snapshot_is_still_strict_about_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CanonPublisherBindingSnapshot.model_validate(
            {"platform": "fanqie", "book_name": "雾港", "routing": "hub"}
        )
    with pytest.raises(ValidationError):
        CanonPublisherBindingSnapshot.model_validate(
            {"platform": "fanqie", "book_name": "雾港", "route": "both"}
        )


def test_route_and_destination_travel_from_project_settings_into_the_outbox_event() -> None:
    automation = {
        "publish_bindings": [
            {
                "platform": "fanqie",
                "book_name": "雾港",
                "route": "hub",
                "hub_destination_id": DESTINATION,
            },
            {"platform": "qidian", "book_name": "雾港"},
        ]
    }

    bindings = publisher_binding_snapshot(automation, default_book_name="雾港")
    events = build_canon_recovery_events(
        canon_idempotency_key="canon-key-1",
        canon_commit_id_value=canon_commit_id("canon-key-1"),
        project_id="p1",
        chapter_number=3,
        candidate_id="cand-1",
        chapter_title="第三章",
        body_sha256="a" * 64,
        publisher_bindings=bindings,
    )
    [publisher_event] = [e for e in events if e.event_type == CANON_PUBLISHER_REQUESTED]
    parsed = parse_canon_event_payload(CANON_PUBLISHER_REQUESTED, publisher_event.payload)

    by_platform = {b.platform: b for b in parsed.publisher_bindings}
    assert (by_platform["fanqie"].route, by_platform["fanqie"].hub_destination_id) == (
        "hub",
        DESTINATION,
    )
    assert (by_platform["qidian"].route, by_platform["qidian"].hub_destination_id) == (
        "legacy",
        None,
    )


def test_an_event_stored_before_the_fields_existed_still_parses() -> None:
    old_binding = {"platform": "fanqie", "book_name": "雾港", "create_if_missing": False}

    parsed = CanonPublisherBindingSnapshot.model_validate(old_binding)

    assert parsed.route == "legacy" and parsed.hub_destination_id is None


def test_the_job_snapshot_of_a_legacy_binding_is_unchanged_and_a_hub_binding_records_its_route() -> None:
    fixture = _fixture("publish-hub-job-snapshot")
    try:
        [legacy] = _materialize(
            fixture,
            bindings=[{"platform": "qidian", "book_name": "起点版"}],
        )
        legacy_binding = legacy["result_payload"]["publisher_binding"]
        # Byte-for-byte what jobs stored before the hub fields existed.
        assert "route" not in legacy_binding
        assert "hub_destination_id" not in legacy_binding

        [hub] = _materialize(
            fixture,
            bindings=[
                {
                    "platform": "fanqie",
                    "book_name": "番茄版",
                    "route": "hub",
                    "hub_destination_id": DESTINATION,
                }
            ],
        )
        hub_binding = hub["result_payload"]["publisher_binding"]
        assert hub_binding["route"] == "hub"
        assert hub_binding["hub_destination_id"] == DESTINATION

        replay = _materialize(
            fixture,
            bindings=[
                {
                    "platform": "fanqie",
                    "book_name": "番茄版",
                    "route": "hub",
                    "hub_destination_id": DESTINATION,
                }
            ],
        )
        assert replay[0]["job_id"] == hub["job_id"], "replaying the same snapshot is idempotent"
    finally:
        fixture.engine.dispose()


def test_superseded_is_a_terminal_upload_status_everywhere() -> None:
    assert UPLOAD_JOB_TERMINAL_STATUSES == frozenset(
        {"succeeded", "failed", "cancelled", "superseded"}
    )
    assert UPLOAD_TERMINAL_STATUSES == UPLOAD_JOB_TERMINAL_STATUSES


def test_a_superseded_row_is_displayed_as_finished_and_can_be_deleted_but_not_terminated() -> None:
    fixture = _fixture("publish-hub-superseded-status")
    try:
        [created] = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, created["job_id"])
            job.status = "superseded"
            job.hub_superseded_reason = "publication_already_published"

        shown = fixture.runtime.upload_jobs.get_upload_job(created["job_id"])
        assert shown["status"] == "superseded"
        assert shown["deletable"] is True and shown["terminable"] is False
        listed = fixture.runtime.upload_jobs.list_upload_jobs(status="superseded")
        assert [item["job_id"] for item in listed] == [created["job_id"]]

        with pytest.raises(ValueError):
            fixture.runtime.upload_jobs.terminate_upload_job(created["job_id"])
        deleted = fixture.runtime.upload_jobs.delete_upload_job(created["job_id"])
        assert deleted["status"] == "superseded"
    finally:
        fixture.engine.dispose()


def test_a_superseded_row_does_not_count_as_an_active_upload() -> None:
    from forwin.http.generation import _project_has_active_upload_job

    fixture = _fixture("publish-hub-superseded-active")
    try:
        [created] = _materialize(fixture)
        with fixture.runtime.session_factory.begin() as session:
            job = session.get(PublisherUploadJob, created["job_id"])
            assert _project_has_active_upload_job(
                SimpleNamespace(session_factory=object()), fixture.project_id, session=session
            ) is True
            job.status = "superseded"
            session.flush()

            assert _project_has_active_upload_job(
                SimpleNamespace(session_factory=object()), fixture.project_id, session=session
            ) is False
    finally:
        fixture.engine.dispose()


def test_a_hub_binding_snapshot_survives_a_json_round_trip() -> None:
    snapshot = CanonPublisherBindingSnapshot.model_validate(
        {"platform": "fanqie", "book_name": "雾港", "route": "hub", "hub_destination_id": DESTINATION}
    )

    again = CanonPublisherBindingSnapshot.model_validate(
        json.loads(json.dumps(snapshot.model_dump(mode="json")))
    )

    assert again == snapshot
