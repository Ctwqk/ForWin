"""Task 11: the project automation settings keep a hub-routed platform on publish-hub.

Spec 11.2 leaves no switch-back path: a platform that went to publish-hub is not put back on
the legacy route by a setting. Two things used to be able to do that by accident or by hand:

* the home form saves `publish` without `route` / `hub_destination_id`, which read as "the
  default route" and moved the platform back to legacy (R28: an omitted field keeps what is
  stored; the form also sends both back);
* anyone could PUT `route: "legacy"` (R31: refused with a 400 that points at 11.2; clearing the
  destination of a hub binding is refused too, since a hub route without one only holds chapters).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forwin.api_schema import ProjectAutomationUpdateRequest
from forwin.application.project_control.support import persist_project_automation
from forwin.application.projects.generation import update_project_automation
from forwin.application.read_models import normalize_project_automation
from forwin.config import InfrastructureConfig
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import PublisherUploadJob
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url

DESTINATION = "3b3c8f1e-0000-4000-8000-000000000002"
OTHER_DESTINATION = "3b3c8f1e-0000-4000-8000-000000000003"

HUB_FANQIE = {
    "platform": "fanqie",
    "book_name": "番茄书名",
    "route": "hub",
    "hub_destination_id": DESTINATION,
}
LEGACY_QIDIAN = {"platform": "qidian", "book_name": "起点书名"}


class _Database:
    def __init__(self, name: str, stored: dict) -> None:
        self.engine = get_engine(postgres_test_url(name))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        self.project_id = new_id()
        with self.session_factory.begin() as session:
            session.add(
                Project(
                    id=self.project_id,
                    title="路由保护测试",
                    premise="验证 publish-hub 路由不会被设置悄悄改回。",
                    genre="悬疑",
                    automation_json=json.dumps(stored, ensure_ascii=False),
                )
            )

    def dispose(self) -> None:
        self.engine.dispose()

    def update(self, body: dict):
        return update_project_automation(
            self.project_id,
            ProjectAutomationUpdateRequest.model_validate({"enabled": True, **body}),
            get_session=self.session_factory,
            persist_project_automation=persist_project_automation,
        )

    def stored(self):
        with self.session_factory() as session:
            return normalize_project_automation(session.get(Project, self.project_id).automation_json)

    def stored_json(self) -> str:
        with self.session_factory() as session:
            return session.get(Project, self.project_id).automation_json

    def routing(self) -> dict[str, tuple[str, str | None]]:
        return {
            binding.platform: (binding.route, binding.hub_destination_id)
            for binding in self.stored().publish_bindings
        }


@pytest.fixture()
def hub_project(request):
    """A project with fanqie on publish-hub (primary) and qidian on the legacy route."""
    database = _Database(
        f"hub-guard-{request.node.name}"[:48],
        {"publish": HUB_FANQIE, "publish_bindings": [HUB_FANQIE, LEGACY_QIDIAN]},
    )
    try:
        yield database
    finally:
        database.dispose()


def _refusal(call) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value


# --- R28: an omitted field keeps what is stored -------------------------------------------


def test_the_home_form_save_without_routing_fields_keeps_the_platform_on_the_hub(hub_project) -> None:
    # What the home form sent before it round-tripped the fields: the primary entry, no route.
    hub_project.update(
        {"publish": {"platform": "fanqie", "book_name": "改过的番茄书名", "create_if_missing": True}}
    )

    stored = hub_project.stored()
    assert hub_project.routing()["fanqie"] == ("hub", DESTINATION)
    assert (stored.publish.route, stored.publish.hub_destination_id) == ("hub", DESTINATION)
    assert stored.publish.book_name == "改过的番茄书名" and stored.publish.create_if_missing is True
    assert hub_project.routing()["qidian"] == ("legacy", None)


def test_a_binding_entry_without_routing_fields_keeps_that_platforms_routing() -> None:
    database = _Database(
        "hub-guard-binding-entry",
        {"publish": LEGACY_QIDIAN, "publish_bindings": [LEGACY_QIDIAN, HUB_FANQIE]},
    )
    try:
        database.update(
            {
                "publish_bindings": [
                    {"platform": "qidian", "book_name": "起点书名"},
                    {"platform": "fanqie", "book_name": "番茄新书名"},
                ]
            }
        )

        assert database.routing() == {"qidian": ("legacy", None), "fanqie": ("hub", DESTINATION)}
        assert {b.platform: b.book_name for b in database.stored().publish_bindings}["fanqie"] == (
            "番茄新书名"
        )
    finally:
        database.dispose()


def test_each_omitted_field_is_kept_on_its_own(hub_project) -> None:
    hub_project.update(
        {"publish": {"platform": "fanqie", "book_name": "甲", "route": "hub"}}
    )
    assert hub_project.routing()["fanqie"] == ("hub", DESTINATION)

    hub_project.update(
        {"publish": {"platform": "fanqie", "book_name": "乙", "hub_destination_id": OTHER_DESTINATION}}
    )
    assert hub_project.routing()["fanqie"] == ("hub", OTHER_DESTINATION)


def test_a_save_with_no_publish_entries_at_all_changes_no_routing(hub_project) -> None:
    hub_project.update({"daily_chapter_quota": 3})

    assert hub_project.routing() == {"fanqie": ("hub", DESTINATION), "qidian": ("legacy", None)}


def test_explicit_values_still_win_hub_to_hub_and_legacy_to_hub(hub_project) -> None:
    hub_project.update({"publish": {**HUB_FANQIE, "hub_destination_id": OTHER_DESTINATION}})
    assert hub_project.routing()["fanqie"] == ("hub", OTHER_DESTINATION)

    hub_project.update(
        {
            "publish": {**HUB_FANQIE, "hub_destination_id": OTHER_DESTINATION},
            "publish_bindings": [
                {**HUB_FANQIE, "hub_destination_id": OTHER_DESTINATION},
                {**LEGACY_QIDIAN, "route": "hub", "hub_destination_id": DESTINATION},
            ],
        }
    )
    assert hub_project.routing() == {
        "fanqie": ("hub", OTHER_DESTINATION),
        "qidian": ("hub", DESTINATION),
    }


def test_a_save_that_leaves_routing_out_of_both_entries_changes_nothing(hub_project) -> None:
    hub_project.update(
        {
            "publish": {"platform": "fanqie", "book_name": "番茄书名"},
            "publish_bindings": [
                {"platform": "fanqie", "book_name": "番茄书名"},
                {"platform": "qidian", "book_name": "起点书名"},
            ],
        }
    )
    assert hub_project.routing() == {"fanqie": ("hub", DESTINATION), "qidian": ("legacy", None)}


def test_changing_the_primary_platform_does_not_hand_it_another_platforms_destination() -> None:
    database = _Database(
        "hub-guard-platform-change",
        {"publish": HUB_FANQIE, "publish_bindings": [HUB_FANQIE]},
    )
    try:
        database.update({"publish": {"platform": "qidian", "book_name": "起点书名"}})

        assert database.routing() == {"qidian": ("legacy", None), "fanqie": ("hub", DESTINATION)}
        assert database.stored().publish.platform == "qidian"
    finally:
        database.dispose()


# --- R31: no way back to legacy, no hub binding without its destination -------------------


def test_switching_a_hub_platform_back_to_legacy_is_refused_with_a_pointer_to_the_spec(hub_project) -> None:
    before = hub_project.stored_json()

    error = _refusal(
        lambda: hub_project.update(
            {"enabled": False, "publish": {"platform": "fanqie", "book_name": "番茄书名", "route": "legacy"}}
        )
    )

    assert error.status_code == 400
    assert "11.2" in error.detail and "fanqie" in error.detail
    assert "route" in error.detail and "重新部署" in error.detail
    assert hub_project.stored_json() == before, "a refused save writes nothing"


def test_switching_back_through_a_binding_entry_is_refused_too(hub_project) -> None:
    error = _refusal(
        lambda: hub_project.update(
            {"publish_bindings": [{**HUB_FANQIE, "route": "legacy"}, LEGACY_QIDIAN]}
        )
    )

    assert error.status_code == 400 and "11.2" in error.detail
    assert hub_project.routing()["fanqie"] == ("hub", DESTINATION)


def test_switching_back_is_refused_even_when_the_destination_is_kept(hub_project) -> None:
    error = _refusal(
        lambda: hub_project.update(
            {"publish": {**HUB_FANQIE, "route": "legacy", "hub_destination_id": DESTINATION}}
        )
    )

    assert error.status_code == 400


def test_a_second_platform_cannot_be_switched_back_either() -> None:
    database = _Database(
        "hub-guard-second-platform",
        {
            "publish": LEGACY_QIDIAN,
            "publish_bindings": [
                LEGACY_QIDIAN,
                {**HUB_FANQIE},
            ],
        },
    )
    try:
        error = _refusal(
            lambda: database.update(
                {
                    "publish": LEGACY_QIDIAN,
                    "publish_bindings": [LEGACY_QIDIAN, {**HUB_FANQIE, "route": "legacy"}],
                }
            )
        )
        assert error.status_code == 400 and "fanqie" in error.detail
        assert database.routing()["fanqie"] == ("hub", DESTINATION)
    finally:
        database.dispose()


def test_removing_a_hub_binding_by_replacing_both_lists_is_refused(hub_project) -> None:
    before = hub_project.stored_json()

    error = _refusal(
        lambda: hub_project.update(
            {
                "publish": {"platform": "qidian", "book_name": "起点书名"},
                "publish_bindings": [{"platform": "qidian", "book_name": "起点书名"}],
            }
        )
    )

    assert error.status_code == 400
    assert "11.2" in error.detail and "fanqie" in error.detail
    assert hub_project.stored_json() == before, "a refused save writes nothing"


def test_emptying_both_lists_cannot_drop_a_hub_binding(hub_project) -> None:
    before = hub_project.stored_json()

    error = _refusal(
        lambda: hub_project.update({"publish": {"platform": ""}, "publish_bindings": []})
    )

    assert error.status_code == 400 and "11.2" in error.detail
    assert hub_project.stored_json() == before


@pytest.mark.parametrize(
    "body",
    [{"publish_bindings": []}, {"publish_bindings": [LEGACY_QIDIAN]}],
    ids=["bindings-emptied", "bindings-without-the-hub-platform"],
)
def test_shrinking_the_bindings_cannot_drop_a_second_hub_binding(body) -> None:
    # The stored primary entry is kept when a request leaves `publish` out, so the hub binding
    # that can be lost this way is the one that is not the primary.
    database = _Database(
        "hub-guard-shrink-bindings",
        {"publish": LEGACY_QIDIAN, "publish_bindings": [LEGACY_QIDIAN, HUB_FANQIE]},
    )
    try:
        before = database.stored_json()

        error = _refusal(lambda: database.update(body))

        assert error.status_code == 400 and "11.2" in error.detail and "fanqie" in error.detail
        assert database.stored_json() == before
    finally:
        database.dispose()


def test_a_bindings_only_request_leaves_the_primary_hub_binding_where_it_is(hub_project) -> None:
    # No refusal because nothing is lost: the stored primary entry outvotes the (empty) list.
    hub_project.update({"publish_bindings": []})

    assert hub_project.routing()["fanqie"] == ("hub", DESTINATION)


def test_a_removed_hub_binding_cannot_come_back_on_the_default_route(hub_project) -> None:
    # Remove, then re-add in a second request: the removal is already refused, so the re-add finds
    # the binding on the hub and (route left out) keeps it there.
    before = hub_project.stored_json()
    removal = {
        "publish": {"platform": "qidian", "book_name": "起点书名"},
        "publish_bindings": [{"platform": "qidian", "book_name": "起点书名"}],
    }
    assert _refusal(lambda: hub_project.update(removal)).status_code == 400
    assert hub_project.stored_json() == before

    hub_project.update(
        {
            "publish": {"platform": "fanqie", "book_name": "番茄书名"},
            "publish_bindings": [
                {"platform": "fanqie", "book_name": "番茄书名"},
                {"platform": "qidian", "book_name": "起点书名"},
            ],
        }
    )

    assert hub_project.routing() == {"fanqie": ("hub", DESTINATION), "qidian": ("legacy", None)}


def test_a_second_hub_platform_cannot_be_removed_either() -> None:
    database = _Database(
        "hub-guard-remove-second",
        {"publish": LEGACY_QIDIAN, "publish_bindings": [LEGACY_QIDIAN, HUB_FANQIE]},
    )
    try:
        before = database.stored_json()

        error = _refusal(
            lambda: database.update(
                {"publish": LEGACY_QIDIAN, "publish_bindings": [LEGACY_QIDIAN]}
            )
        )

        assert error.status_code == 400 and "fanqie" in error.detail
        assert database.stored_json() == before
    finally:
        database.dispose()


def test_a_platform_that_is_not_on_the_hub_can_still_be_removed() -> None:
    database = _Database(
        "hub-guard-remove-legacy",
        {"publish": HUB_FANQIE, "publish_bindings": [HUB_FANQIE, LEGACY_QIDIAN]},
    )
    try:
        database.update(
            {"publish": HUB_FANQIE, "publish_bindings": [HUB_FANQIE]}
        )

        assert database.routing() == {"fanqie": ("hub", DESTINATION)}
    finally:
        database.dispose()


def test_a_project_without_a_hub_binding_can_clear_its_bindings() -> None:
    database = _Database("hub-guard-clear-legacy", {"publish": LEGACY_QIDIAN})
    try:
        database.update({"publish": {"platform": ""}, "publish_bindings": []})

        assert database.routing() == {}
    finally:
        database.dispose()


def test_clearing_the_destination_of_a_hub_binding_is_refused(hub_project) -> None:
    before = hub_project.stored_json()

    for publish in (
        {**HUB_FANQIE, "hub_destination_id": None},
        {**HUB_FANQIE, "hub_destination_id": "   "},
        {"platform": "fanqie", "book_name": "番茄书名", "hub_destination_id": None},
    ):
        error = _refusal(lambda publish=publish: hub_project.update({"publish": publish}))
        assert error.status_code == 400
        assert "hub_destination_id" in error.detail and "fanqie" in error.detail

    assert hub_project.stored_json() == before


def test_legacy_to_hub_is_allowed(hub_project) -> None:
    hub_project.update(
        {
            "publish_bindings": [
                HUB_FANQIE,
                {**LEGACY_QIDIAN, "route": "hub", "hub_destination_id": OTHER_DESTINATION},
            ]
        }
    )

    assert hub_project.routing()["qidian"] == ("hub", OTHER_DESTINATION)


def test_a_legacy_platform_may_be_switched_to_hub_before_its_destination_exists() -> None:
    # The release gate holds the chapters with a message while the destination is missing, and
    # the cutover sets the route first; only clearing a destination that was there is refused.
    database = _Database("hub-guard-hub-first", {"publish": LEGACY_QIDIAN})
    try:
        database.update({"publish": {**LEGACY_QIDIAN, "route": "hub"}})
        assert database.routing()["qidian"] == ("hub", None)

        database.update({"publish": {"platform": "qidian", "book_name": "起点书名"}})
        assert database.routing()["qidian"] == ("hub", None), "an ordinary save is not a clearing"
    finally:
        database.dispose()


def test_a_legacy_platform_stays_changeable_in_every_way() -> None:
    database = _Database("hub-guard-legacy-free", {"publish": LEGACY_QIDIAN})
    try:
        database.update({"publish": {**LEGACY_QIDIAN, "route": "legacy", "book_name": "改名"}})
        database.update({"publish": {"platform": "fanqie", "book_name": "换平台"}})

        assert database.routing()["fanqie"] == ("legacy", None)
    finally:
        database.dispose()


# --- re-pointing a hub binding while stored requests wait (final review, item 4) ----------


def _store_request(
    database,
    destination: str,
    *,
    platform: str = "fanqie",
    status: str = "pending",
    hub_job_id: str | None = None,
    deleted: bool = False,
    task_kind: str = "chapter_upload",
) -> None:
    """A row whose request body is stored for `destination` (sent once the sync gets to it)."""
    with database.session_factory.begin() as session:
        session.add(
            PublisherUploadJob(
                project_id=database.project_id,
                platform_id=platform,
                task_kind=task_kind,
                idempotency_key=new_id(),
                book_name="番茄书名",
                body_text="",
                status=status,
                hub_submit_body_json={"destination_id": destination, "idempotency_key": new_id()},
                hub_job_id=hub_job_id,
                deleted_at=datetime(2026, 11, 1) if deleted else None,
            )
        )


@pytest.mark.parametrize("entry", ["publish", "publish_bindings"])
def test_re_pointing_a_hub_binding_while_stored_requests_wait_is_refused(hub_project, entry) -> None:
    """A stored body names the destination it was built for and is sent unchanged, so the waiting
    chapters would still go to the old book."""
    _store_request(hub_project, DESTINATION)
    _store_request(hub_project, DESTINATION, task_kind="cover_upload")
    before = hub_project.stored_json()
    repointed = {**HUB_FANQIE, "hub_destination_id": OTHER_DESTINATION}
    body = (
        {"publish": repointed}
        if entry == "publish"
        else {"publish_bindings": [repointed, LEGACY_QIDIAN]}
    )

    error = _refusal(lambda: hub_project.update(body))

    assert error.status_code == 400
    assert "fanqie" in error.detail and "hub_destination_id" in error.detail
    assert "2 条" in error.detail
    assert hub_project.stored_json() == before


def test_re_pointing_is_allowed_once_no_stored_request_waits(hub_project) -> None:
    # None of these is resent to the old destination by the sync.
    _store_request(hub_project, DESTINATION, hub_job_id="5b1f0c2e-0000-4000-8000-0000000000aa")
    _store_request(hub_project, DESTINATION, status="failed")
    _store_request(hub_project, DESTINATION, status="superseded")
    _store_request(hub_project, DESTINATION, deleted=True)
    _store_request(hub_project, DESTINATION, platform="qidian")
    _store_request(hub_project, OTHER_DESTINATION)  # already built for the new destination

    hub_project.update({"publish": {**HUB_FANQIE, "hub_destination_id": OTHER_DESTINATION}})

    assert hub_project.routing()["fanqie"] == ("hub", OTHER_DESTINATION)


def test_a_save_that_keeps_the_destination_is_not_held_up_by_waiting_requests(hub_project) -> None:
    _store_request(hub_project, DESTINATION)

    hub_project.update({"daily_chapter_quota": 4})
    hub_project.update({"publish": {**HUB_FANQIE, "book_name": "番茄新名"}})
    hub_project.update({"publish": {"platform": "fanqie", "book_name": "番茄新名"}})

    assert hub_project.routing()["fanqie"] == ("hub", DESTINATION)
    assert hub_project.stored().publish.book_name == "番茄新名"


def test_the_refusal_comes_back_as_a_400_over_http() -> None:
    database = _Database(
        "hub-guard-http",
        {"publish": HUB_FANQIE, "publish_bindings": [HUB_FANQIE]},
    )
    try:
        harness = HttpRuntimeHarness(
            session_factory=database.session_factory,
            config=InfrastructureConfig(database_url=postgres_test_url("hub-guard-http")),
            engine=database.engine,
        )
        client = TestClient(harness.app)
        url = f"/api/projects/{database.project_id}/automation"

        refused = client.put(
            url, json={"publish": {"platform": "fanqie", "book_name": "番茄书名", "route": "legacy"}}
        )
        assert refused.status_code == 400
        assert "11.2" in refused.json()["detail"]

        removed = client.put(
            url,
            json={
                "publish": {"platform": "qidian", "book_name": "起点书名"},
                "publish_bindings": [{"platform": "qidian", "book_name": "起点书名"}],
            },
        )
        assert removed.status_code == 400
        assert "11.2" in removed.json()["detail"]
        assert database.routing()["fanqie"] == ("hub", DESTINATION)

        # The form's save before this task: no route, no destination.
        kept = client.put(url, json={"publish": {"platform": "fanqie", "book_name": "改名"}})
        assert kept.status_code == 200
        saved = kept.json()["automation"]
        assert (saved["publish"]["route"], saved["publish"]["hub_destination_id"]) == (
            "hub",
            DESTINATION,
        )
        assert database.routing()["fanqie"] == ("hub", DESTINATION)
    finally:
        database.dispose()


# --- the home form sends the routing back --------------------------------------------------

TASK_CONTROL = Path("forwin/ui_assets/home/app_task_control.js")
NODE = shutil.which("node")


def _routing_helper() -> str:
    source = TASK_CONTROL.read_text(encoding="utf-8")
    match = re.search(r"^    function publishRoutingFields\(.*?^    }\n", source, re.S | re.M)
    assert match, "app_task_control.js must define publishRoutingFields(automation, platform)"
    return match.group(0)


def _routing_for(automation: dict | None, platform: str) -> dict:
    if NODE is None:
        pytest.skip("node is required to run the home form script")
    program = (
        _routing_helper()
        + f"\nconsole.log(JSON.stringify(publishRoutingFields({json.dumps(automation)}, {json.dumps(platform)})));\n"
    )
    result = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_form_sends_the_stored_routing_of_the_platform_it_shows() -> None:
    automation = {"publish": {"platform": "fanqie", "route": "hub", "hub_destination_id": DESTINATION}}

    assert _routing_for(automation, "fanqie") == {
        "route": "hub",
        "hub_destination_id": DESTINATION,
    }


def test_the_form_sends_a_platform_without_a_destination_as_null() -> None:
    assert _routing_for(
        {"publish": {"platform": "qidian", "route": "legacy", "hub_destination_id": ""}}, "qidian"
    ) == {"route": "legacy", "hub_destination_id": None}
    # An answer without a route (it always has one) sends none, so the server keeps its own.
    assert _routing_for({"publish": {"platform": "qidian"}}, "qidian") == {
        "hub_destination_id": None
    }


def test_the_form_sends_no_routing_for_a_platform_it_did_not_start_with() -> None:
    automation = {"publish": {"platform": "fanqie", "route": "hub", "hub_destination_id": DESTINATION}}

    assert _routing_for(automation, "qidian") == {}
    assert _routing_for(automation, "") == {}
    assert _routing_for({}, "fanqie") == {}
    assert _routing_for(None, "fanqie") == {}


def test_the_save_button_puts_the_routing_into_the_publish_payload() -> None:
    source = TASK_CONTROL.read_text(encoding="utf-8")
    save = source[source.index("'保存自动化设置'") :]
    publish = save[save.index("publish: {") : save.index("const result = await requestJson")]

    assert "...publishRoutingFields(automation, platformSelect.value)" in publish
