"""Audit sync over the direct request channel (D8).

ForWin keeps audit requests itself and the extension claims them, like comment sync; nothing
here goes through publish-hub or the legacy upload-job runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from forwin.audit.events import DecisionEventType
from forwin.audit_direct.service import (
    AUDIT_REQUEST_LEASE_SECONDS,
    AUDIT_REQUEST_MAX_ATTEMPTS,
    AuditRequestError,
)
from forwin.http import HttpRuntime, create_app
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherAuditRequest,
    PublisherChapterBinding,
    PublisherMilestone,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publishers.manager import PublisherManager
from tests.postgres import postgres_test_url

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
LEASE = timedelta(seconds=AUDIT_REQUEST_LEASE_SECONDS)
KEY_HEADER = "X-Forwin-Extension-Key"
CLAIM_KEYS = {"request_id", "platform", "remote_book_id", "upload_url", "remote_url"}
FANQIE_URL = "https://fanqienovel.com/main/writer/book-info/7001"


@pytest.fixture
def env(request):
    engine = get_engine(postgres_test_url(f"audit-direct-{request.node.name}"))
    init_db(engine)
    manager = PublisherManager(get_session_factory(engine), extension_api_key="secret")
    try:
        yield engine, manager
    finally:
        engine.dispose()


def _binding(
    manager: PublisherManager,
    *,
    project_id: str = "project-1",
    platform: str = "fanqie",
    book_name: str = "雾港",
    remote_book_id: str = "7001",
    remote_url: str = FANQIE_URL,
) -> str:
    with manager.session_factory() as session:
        work = PublisherWorkBinding(
            project_id=project_id,
            platform_id=platform,
            book_name=book_name,
            remote_book_id=remote_book_id,
            remote_url=remote_url,
            audit_state="under_review",
        )
        session.add(work)
        session.commit()
        return work.id


def _enqueue(manager: PublisherManager, work_binding_id: str, **extra) -> dict:
    return manager.enqueue_audit_sync(
        project_id="project-1",
        platform="fanqie",
        work_binding_id=work_binding_id,
        **extra,
    )


def _claim(manager: PublisherManager, client_id="client-1", platforms=("fanqie",), now=NOW):
    return manager.claim_next_audit_request(
        client_id=client_id, connected_platforms=list(platforms), now=now
    )


def _observation(**work) -> dict:
    return {
        "work": {
            "work_binding_id": "",
            "remote_book_id": "7001",
            "remote_url": FANQIE_URL,
            "audit_state": "approved",
            "official_status": "已发布",
            "platform_message": "审核通过",
            **work,
        },
        "chapters": [
            {
                "chapter_number": 3,
                "chapter_title": "第3章 风起",
                "remote_chapter_id": "c-3",
                "publish_state": "published",
                "audit_state": "approved",
                "word_count": 2100,
            }
        ],
        "cover": {"cover_state": "approved"},
        "milestones": [
            {
                "milestone_type": "signing_entry_visible",
                "state": "open",
                "message": "平台页面出现签约入口或签约提示。",
            }
        ],
    }


def _submit(manager, request_id, *, client_id="client-1", observation=None, now=NOW, **extra):
    return manager.submit_audit_request_result(
        request_id=request_id,
        client_id=client_id,
        now=now,
        current_url=extra.pop("current_url", FANQIE_URL),
        **(observation or _observation()),
    )


def _row(manager, request_id) -> PublisherAuditRequest:
    with manager.session_factory() as session:
        return session.get(PublisherAuditRequest, request_id)


def _binding_row(manager, work_binding_id) -> PublisherWorkBinding:
    with manager.session_factory() as session:
        return session.get(PublisherWorkBinding, work_binding_id)


def _count(manager, model) -> int:
    with manager.session_factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


# --- the manual API creates a request, not an upload job -----------------------------------


def test_manual_audit_creates_a_request_and_no_upload_job(env) -> None:
    _, manager = env
    work_id = _binding(manager)

    created = _enqueue(manager, work_id, book_name="雾港")

    assert created["status"] == "pending"
    assert created["created"] is True
    assert created["platform"] == "fanqie"
    assert created["project_id"] == "project-1"
    assert created["work_binding_id"] == work_id
    assert created["remote_book_id"] == "7001"
    assert created["remote_url"] == FANQIE_URL
    row = _row(manager, created["request_id"])
    assert (row.status, row.work_binding_id, row.remote_book_id) == ("pending", work_id, "7001")
    assert _count(manager, PublisherUploadJob) == 0
    # the legacy queue has nothing to hand out for it either
    assert (
        manager.claim_next_upload_job(client_id="client-1", connected_platforms=["fanqie"])
        is None
    )


def test_manual_audit_finds_the_binding_by_project_and_platform(env) -> None:
    _, manager = env
    work_id = _binding(manager)

    created = manager.enqueue_audit_sync(project_id="project-1", platform="fanqie")

    assert created["work_binding_id"] == work_id


def test_manual_audit_keeps_the_binding_identity_checks(env) -> None:
    _, manager = env
    work_id = _binding(manager)

    with pytest.raises(ValueError, match="项目与作品绑定不一致"):
        manager.enqueue_audit_sync(project_id="other", platform="fanqie", work_binding_id=work_id)
    with pytest.raises(ValueError, match="平台与作品绑定不一致"):
        manager.enqueue_audit_sync(
            project_id="project-1", platform="qidian", work_binding_id=work_id
        )
    with pytest.raises(ValueError, match="书名与作品绑定不一致"):
        _enqueue(manager, work_id, book_name="另一部作品")
    unbound = _binding(manager, project_id="project-2", remote_book_id="")
    with pytest.raises(ValueError, match="已绑定的平台作品 ID"):
        manager.enqueue_audit_sync(
            project_id="project-2", platform="fanqie", work_binding_id=unbound
        )
    assert _count(manager, PublisherAuditRequest) == 0


def test_an_open_request_is_returned_instead_of_a_duplicate(env) -> None:
    _, manager = env
    work_id = _binding(manager)

    first = _enqueue(manager, work_id)
    again = _enqueue(manager, work_id)
    by_project = manager.enqueue_audit_sync(project_id="project-1", platform="fanqie")

    assert again["request_id"] == first["request_id"] == by_project["request_id"]
    assert again["created"] is False and by_project["created"] is False
    assert _count(manager, PublisherAuditRequest) == 1

    # still the same request once it is claimed
    assert _claim(manager) is not None
    claimed_again = _enqueue(manager, work_id)
    assert claimed_again["request_id"] == first["request_id"]
    assert claimed_again["status"] == "claimed"
    assert _count(manager, PublisherAuditRequest) == 1


def test_a_finished_request_does_not_block_the_next_one(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    first = _enqueue(manager, work_id)
    _claim(manager)
    _submit(manager, first["request_id"])

    second = _enqueue(manager, work_id)

    assert second["request_id"] != first["request_id"]
    assert second["created"] is True and second["status"] == "pending"


def test_requests_for_different_books_do_not_collide(env) -> None:
    _, manager = env
    _binding(manager)
    _binding(manager, project_id="project-2", remote_book_id="7002")
    _binding(manager, platform="qidian", remote_book_id="9", remote_url="")

    ids = {
        manager.enqueue_audit_sync(project_id="project-1", platform="fanqie")["request_id"],
        manager.enqueue_audit_sync(project_id="project-2", platform="fanqie")["request_id"],
        manager.enqueue_audit_sync(project_id="project-1", platform="qidian")["request_id"],
    }

    assert len(ids) == 3
    assert _count(manager, PublisherAuditRequest) == 3


def test_the_database_refuses_two_open_requests_for_one_book(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    _enqueue(manager, work_id)

    with manager.session_factory() as session:
        session.add(
            PublisherAuditRequest(
                project_id="project-1",
                platform_id="fanqie",
                work_binding_id=work_id,
                book_name="雾港",
                remote_book_id="7001",
                status="claimed",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# --- claim --------------------------------------------------------------------------------


def test_claim_returns_exactly_the_request_fields(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)

    claim = _claim(manager)

    assert set(claim) == CLAIM_KEYS
    assert claim == {
        "request_id": created["request_id"],
        "platform": "fanqie",
        "remote_book_id": "7001",
        "upload_url": FANQIE_URL,
        "remote_url": FANQIE_URL,
    }
    row = _row(manager, created["request_id"])
    assert row.status == "claimed"
    assert row.extension_client_id == "client-1"
    assert row.attempt_count == 1


def test_claim_is_empty_when_there_is_nothing(env) -> None:
    _, manager = env

    assert _claim(manager) is None

    _enqueue(manager, _binding(manager))
    assert _claim(manager) is not None
    assert _claim(manager, client_id="client-2") is None


def test_claim_only_serves_the_platforms_the_extension_can_serve(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))

    assert _claim(manager, platforms=["qidian"]) is None
    assert _claim(manager, platforms=[]) is None
    assert _claim(manager, platforms=["not-a-platform"]) is None
    assert _row(manager, created["request_id"]).status == "pending"

    claim = _claim(manager, platforms=["qidian", "not-a-platform", "fanqie"])
    assert claim["request_id"] == created["request_id"]


def test_claim_serves_the_oldest_request_first(env) -> None:
    _, manager = env
    _binding(manager)
    first = manager.enqueue_audit_sync(project_id="project-1", platform="fanqie")
    _binding(manager, project_id="project-2", remote_book_id="7002")
    second = manager.enqueue_audit_sync(project_id="project-2", platform="fanqie")

    one = _claim(manager)
    two = _claim(manager)
    three = _claim(manager)

    assert [one["request_id"], two["request_id"]] == [first["request_id"], second["request_id"]]
    assert three is None


def test_claim_respects_the_preferred_client_rule_like_comment_sync(env) -> None:
    engine, _ = env
    manager = PublisherManager(
        get_session_factory(engine),
        extension_api_key="secret",
        preferred_client_id="main",
        strict_preferred_client=True,
    )
    _enqueue(manager, _binding(manager))

    assert _claim(manager, client_id="other") is None
    assert _claim(manager, client_id="main") is not None


# --- lease --------------------------------------------------------------------------------


def test_a_request_nobody_answers_is_claimable_again_after_the_lease(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    first = _claim(manager, client_id="client-1", now=NOW)
    assert first["request_id"] == created["request_id"]

    # held while the lease runs, by anyone, the owner included
    assert _claim(manager, client_id="client-2", now=NOW + LEASE - timedelta(seconds=1)) is None
    assert _claim(manager, client_id="client-1", now=NOW + LEASE - timedelta(seconds=1)) is None

    again = _claim(manager, client_id="client-2", now=NOW + LEASE + timedelta(seconds=1))

    assert again["request_id"] == created["request_id"]
    row = _row(manager, created["request_id"])
    assert (row.status, row.extension_client_id, row.attempt_count) == ("claimed", "client-2", 2)
    assert _count(manager, PublisherAuditRequest) == 1


def test_the_owner_may_claim_its_own_request_again_after_the_lease(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    _claim(manager, now=NOW)

    again = _claim(manager, now=NOW + LEASE + timedelta(seconds=1))

    assert again["request_id"] == created["request_id"]
    assert _row(manager, created["request_id"]).attempt_count == 2


def test_a_claim_takes_a_fresh_lease(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    _claim(manager, client_id="client-1", now=NOW)
    later = NOW + LEASE + timedelta(seconds=5)
    _claim(manager, client_id="client-2", now=later)

    assert _claim(manager, client_id="client-3", now=later + LEASE - timedelta(seconds=1)) is None
    assert (
        _claim(manager, client_id="client-3", now=later + LEASE + timedelta(seconds=1))[
            "request_id"
        ]
        == created["request_id"]
    )


# --- result -------------------------------------------------------------------------------


def test_a_result_updates_the_bindings_through_update_from_audit_sync_result(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)

    calls = []
    original = manager.runtime.bindings.update_from_audit_sync_result

    def spy(session, *, job, result_payload, current_url=""):
        calls.append((job, result_payload, current_url))
        return original(session, job=job, result_payload=result_payload, current_url=current_url)

    manager.runtime.bindings.update_from_audit_sync_result = spy

    done = _submit(manager, created["request_id"])

    assert len(calls) == 1
    job, payload, current_url = calls[0]
    assert job.task_kind == "audit_sync"
    assert (job.project_id, job.platform_id, job.book_name) == ("project-1", "fanqie", "雾港")
    assert job.id == created["request_id"]
    assert current_url == FANQIE_URL
    assert payload["work"]["audit_state"] == "approved"
    assert [item["chapter_number"] for item in payload["chapters"]] == [3]

    assert done["status"] == "succeeded"
    assert done["request_id"] == created["request_id"]
    assert done["work_binding_id"] == work_id
    binding = _binding_row(manager, work_id)
    assert binding.audit_state == "approved"
    assert binding.platform_status == "已发布"
    assert binding.remote_book_id == "7001"
    assert binding.last_synced_at is not None
    with manager.session_factory() as session:
        chapters = session.execute(select(PublisherChapterBinding)).scalars().all()
        milestones = session.execute(select(PublisherMilestone)).scalars().all()
    assert [(c.work_binding_id, c.chapter_number, c.audit_state) for c in chapters] == [
        (work_id, 3, "approved")
    ]
    assert [(m.work_binding_id, m.milestone_type) for m in milestones] == [
        (work_id, "signing_entry_visible")
    ]
    row = _row(manager, created["request_id"])
    assert row.status == "succeeded" and row.finished_at is not None
    assert _count(manager, PublisherUploadJob) == 0


def test_the_stored_book_identity_wins_over_the_page_values(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)

    _submit(
        manager,
        created["request_id"],
        observation=_observation(remote_book_id="", work_binding_id=""),
    )

    binding = _binding_row(manager, work_id)
    assert binding.remote_book_id == "7001"
    assert _count(manager, PublisherWorkBinding) == 1


def _snapshot(manager, work_id, request_id):
    binding = _binding_row(manager, work_id)
    row = _row(manager, request_id)
    return (
        binding.audit_state,
        binding.platform_status,
        binding.remote_book_id,
        binding.last_synced_at,
        _count(manager, PublisherChapterBinding),
        _count(manager, PublisherMilestone),
        _count(manager, PublisherWorkBinding),
        row.status,
        row.extension_client_id,
        row.finished_at,
    )


def test_a_result_for_an_unknown_request_is_refused(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    before = _snapshot(manager, work_id, created["request_id"])

    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, "no-such-request")

    assert (refused.value.status_code, refused.value.code) == (404, "audit_request_not_found")
    assert _snapshot(manager, work_id, created["request_id"]) == before


def test_a_result_for_a_finished_request_is_refused_and_changes_nothing(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    _submit(manager, created["request_id"])
    before = _snapshot(manager, work_id, created["request_id"])

    with pytest.raises(AuditRequestError) as refused:
        _submit(
            manager,
            created["request_id"],
            observation={
                **_observation(audit_state="rejected"),
                "chapters": [{"chapter_number": 9, "chapter_title": "不该写入"}],
                "milestones": [{"milestone_type": "revenue_entry_visible"}],
            },
        )

    assert (refused.value.status_code, refused.value.code) == (409, "audit_request_finished")
    assert _snapshot(manager, work_id, created["request_id"]) == before


def test_a_result_from_a_client_that_does_not_own_the_request_is_refused(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)

    # never claimed
    before = _snapshot(manager, work_id, created["request_id"])
    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"])
    assert (refused.value.status_code, refused.value.code) == (409, "audit_request_not_owned")
    assert _snapshot(manager, work_id, created["request_id"]) == before

    # claimed by someone else
    _claim(manager, client_id="client-1")
    before = _snapshot(manager, work_id, created["request_id"])
    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"], client_id="client-2")
    assert refused.value.code == "audit_request_not_owned"
    assert _snapshot(manager, work_id, created["request_id"]) == before


def test_a_result_after_the_request_moved_to_another_client_is_refused(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager, client_id="client-1", now=NOW)
    _claim(manager, client_id="client-2", now=NOW + LEASE + timedelta(seconds=1))
    before = _snapshot(manager, work_id, created["request_id"])

    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"], client_id="client-1")

    assert refused.value.code == "audit_request_not_owned"
    assert _snapshot(manager, work_id, created["request_id"]) == before
    assert _submit(manager, created["request_id"], client_id="client-2")["status"] == "succeeded"


def test_a_late_result_is_kept_while_nobody_else_took_the_request(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager, now=NOW)

    done = _submit(manager, created["request_id"], now=NOW + LEASE * 3)

    assert done["status"] == "succeeded"
    assert _binding_row(manager, work_id).audit_state == "approved"


def test_a_result_about_another_book_is_refused_without_side_effects(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    before = _snapshot(manager, work_id, created["request_id"])

    for work in ({"remote_book_id": "8888"}, {"work_binding_id": "someone-elses-binding"}):
        with pytest.raises(AuditRequestError) as refused:
            _submit(manager, created["request_id"], observation=_observation(**work))
        assert (refused.value.status_code, refused.value.code) == (
            422,
            "audit_result_identity_mismatch",
        )
        assert _snapshot(manager, work_id, created["request_id"]) == before


def test_a_result_for_a_binding_that_is_gone_is_refused(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    with manager.session_factory() as session:
        session.delete(session.get(PublisherWorkBinding, work_id))
        session.commit()

    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"])

    assert (refused.value.status_code, refused.value.code) == (409, "audit_work_binding_missing")
    assert _count(manager, PublisherWorkBinding) == 0
    assert _count(manager, PublisherChapterBinding) == 0
    # nothing to retry: the request is over
    row = _row(manager, created["request_id"])
    assert (row.status, row.error_message) == ("failed", "work binding no longer exists")


def test_a_failing_update_leaves_the_request_open_for_the_lease_retry(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)

    def boom(*_args, **_kwargs):
        raise RuntimeError("db hiccup")

    manager.runtime.bindings.update_from_audit_sync_result = boom
    with pytest.raises(RuntimeError):
        _submit(manager, created["request_id"])

    row = _row(manager, created["request_id"])
    assert row.status == "claimed" and row.finished_at is None
    assert _binding_row(manager, work_id).audit_state == "under_review"


# --- HTTP ----------------------------------------------------------------------------------


@pytest.fixture
def client(env):
    _, manager = env
    runtime = HttpRuntime()
    runtime.publisher_manager = manager
    return TestClient(create_app(runtime)), manager


def _post(http, path, body, key="secret"):
    headers = {KEY_HEADER: key} if key is not None else {}
    return http.post(path, json=body, headers=headers)


def test_the_routes_need_the_extension_key(client) -> None:
    http, manager = client
    created = _enqueue(manager, _binding(manager))

    for key in (None, "wrong"):
        claim = _post(http, "/api/publishers/extension/audit-requests/claim",
                      {"client_id": "client-1", "connected_platforms": ["fanqie"]}, key)
        result = _post(
            http,
            f"/api/publishers/extension/audit-requests/{created['request_id']}/result",
            {"client_id": "client-1", **_observation()},
            key,
        )
        assert claim.status_code == 401 and result.status_code == 401
    assert _row(manager, created["request_id"]).status == "pending"


def test_claim_over_http_answers_204_when_empty_and_the_request_when_there_is_one(client) -> None:
    http, manager = client
    path = "/api/publishers/extension/audit-requests/claim"
    body = {"client_id": "client-1", "connected_platforms": ["fanqie"]}

    empty = _post(http, path, body)
    assert empty.status_code == 204 and empty.content == b""

    created = _enqueue(manager, _binding(manager))
    wrong_platform = _post(http, path, {**body, "connected_platforms": ["qidian"]})
    assert wrong_platform.status_code == 204

    claimed = _post(http, path, body)
    assert claimed.status_code == 200
    assert claimed.json() == {
        "request_id": created["request_id"],
        "platform": "fanqie",
        "remote_book_id": "7001",
        "upload_url": FANQIE_URL,
        "remote_url": FANQIE_URL,
    }
    assert _post(http, path, body).status_code == 204


def test_result_over_http_applies_it_and_refuses_the_rest(client) -> None:
    http, manager = client
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    base = "/api/publishers/extension/audit-requests"
    claim_body = {"client_id": "client-1", "connected_platforms": ["fanqie"]}
    assert _post(http, f"{base}/claim", claim_body).status_code == 200

    unknown = _post(http, f"{base}/nope/result", {"client_id": "client-1", **_observation()})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "audit_request_not_found"

    stranger = _post(
        http, f"{base}/{created['request_id']}/result", {"client_id": "client-2", **_observation()}
    )
    assert stranger.status_code == 409
    assert stranger.json()["detail"]["code"] == "audit_request_not_owned"

    incomplete = _post(
        http,
        f"{base}/{created['request_id']}/result",
        {"client_id": "client-1", "work": {}, "chapters": [], "cover": {}},
    )
    assert incomplete.status_code == 422

    ok = _post(
        http,
        f"{base}/{created['request_id']}/result",
        {"client_id": "client-1", "current_url": FANQIE_URL, **_observation()},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "succeeded"
    assert _binding_row(manager, work_id).audit_state == "approved"

    again = _post(
        http,
        f"{base}/{created['request_id']}/result",
        {"client_id": "client-1", **_observation(audit_state="rejected")},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "audit_request_finished"
    assert _binding_row(manager, work_id).audit_state == "approved"


def test_the_manual_audit_api_creates_a_request_over_http(client) -> None:
    http, manager = client
    work_id = _binding(manager)

    response = http.post(
        "/api/publishers/audit-sync",
        json={"project_id": "project-1", "platform": "fanqie", "work_binding_id": work_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["work_binding_id"] == work_id
    assert body["remote_book_id"] == "7001"
    assert _count(manager, PublisherUploadJob) == 0

    again = http.post(
        "/api/publishers/audit-sync",
        json={"project_id": "project-1", "platform": "fanqie"},
    )
    assert again.status_code == 200
    assert again.json()["request_id"] == body["request_id"]
    assert again.json()["created"] is False

    missing = http.post(
        "/api/publishers/audit-sync", json={"project_id": "nobody", "platform": "fanqie"}
    )
    assert missing.status_code == 400


# --- failure reports (R49) -----------------------------------------------------------------


def _fail(manager, request_id, *, client_id="client-1", reason="平台审核页面没有准备好", now=NOW, **extra):
    return manager.fail_audit_request(
        request_id=request_id, client_id=client_id, reason=reason, now=now, **extra
    )


def _claim_fail_cycle(manager, request_id, attempts, *, report=True):
    """Claim a request `attempts` times, each after the previous lease ran out."""
    for attempt in range(attempts):
        at = NOW + (LEASE + timedelta(seconds=1)) * attempt
        claim = _claim(manager, now=at)
        assert claim is not None and claim["request_id"] == request_id
        if report:
            _fail(manager, request_id, now=at)


def test_a_failure_report_keeps_the_reason_and_the_request_waits_for_its_lease(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    _claim(manager, now=NOW)

    reported = _fail(manager, created["request_id"], reason="平台审核页面没有准备好")

    assert reported["status"] == "claimed"
    row = _row(manager, created["request_id"])
    assert (row.status, row.error_message, row.finished_at) == (
        "claimed",
        "平台审核页面没有准备好",
        None,
    )
    # not handed out again inside the round: it waits for its lease like an unanswered one
    assert _claim(manager, now=NOW + timedelta(seconds=5)) is None
    again = _claim(manager, now=NOW + LEASE + timedelta(seconds=1))
    assert again["request_id"] == created["request_id"]
    assert _row(manager, created["request_id"]).attempt_count == 2


def test_a_failure_at_the_attempt_cap_fails_the_request(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)

    _claim_fail_cycle(manager, created["request_id"], AUDIT_REQUEST_MAX_ATTEMPTS)

    row = _row(manager, created["request_id"])
    assert (row.status, row.attempt_count) == ("failed", AUDIT_REQUEST_MAX_ATTEMPTS)
    assert row.error_message == "平台审核页面没有准备好"
    assert row.finished_at is not None and row.lease_expires_at is None
    # nothing left to claim, and the book can be asked about again
    assert _claim(manager, now=NOW + LEASE * 100) is None
    fresh = _enqueue(manager, work_id)
    assert fresh["created"] is True and fresh["request_id"] != created["request_id"]


def test_a_claim_of_a_request_past_the_cap_fails_it_and_serves_the_next_one(env) -> None:
    _, manager = env
    stuck = _enqueue(manager, _binding(manager))
    _claim_fail_cycle(manager, stuck["request_id"], AUDIT_REQUEST_MAX_ATTEMPTS, report=False)
    _binding(manager, project_id="project-2", remote_book_id="7002")
    other = manager.enqueue_audit_sync(project_id="project-2", platform="fanqie")
    later = NOW + (LEASE + timedelta(seconds=1)) * AUDIT_REQUEST_MAX_ATTEMPTS

    claim = _claim(manager, now=later)

    assert claim["request_id"] == other["request_id"]
    row = _row(manager, stuck["request_id"])
    assert (row.status, row.attempt_count) == ("failed", AUDIT_REQUEST_MAX_ATTEMPTS)
    assert "没有" in row.error_message or "attempts" in row.error_message
    assert row.finished_at is not None


def test_a_request_past_the_cap_alone_is_failed_by_the_claim_and_nothing_is_served(env) -> None:
    _, manager = env
    stuck = _enqueue(manager, _binding(manager))
    _claim_fail_cycle(manager, stuck["request_id"], AUDIT_REQUEST_MAX_ATTEMPTS, report=False)
    later = NOW + (LEASE + timedelta(seconds=1)) * AUDIT_REQUEST_MAX_ATTEMPTS

    assert _claim(manager, now=later) is None

    assert _row(manager, stuck["request_id"]).status == "failed"


def test_the_last_reported_reason_survives_the_cap(env) -> None:
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    for attempt in range(AUDIT_REQUEST_MAX_ATTEMPTS - 1):
        at = NOW + (LEASE + timedelta(seconds=1)) * attempt
        _claim(manager, now=at)
        _fail(manager, created["request_id"], now=at, reason="打不开")
    at = NOW + (LEASE + timedelta(seconds=1)) * (AUDIT_REQUEST_MAX_ATTEMPTS - 1)
    _claim(manager, now=at)

    _fail(manager, created["request_id"], now=at, reason="仍然打不开")

    row = _row(manager, created["request_id"])
    assert (row.status, row.error_message) == ("failed", "仍然打不开")


def test_failure_reports_from_anyone_but_the_owner_are_refused_without_side_effects(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    request_id = created["request_id"]

    # unknown
    before = _snapshot(manager, work_id, request_id)
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, "no-such-request")
    assert (refused.value.status_code, refused.value.code) == (404, "audit_request_not_found")
    assert _snapshot(manager, work_id, request_id) == before

    # never claimed
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, request_id)
    assert (refused.value.status_code, refused.value.code) == (409, "audit_request_not_owned")
    assert _row(manager, request_id).error_message == ""

    # claimed by somebody else, or taken over after the lease
    _claim(manager, client_id="client-1", now=NOW)
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, request_id, client_id="client-2")
    assert refused.value.code == "audit_request_not_owned"
    _claim(manager, client_id="client-2", now=NOW + LEASE + timedelta(seconds=1))
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, request_id, client_id="client-1")
    assert refused.value.code == "audit_request_not_owned"
    assert _row(manager, request_id).error_message == ""

    # a bad risk reason changes nothing either
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, request_id, client_id="client-2", risk_reason="made-up")
    assert (refused.value.status_code, refused.value.code) == (422, "audit_risk_reason_invalid")
    row = _row(manager, request_id)
    assert (row.status, row.error_message, row.risk_reason) == ("claimed", "", "")


def test_failure_reports_for_finished_requests_are_refused_without_side_effects(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    done = _enqueue(manager, work_id)
    _claim(manager)
    _submit(manager, done["request_id"])
    before = _snapshot(manager, work_id, done["request_id"])
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, done["request_id"])
    assert (refused.value.status_code, refused.value.code) == (409, "audit_request_finished")
    assert _snapshot(manager, work_id, done["request_id"]) == before
    assert _row(manager, done["request_id"]).error_message == ""

    failing = _enqueue(manager, work_id)
    _claim(manager)
    _fail(manager, failing["request_id"], risk_reason="captcha")
    with pytest.raises(AuditRequestError) as refused:
        _fail(manager, failing["request_id"], reason="a second report")
    assert refused.value.code == "audit_request_finished"
    assert _row(manager, failing["request_id"]).error_message == "平台审核页面没有准备好"


def test_a_risk_failure_fails_the_request_at_once_and_keeps_the_risk_reason(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)

    reported = _fail(
        manager,
        created["request_id"],
        reason="Publisher captcha risk signal detected.",
        risk_reason="captcha",
        current_url="https://fanqienovel.com/main/writer/verify",
    )

    assert reported["status"] == "failed"
    assert reported["risk_reason"] == "captcha"
    row = _row(manager, created["request_id"])
    assert (row.status, row.risk_reason, row.attempt_count) == ("failed", "captcha", 1)
    assert row.error_message == "Publisher captcha risk signal detected."
    assert row.finished_at is not None and row.lease_expires_at is None
    assert _claim(manager, now=NOW + LEASE * 10) is None
    assert _binding_row(manager, work_id).audit_state == "under_review"
    # failed is outside the open set: the book can be audited again once the operator is done
    assert _enqueue(manager, work_id)["created"] is True


def test_a_risk_failure_records_the_pause_event_like_the_old_audit_job(env) -> None:
    engine, manager = env
    project_id = new_id()
    with manager.session_factory() as session:
        session.add(
            Project(
                id=project_id,
                title="雾港",
                premise="测试 premise",
                genre="玄幻",
                setting_summary="",
            )
        )
        session.commit()
    work_id = _binding(manager, project_id=project_id)
    created = manager.enqueue_audit_sync(project_id=project_id, platform="fanqie")
    _claim(manager)

    _fail(
        manager,
        created["request_id"],
        reason="Publisher mfa risk signal detected.",
        risk_reason="mfa",
        current_url="https://fanqienovel.com/main/writer/verify",
    )

    with manager.session_factory() as session:
        events = (
            session.execute(
                select(DecisionEvent).where(DecisionEvent.project_id == project_id)
            )
            .scalars()
            .all()
        )
    paused = [e for e in events if e.event_type == DecisionEventType.UPLOAD_JOB_PAUSED]
    assert len(paused) == 1
    assert paused[0].related_object_type == "publisher_audit_request"
    assert paused[0].related_object_id == created["request_id"]
    payload = json.loads(paused[0].payload_json)
    assert payload["risk_reason"] == "mfa"
    assert payload["platform_id"] == "fanqie"
    assert payload["task_kind"] == "audit_sync"
    assert payload["request_id"] == created["request_id"]
    assert payload["work_binding_id"] == work_id
    assert payload["current_url"] == "https://fanqienovel.com/main/writer/verify"
    assert work_id


def test_the_risk_pause_is_kept_even_when_its_event_cannot_be_written(env) -> None:
    # "project-1" has no project row, so the decision event (a foreign key) cannot be saved
    _, manager = env
    created = _enqueue(manager, _binding(manager))
    _claim(manager)

    _fail(manager, created["request_id"], risk_reason="account_risk")

    row = _row(manager, created["request_id"])
    assert (row.status, row.risk_reason) == ("failed", "account_risk")


# --- a result from a page that is not the book (R50) ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://fanqienovel.com/main/writer/",
        "https://fanqienovel.com/main/writer/login?redirect=/main/writer/book-info/7001",
        "https://fanqienovel.com/main/writer/book-info/70011",
        "https://fanqienovel.com/main/writer/book-info/17001",
        "https://fanqienovel.com/main/writer/book-info/9999",
        "",
    ],
)
def test_a_result_from_a_page_that_is_not_the_book_counts_as_a_failure(env, url) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    before = _binding_row(manager, work_id)
    chapters_before = _count(manager, PublisherChapterBinding)

    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"], current_url=url)

    assert (refused.value.status_code, refused.value.code) == (422, "audit_page_not_the_book")
    after = _binding_row(manager, work_id)
    assert (after.remote_url, after.audit_state, after.last_synced_at, after.platform_status) == (
        before.remote_url,
        before.audit_state,
        before.last_synced_at,
        before.platform_status,
    )
    assert _count(manager, PublisherChapterBinding) == chapters_before == 0
    assert _count(manager, PublisherMilestone) == 0
    row = _row(manager, created["request_id"])
    assert (row.status, row.error_message) == ("claimed", "audit page is not the book")
    assert row.finished_at is None


def test_pages_that_do_reference_the_book_are_accepted(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    for url in (
        FANQIE_URL,
        "https://fanqienovel.com/main/writer/chapter-manage/7001&%E9%9B%BE%E6%B8%AF?type=1",
        "https://fanqienovel.com/main/writer/book?book_id=7001",
    ):
        created = _enqueue(manager, work_id)
        _claim(manager, now=NOW)
        assert _submit(manager, created["request_id"], current_url=url)["status"] == "succeeded"


def test_wrong_pages_fail_the_request_at_the_cap_and_never_count_as_an_audit(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    login = "https://fanqienovel.com/main/writer/login"
    for attempt in range(AUDIT_REQUEST_MAX_ATTEMPTS):
        at = NOW + (LEASE + timedelta(seconds=1)) * attempt
        _claim(manager, now=at)
        with pytest.raises(AuditRequestError):
            _submit(manager, created["request_id"], current_url=login, now=at)

    row = _row(manager, created["request_id"])
    assert (row.status, row.error_message) == ("failed", "audit page is not the book")
    assert _binding_row(manager, work_id).remote_url == FANQIE_URL


# --- the live work binding (R50) ------------------------------------------------------------


def test_a_binding_corrected_after_the_request_keeps_its_corrected_values(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    with manager.session_factory() as session:
        work = session.get(PublisherWorkBinding, work_id)
        work.book_name = "更正后的书名"
        session.commit()

    _submit(manager, created["request_id"])

    binding = _binding_row(manager, work_id)
    assert binding.book_name == "更正后的书名"
    assert binding.audit_state == "approved"
    assert _count(manager, PublisherWorkBinding) == 1


def test_a_binding_whose_book_changed_after_the_request_is_not_overwritten(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    _claim(manager)
    with manager.session_factory() as session:
        work = session.get(PublisherWorkBinding, work_id)
        work.remote_book_id = "7002"
        work.remote_url = "https://fanqienovel.com/main/writer/book-info/7002"
        session.commit()

    with pytest.raises(AuditRequestError) as refused:
        _submit(manager, created["request_id"])

    assert (refused.value.status_code, refused.value.code) == (409, "audit_request_stale")
    binding = _binding_row(manager, work_id)
    assert (binding.remote_book_id, binding.audit_state) == ("7002", "under_review")
    row = _row(manager, created["request_id"])
    assert row.status == "failed" and "binding" in row.error_message
    # the corrected binding can be audited with a fresh request
    fresh = _enqueue(manager, work_id)
    assert fresh["created"] is True and fresh["remote_book_id"] == "7002"


# --- the latest request of a binding (for the cutover import) -------------------------------


def test_the_latest_request_of_a_binding_is_readable(env) -> None:
    _, manager = env
    work_id = _binding(manager)
    assert manager.latest_audit_request(work_binding_id=work_id) is None

    first = _enqueue(manager, work_id)
    pending = manager.latest_audit_request(work_binding_id=work_id)
    assert (pending["request_id"], pending["status"], pending["reason"]) == (
        first["request_id"],
        "pending",
        "",
    )
    assert pending["finished_at"] == ""

    _claim(manager)
    _fail(manager, first["request_id"], reason="Publisher captcha risk signal detected.", risk_reason="captcha")
    failed = manager.latest_audit_request(work_binding_id=work_id)
    assert failed["status"] == "failed"
    assert failed["reason"] == "Publisher captcha risk signal detected."
    assert failed["risk_reason"] == "captcha"
    assert failed["finished_at"] != ""

    second = _enqueue(manager, work_id)
    _claim(manager, now=NOW + LEASE)
    _submit(manager, second["request_id"], now=NOW + LEASE)
    done = manager.latest_audit_request(work_binding_id=work_id)
    assert (done["request_id"], done["status"], done["reason"]) == (
        second["request_id"],
        "succeeded",
        "",
    )
    assert done["finished_at"] != ""


def test_the_latest_request_is_per_binding(env) -> None:
    _, manager = env
    one = _binding(manager)
    two = _binding(manager, project_id="project-2", remote_book_id="7002")
    request_one = _enqueue(manager, one)

    assert manager.latest_audit_request(work_binding_id=two) is None
    assert manager.latest_audit_request(work_binding_id=one)["request_id"] == request_one["request_id"]


# --- failure over HTTP ----------------------------------------------------------------------


def test_the_failure_route_needs_the_key_and_the_owner(client) -> None:
    http, manager = client
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    base = "/api/publishers/extension/audit-requests"
    body = {"client_id": "client-1", "reason": "平台审核页面没有准备好"}
    assert _post(http, f"{base}/claim", {"client_id": "client-1", "connected_platforms": ["fanqie"]}).status_code == 200

    for key in (None, "wrong"):
        assert _post(http, f"{base}/{created['request_id']}/failure", body, key).status_code == 401

    unknown = _post(http, f"{base}/nope/failure", body)
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "audit_request_not_found"
    stranger = _post(http, f"{base}/{created['request_id']}/failure", {**body, "client_id": "client-2"})
    assert stranger.status_code == 409 and stranger.json()["detail"]["code"] == "audit_request_not_owned"
    bad_risk = _post(http, f"{base}/{created['request_id']}/failure", {**body, "risk_reason": "made-up"})
    assert bad_risk.status_code == 422
    no_reason = _post(http, f"{base}/{created['request_id']}/failure", {"client_id": "client-1"})
    assert no_reason.status_code == 422
    assert _row(manager, created["request_id"]).error_message == ""

    ok = _post(http, f"{base}/{created['request_id']}/failure", body)
    assert ok.status_code == 200
    assert ok.json()["status"] == "claimed" and ok.json()["request_id"] == created["request_id"]

    risk = _post(
        http,
        f"{base}/{created['request_id']}/failure",
        {**body, "risk_reason": "captcha", "current_url": "https://fanqienovel.com/verify"},
    )
    assert risk.status_code == 200 and risk.json()["status"] == "failed"
    again = _post(http, f"{base}/{created['request_id']}/failure", body)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "audit_request_finished"


def test_a_wrong_page_result_over_http_is_422_and_recorded_as_a_failure(client) -> None:
    http, manager = client
    work_id = _binding(manager)
    created = _enqueue(manager, work_id)
    base = "/api/publishers/extension/audit-requests"
    _post(http, f"{base}/claim", {"client_id": "client-1", "connected_platforms": ["fanqie"]})

    response = _post(
        http,
        f"{base}/{created['request_id']}/result",
        {"client_id": "client-1", "current_url": "https://fanqienovel.com/main/writer/", **_observation()},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "audit_page_not_the_book"
    assert _binding_row(manager, work_id).audit_state == "under_review"
    assert _row(manager, created["request_id"]).error_message == "audit page is not the book"
