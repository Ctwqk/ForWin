from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from forwin.api_schema import ProjectAutomationUpdateRequest
from forwin.api_schema.project import ProjectAutomationPublishSettings
from forwin.application.project_control.support import persist_project_automation
from forwin.application.projects.generation import update_project_automation
from forwin.application.read_models import normalize_project_automation
from forwin.canon.outbox_events import (
    CANON_PUBLISHER_REQUESTED,
    CanonPublisherBindingSnapshot,
    build_canon_recovery_events,
    canon_commit_id,
    parse_canon_event_payload,
    publisher_binding_snapshot,
)
from forwin.http.runtime import UPLOAD_TERMINAL_STATUSES
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import UPLOAD_JOB_TERMINAL_STATUSES, PublisherUploadJob
from tests.postgres import postgres_test_url
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


def test_snapshot_builder_reads_route_and_destination_from_a_raw_automation_dict() -> None:
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


def test_the_job_snapshot_keeps_the_shape_it_always_had_and_carries_no_routing() -> None:
    # The release point reads the project's current settings, so a copy of the route in the
    # frozen snapshot could only go stale.
    fixture = _fixture("publish-hub-job-snapshot")
    try:
        [plain] = _materialize(
            fixture, bindings=[{"platform": "qidian", "book_name": "起点版"}]
        )
        hub_binding = {
            "platform": "fanqie",
            "book_name": "番茄版",
            "route": "hub",
            "hub_destination_id": DESTINATION,
        }
        [hub] = _materialize(fixture, bindings=[hub_binding])

        for job in (plain, hub):
            snapshot = job["result_payload"]["publisher_binding"]
            assert "route" not in snapshot
            assert "hub_destination_id" not in snapshot
        assert set(plain["result_payload"]["publisher_binding"]) == set(
            hub["result_payload"]["publisher_binding"]
        )
        replay = _materialize(fixture, bindings=[hub_binding])
        assert replay[0]["job_id"] == hub["job_id"], "replaying the same snapshot is idempotent"
        changed = _materialize(
            fixture, bindings=[{**hub_binding, "hub_destination_id": "another-destination"}]
        )
        assert changed[0]["job_id"] == hub["job_id"], "re-pointing the route is not a new job"
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


HUB_BINDINGS = [
    {
        "platform": "fanqie",
        "book_name": "番茄书名",
        "route": "hub",
        "hub_destination_id": DESTINATION,
    },
    {"platform": "qidian", "book_name": "起点书名"},
]


def _by_platform(automation) -> dict:
    return {binding.platform: binding for binding in automation.publish_bindings}


def test_normalize_project_automation_keeps_route_and_destination() -> None:
    automation = normalize_project_automation(
        {"publish": HUB_BINDINGS[0], "publish_bindings": HUB_BINDINGS}
    )

    assert (automation.publish.route, automation.publish.hub_destination_id) == (
        "hub",
        DESTINATION,
    )
    bindings = _by_platform(automation)
    assert (bindings["fanqie"].route, bindings["fanqie"].hub_destination_id) == (
        "hub",
        DESTINATION,
    )
    assert (bindings["qidian"].route, bindings["qidian"].hub_destination_id) == (
        "legacy",
        None,
    )


def test_normalize_project_automation_reads_stored_json_with_the_new_fields() -> None:
    stored = json.dumps({"publish_bindings": HUB_BINDINGS}, ensure_ascii=False)

    automation = normalize_project_automation(stored)

    assert _by_platform(automation)["fanqie"].route == "hub"
    assert _by_platform(automation)["fanqie"].hub_destination_id == DESTINATION
    assert normalize_project_automation(automation.model_dump(mode="json")) == automation


@pytest.mark.parametrize(
    ("raw_route", "raw_destination", "route", "destination"),
    [
        ("hub", f"  {DESTINATION}  ", "hub", DESTINATION),
        ("HUB", DESTINATION, "hub", DESTINATION),
        (" hub ", None, "hub", None),
        ("hub", "   ", "hub", None),
        ("hub", "", "hub", None),
        ("legacy", DESTINATION, "legacy", DESTINATION),
        ("both", DESTINATION, "legacy", DESTINATION),
        ("", None, "legacy", None),
        (None, None, "legacy", None),
        (3, 7, "legacy", "7"),
    ],
)
def test_normalize_project_automation_cleans_route_and_destination(
    raw_route, raw_destination, route, destination
) -> None:
    automation = normalize_project_automation(
        {
            "publish_bindings": [
                {
                    "platform": "fanqie",
                    "route": raw_route,
                    "hub_destination_id": raw_destination,
                }
            ]
        }
    )

    [binding] = automation.publish_bindings
    assert (binding.route, binding.hub_destination_id) == (route, destination)


def test_settings_without_the_new_fields_normalize_to_the_default_route() -> None:
    automation = normalize_project_automation(
        {"publish_bindings": [{"platform": "qidian", "book_name": "旧书"}]}
    )

    [binding] = automation.publish_bindings
    assert (binding.route, binding.hub_destination_id) == ("legacy", None)


