from __future__ import annotations

import json

import httpx
import pytest

from forwin.publish_hub.client import HubRequestError, HubUnavailable, PublishHubClient


API_KEY = "test-producer-key-not-a-secret"
JOB = {
    "id": "5f1f4d2e-0000-4000-8000-000000000001",
    "status": "scheduled",
    "action": "upload",
    "publication": None,
    "latest_receipt": None,
}
BODY = {
    "idempotency_key": "forwin:abc",
    "destination_id": "3b3c8f1e-0000-4000-8000-000000000002",
    "source_ref": "project/p1/chapter/3",
    "content_kind": "novel_chapter",
    "action": "upload",
    "payload_version": 1,
    "payload": {
        "book_name": "雾港",
        "chapter_title": "第三章",
        "body_text": "正文\n第二行",
        "mode": "publish",
    },
    "content_refs": [],
}


def _client(handler, *, timeout: float = 20) -> PublishHubClient:
    return PublishHubClient(
        "http://hub.test:18930", API_KEY, timeout, transport=httpx.MockTransport(handler)
    )


def _envelope(status: int, code: str, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


class Recorder:
    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_submit_returns_job_and_201_for_a_new_job() -> None:
    recorder = Recorder(httpx.Response(201, json=JOB))

    job, status = _client(recorder).submit(BODY)

    assert (job, status) == (JOB, 201)
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url == "http://hub.test:18930/v1/jobs"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == BODY


def test_submit_returns_200_when_the_same_key_and_body_were_already_accepted() -> None:
    recorder = Recorder(httpx.Response(200, json=JOB))

    assert _client(recorder).submit(BODY) == (JOB, 200)


def test_submit_sends_the_given_dict_unchanged_and_the_same_bytes_every_time() -> None:
    recorder = Recorder(httpx.Response(201, json=JOB), httpx.Response(200, json=JOB))
    client = _client(recorder)
    # A body stored as jsonb comes back with its keys in another order.
    reloaded = json.loads(json.dumps(BODY, sort_keys=True))
    reordered = dict(reversed(list(reloaded.items())))
    snapshot = json.loads(json.dumps(BODY))

    client.submit(BODY)
    client.submit(reordered)

    first, second = recorder.requests
    assert first.content == second.content
    assert json.loads(first.content) == snapshot
    assert BODY == snapshot, "submit must not touch the dict it is given"
    # Non-ASCII text goes out as UTF-8, not as \u escapes.
    assert "第三章".encode("utf-8") in first.content


def test_submit_does_not_add_fields_to_a_minimal_body() -> None:
    recorder = Recorder(httpx.Response(201, json=JOB))
    minimal = {"idempotency_key": "forwin:x", "action": "upload"}

    _client(recorder).submit(minimal)

    assert json.loads(recorder.requests[0].content) == minimal


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (409, "idempotency_conflict"),
        (409, "publication_already_published"),
        (409, "destination_already_created"),
        (409, "publication_has_active_job"),
        (422, "invalid_payload"),
        (401, "unauthorized"),
        (404, "not_found"),
    ],
)
def test_submit_raises_hub_request_error_for_4xx_with_the_error_code(status, code) -> None:
    recorder = Recorder(_envelope(status, code, "the hub said no"))

    with pytest.raises(HubRequestError) as caught:
        _client(recorder).submit(BODY)

    error = caught.value
    assert (error.status, error.code, error.message) == (status, code, "the hub said no")
    assert code in str(error) and str(status) in str(error)


def test_a_4xx_without_the_error_envelope_still_raises_hub_request_error() -> None:
    recorder = Recorder(httpx.Response(413, text="Request Entity Too Large"))

    with pytest.raises(HubRequestError) as caught:
        _client(recorder).submit(BODY)

    assert caught.value.status == 413
    assert caught.value.code == "http_413"
    assert "Too Large" in caught.value.message


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_submit_raises_hub_unavailable_for_5xx(status) -> None:
    recorder = Recorder(httpx.Response(status, text="boom"))

    with pytest.raises(HubUnavailable):
        _client(recorder).submit(BODY)


@pytest.mark.parametrize("status", [408, 429])
def test_throttling_and_timeouts_from_a_proxy_are_retryable_not_request_errors(status) -> None:
    recorder = Recorder(httpx.Response(status, text="slow down"))

    with pytest.raises(HubUnavailable):
        _client(recorder).submit(BODY)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_network_errors_raise_hub_unavailable(failure) -> None:
    recorder = Recorder(failure)

    with pytest.raises(HubUnavailable):
        _client(recorder).submit(BODY)


def test_an_unreadable_success_response_is_unavailable_so_the_caller_resends() -> None:
    # The hub may have stored the job; the stored body is resent and the key finds it again.
    for response in (
        httpx.Response(201, text="<html>proxy page</html>"),
        httpx.Response(201, json=["not", "a", "job"]),
        httpx.Response(202, json=JOB),
    ):
        with pytest.raises(HubUnavailable):
            _client(Recorder(response)).submit(BODY)


