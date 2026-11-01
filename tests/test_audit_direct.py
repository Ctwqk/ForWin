"""Audit sync over the direct request channel (D8).

ForWin keeps audit requests itself and the extension claims them, like comment sync; nothing
here goes through publish-hub or the legacy upload-job runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from forwin.audit_direct.service import AUDIT_REQUEST_LEASE_SECONDS, AuditRequestError
from forwin.http import HttpRuntime, create_app
from forwin.models.base import get_engine, get_session_factory, init_db
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
        current_url="https://fanqienovel.com/main/writer/book-info/9999",
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
    assert _row(manager, created["request_id"]).status == "claimed"


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