def _project_in_database(name: str, automation: dict | None = None):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    session_factory = get_session_factory(engine)
    project_id = new_id()
    with session_factory.begin() as session:
        session.add(
            Project(
                id=project_id,
                title="路由测试",
                premise="验证 route 能被保存。",
                genre="悬疑",
                automation_json=json.dumps(automation or {}, ensure_ascii=False),
            )
        )
    return engine, session_factory, project_id


def test_persist_project_automation_keeps_route_and_destination() -> None:
    engine, session_factory, project_id = _project_in_database("publish-hub-persist")
    try:
        with session_factory.begin() as session:
            project = session.get(Project, project_id)
            stored = persist_project_automation(
                session,
                project,
                normalize_project_automation({"publish_bindings": HUB_BINDINGS}),
            )
            assert _by_platform(stored)["fanqie"].route == "hub"

        with session_factory() as session:
            raw = session.get(Project, project_id).automation_json
        saved = json.loads(raw)["publish_bindings"]
        assert {b["platform"]: (b["route"], b["hub_destination_id"]) for b in saved} == {
            "fanqie": ("hub", DESTINATION),
            "qidian": ("legacy", None),
        }
        reloaded = normalize_project_automation(raw)
        assert _by_platform(reloaded)["fanqie"].hub_destination_id == DESTINATION
    finally:
        engine.dispose()


def test_update_project_automation_saves_and_changes_route_and_destination() -> None:
    engine, session_factory, project_id = _project_in_database("publish-hub-update")
    try:
        def update(bindings, publish=None):
            return update_project_automation(
                project_id,
                ProjectAutomationUpdateRequest.model_validate(
                    {"enabled": True, "publish": publish, "publish_bindings": bindings}
                ),
                get_session=session_factory,
                persist_project_automation=persist_project_automation,
            )

        response = update(HUB_BINDINGS)

        assert _by_platform(response.automation)["fanqie"].route == "hub"
        with session_factory() as session:
            saved = normalize_project_automation(session.get(Project, project_id).automation_json)
        assert (_by_platform(saved)["fanqie"].route, _by_platform(saved)["fanqie"].hub_destination_id) == (
            "hub",
            DESTINATION,
        )

        # An update that leaves the fields out keeps what is stored (Task 11, R28): it does not
        # put the binding back on the default route. The primary `publish` entry wins over a
        # same-platform binding, as it always has, so the request sends both the way the
        # settings form does. Taking the platform off the hub is refused (R31); the guards have
        # their own tests in test_automation_publish_hub_guards.py.
        plain = {"platform": "fanqie", "book_name": "番茄书名"}
        update([plain], publish=plain)
        with session_factory() as session:
            kept = normalize_project_automation(session.get(Project, project_id).automation_json)
        assert (_by_platform(kept)["fanqie"].route, _by_platform(kept)["fanqie"].hub_destination_id) == (
            "hub",
            DESTINATION,
        )
    finally:
        engine.dispose()


def test_update_project_automation_rejects_an_unknown_route() -> None:
    with pytest.raises(ValidationError):
        ProjectAutomationUpdateRequest.model_validate(
            {"publish_bindings": [{"platform": "fanqie", "route": "both"}]}
        )


def test_route_and_destination_travel_from_saved_settings_into_the_outbox_event() -> None:
    """Settings update -> stored automation_json -> event snapshot, with no step skipped."""
    engine, session_factory, project_id = _project_in_database("publish-hub-end-to-end")
    try:
        update_project_automation(
            project_id,
            ProjectAutomationUpdateRequest.model_validate(
                {"enabled": True, "auto_publish": True, "publish_bindings": HUB_BINDINGS}
            ),
            get_session=session_factory,
            persist_project_automation=persist_project_automation,
        )
        with session_factory() as session:
            automation_json = session.get(Project, project_id).automation_json

        bindings = publisher_binding_snapshot(automation_json, default_book_name="雾港")
        events = build_canon_recovery_events(
            canon_idempotency_key="canon-key-1",
            canon_commit_id_value=canon_commit_id("canon-key-1"),
            project_id=project_id,
            chapter_number=3,
            candidate_id="cand-1",
            chapter_title="第三章",
            body_sha256="a" * 64,
            publisher_bindings=bindings,
        )
        [event] = [e for e in events if e.event_type == CANON_PUBLISHER_REQUESTED]
        parsed = parse_canon_event_payload(CANON_PUBLISHER_REQUESTED, event.payload)

        by_platform = {b.platform: b for b in parsed.publisher_bindings}
        assert (by_platform["fanqie"].route, by_platform["fanqie"].hub_destination_id) == (
            "hub",
            DESTINATION,
        )
        assert by_platform["qidian"].route == "legacy"
    finally:
        engine.dispose()