def test_redirects_are_not_followed_and_count_as_unavailable() -> None:
    recorder = Recorder(httpx.Response(302, headers={"location": "http://elsewhere.test/"}))

    with pytest.raises(HubUnavailable):
        _client(recorder).submit(BODY)

    assert len(recorder.requests) == 1


def test_errors_never_carry_the_api_key() -> None:
    cases = [
        (Recorder(httpx.ConnectError("refused")), HubUnavailable),
        (Recorder(httpx.Response(503, text="down")), HubUnavailable),
        (Recorder(_envelope(409, "idempotency_conflict")), HubRequestError),
    ]
    for recorder, expected in cases:
        client = _client(recorder)
        with pytest.raises(expected) as caught:
            client.submit(BODY)
        assert API_KEY not in str(caught.value) and API_KEY not in repr(caught.value)
    assert API_KEY not in repr(client) and API_KEY not in str(client)


def test_get_job_reads_the_job_by_id() -> None:
    recorder = Recorder(httpx.Response(200, json=JOB))

    assert _client(recorder).get_job(JOB["id"]) == JOB

    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url == f"http://hub.test:18930/v1/jobs/{JOB['id']}"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"


def test_get_job_escapes_the_id_and_raises_for_an_unknown_job() -> None:
    recorder = Recorder(_envelope(404, "not_found", "no such job"))

    with pytest.raises(HubRequestError) as caught:
        _client(recorder).get_job("../jobs/x?y")

    assert caught.value.status == 404 and caught.value.code == "not_found"
    assert recorder.requests[0].url.raw_path == b"/v1/jobs/..%2Fjobs%2Fx%3Fy"


def test_find_by_key_returns_the_item_or_none() -> None:
    found = Recorder(httpx.Response(200, json={"items": [JOB]}))
    missing = Recorder(httpx.Response(200, json={"items": []}))

    assert _client(found).find_by_key("forwin:a b&c") == JOB
    assert _client(missing).find_by_key("forwin:nothing") is None

    request = found.requests[0]
    assert request.method == "GET" and request.url.path == "/v1/jobs"
    assert request.url.params["idempotency_key"] == "forwin:a b&c"


def test_find_by_key_rejects_a_response_without_items() -> None:
    with pytest.raises(HubUnavailable):
        _client(Recorder(httpx.Response(200, json={"unexpected": True}))).find_by_key("k")


def test_changes_returns_items_and_the_next_cursor() -> None:
    items = [
        {"seq": 11, "entity": "job", "entity_id": JOB["id"], "status": "succeeded", "at": "t"},
        {"seq": 12, "entity": "publication", "entity_id": "p1", "status": "published", "at": "t"},
    ]
    recorder = Recorder(httpx.Response(200, json={"items": items, "next_after": 12}))

    result = _client(recorder).changes(10)

    assert result == (items, 12)
    request = recorder.requests[0]
    assert request.url.path == "/v1/changes"
    assert dict(request.url.params) == {"after": "10", "limit": "200"}


def test_changes_passes_the_limit_and_keeps_the_cursor_when_nothing_changed() -> None:
    recorder = Recorder(httpx.Response(200, json={"items": [], "next_after": 40}))

    assert _client(recorder).changes(40, limit=25) == ([], 40)
    assert dict(recorder.requests[0].url.params) == {"after": "40", "limit": "25"}


def test_destinations_returns_the_list() -> None:
    destinations = [
        {
            "id": "d1",
            "account_id": "a1",
            "platform": "fanqie",
            "kind": "novel",
            "remote_id": None,
            "remote_url": None,
            "settings": {"book_name": "雾港"},
            "account_status": "active",
        }
    ]
    recorder = Recorder(httpx.Response(200, json=destinations))

    assert _client(recorder).destinations() == destinations

    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/v1/destinations"


def test_destinations_rejects_an_unexpected_shape() -> None:
    with pytest.raises(HubUnavailable):
        _client(Recorder(httpx.Response(200, json={"not": "a list"}))).destinations()


def test_timeout_is_applied_to_every_phase() -> None:
    client = _client(Recorder(), timeout=7)

    timeout = client._http.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (7, 7, 7, 7)


def test_client_ignores_proxy_environment_for_the_internal_hub(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    client = PublishHubClient("http://hub.test:18930", API_KEY, 20)

    assert client._http.trust_env is False
    client.close()


def test_a_trailing_slash_on_the_url_is_tolerated() -> None:
    recorder = Recorder(httpx.Response(200, json=JOB))
    client = PublishHubClient(
        "http://hub.test:18930/", API_KEY, 20, transport=httpx.MockTransport(recorder)
    )

    client.get_job(JOB["id"])

    assert recorder.requests[0].url == f"http://hub.test:18930/v1/jobs/{JOB['id']}"


@pytest.mark.parametrize("url, key", [("", API_KEY), ("   ", API_KEY), ("http://hub.test", ""), ("http://hub.test", "  ")])
def test_url_and_key_are_required(url, key) -> None:
    with pytest.raises(ValueError):
        PublishHubClient(url, key, 20)


@pytest.mark.parametrize("timeout", [0, -1])
def test_timeout_must_be_positive(timeout) -> None:
    with pytest.raises(ValueError, match="timeout"):
        PublishHubClient("http://hub.test", API_KEY, timeout)
