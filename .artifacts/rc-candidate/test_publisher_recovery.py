from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import base64
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest


ARTIFACT_DIR = Path(__file__).resolve().parent
RUNNER_PATH = ARTIFACT_DIR / "publisher_recovery.py"
COMMON_PATH = ARTIFACT_DIR / "recovery_runner_common.py"


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> Any:
    return load_module("task6_publisher_recovery", RUNNER_PATH)


@pytest.fixture(scope="module")
def common() -> Any:
    if str(ARTIFACT_DIR) not in sys.path:
        sys.path.insert(0, str(ARTIFACT_DIR))
    return importlib.import_module("recovery_runner_common")


def sensitive_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in (
                    "authorization",
                    "cookie",
                    "credential",
                    "password",
                    "secret",
                    "api_key",
                )
            ):
                found.append(path)
            found.extend(sensitive_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(sensitive_paths(nested, f"{prefix}[{index}]"))
    return found


def test_runner_reuses_shared_task4_components(runner: Any, common: Any) -> None:
    assert runner.CandidateIdentity.__module__ == common.CandidateIdentity.__module__
    assert runner.RecoveryController.__module__ == common.RecoveryController.__module__
    assert runner.EvidenceWriter.__module__ == common.EvidenceWriter.__module__
    assert runner.atomic_write_json_new.__module__ == common.atomic_write_json_new.__module__
    assert runner.http_json.__module__ == common.http_json.__module__


@pytest.mark.parametrize(
    ("kind", "task_kind"),
    (
        ("publisher_backend_unavailable", "cover_generate"),
        ("publisher_browser_unavailable", "chapter_upload"),
        ("publisher_captcha", "chapter_upload"),
        ("publisher_mfa", "chapter_upload"),
        ("publisher_account_risk", "chapter_upload"),
    ),
)
def test_fixture_is_fixed_projectless_unpublished_and_fault_unique(
    runner: Any,
    kind: str,
    task_kind: str,
) -> None:
    first = runner.publisher_fixture(kind, "fault-alpha")
    second = runner.publisher_fixture(kind, "fault-beta")

    assert first.task_kind == task_kind
    assert first.project_id == ""
    assert first.publish is False
    assert first.platform_id == "qidian"
    assert first.book_name == "Publisher Recovery Fixture"
    assert first.chapter_title == (
        "" if task_kind == "cover_generate" else "Recovery Chapter"
    )
    assert first.body == (
        ""
        if task_kind == "cover_generate"
        else "Generic publisher recovery fixture content."
    )
    assert first.body_sha256 == hashlib.sha256(first.body.encode()).hexdigest()
    assert first.logical_key == "publisher-recovery:v1:fault-alpha"
    assert second.logical_key == "publisher-recovery:v1:fault-beta"
    assert first.job_id != second.job_id
    assert first.fixture_id != second.fixture_id
    assert first.result_payload == (
        {
            "book_meta": {
                "intro": "Generic recovery cover fixture.",
                "primary_category": "systems",
            },
            "auto_cover_upload_enabled": False,
            "cover_candidate_count": 1,
            "cover_confirmation_required": False,
        }
        if task_kind == "cover_generate"
        else {}
    )
    assert sensitive_paths(first.evidence_identity()) == []
    assert sensitive_paths(first.result_payload) == []
    assert "project_id" not in first.result_payload
    assert "receipt" not in json.dumps(first.result_payload).lower()


def test_fixture_validation_rejects_project_credentials_publish_and_receipt(
    runner: Any,
) -> None:
    fixture = runner.publisher_fixture("publisher_captcha", "fault-safety")

    unsafe = (
        {"project_id": "project-live"},
        {"publish": True},
        {"result_payload": {"password": "stored"}},
        {"result_payload": {"external_receipt": {"id": "remote"}}},
        {"logical_key": "publisher-recovery:v1:another-fault"},
        {"book_name": "Different Book"},
        {"platform_id": "other-platform"},
        {
            "body": "Different content.",
            "body_sha256": hashlib.sha256(
                b"Different content."
            ).hexdigest(),
        },
        {"job_id": "different-job"},
    )
    for changes in unsafe:
        with pytest.raises(runner.SetupBlocked):
            runner.validate_fixture_spec(fixture.with_changes(**changes))


def test_fixture_identities_are_generalized_not_bound_to_live_task_constants(
    runner: Any,
) -> None:
    observed = {
        runner.publisher_fixture(
            kind,
            f"{kind.replace('_', '-')}-{fault_id}",
        ).evidence_identity()["resource_id"]
        for kind in runner.SUPPORTED_FAULTS
        for fault_id in ("fixture-one", "fixture-two")
    }

    assert len(observed) == len(runner.SUPPORTED_FAULTS) * 2
    assert all("80af1c8" not in identity for identity in observed)
    assert all("d97e58c" not in identity for identity in observed)


def test_shared_controller_exposes_typed_mark_and_read_only_file_inventory(
    tmp_path: Path,
    common: Any,
) -> None:
    calls: list[list[str]] = []

    def execute(args: list[str], **_kwargs: Any) -> Any:
        calls.append(list(args))
        return SimpleNamespace(
            returncode=0,
            stdout='{"files": []}\n',
            stderr="",
        )

    manifest = tmp_path / "candidate.json"
    manifest.write_text("{}", encoding="utf-8")
    controller = common.RecoveryController(
        candidate_manifest=manifest,
        evidence_dir=tmp_path / "evidence",
        execute=execute,
        python_executable="/python",
    )

    controller.mark("publisher_mfa", "fault", "fault-controller")
    inventory = controller.file_inventory(
        "publisher-browser",
        "fault-controller",
        "/app/data/publisher_covers",
    )

    assert inventory == {"files": []}
    assert [call[2:] for call in calls] == [
        [
            "mark",
            "publisher_mfa",
            "fault",
            "--fault-id",
            "fault-controller",
        ],
        [
            "file-inventory",
            "publisher-browser",
            "--fault-id",
            "fault-controller",
            "--root",
            "/app/data/publisher_covers",
        ],
    ]


def endpoint_identity(
    common: Any | None,
    *,
    fault_id: str,
    source_sha: str = "1" * 40,
    api_port: int = 23117,
    mcp_port: int = 23118,
    database_port: int = 23119,
) -> dict[str, Any]:
    sentinel = {
        "table": "forwin_recovery_run_sentinel",
        "sentinel_id": hashlib.sha256(fault_id.encode()).hexdigest(),
        "run_id": hashlib.sha256(
            f"run:{fault_id}".encode()
        ).hexdigest()[:32],
        "fault_id": fault_id,
        "source_sha": source_sha,
    }
    record = {
        "schema_version": 1,
        "fault_id": fault_id,
        "run_id": sentinel["run_id"],
        "source_sha": source_sha,
        "source_tree": "2" * 40,
        "project_name": f"forwin-v5-recovery-{sentinel['run_id']}",
        "candidate_manifest_sha256": "3" * 64,
        "candidate_identity_sha256": "4" * 64,
        "sentinel": sentinel,
        "api": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": api_port,
            "endpoint_path": "",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin",
            "container_port": 8899,
            "container_id": "api-container-generalized",
            "image_id": "sha256:" + "5" * 64,
        },
        "mcp": {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": mcp_port,
            "endpoint_path": "/mcp",
            "health_path": "/health",
            "health_status": 200,
            "service": "forwin-mcp",
            "container_port": 8896,
            "container_id": "mcp-container-generalized",
            "image_id": "sha256:" + "5" * 64,
        },
        "database": {
            "scheme": "postgresql",
            "host": "127.0.0.1",
            "port": database_port,
            "database": "forwin",
            "service": "postgres",
            "container_port": 5432,
            "container_id": "postgres-container-generalized",
            "image_id": "sha256:" + "6" * 64,
        },
    }
    record["identity_sha256"] = (
        common.stable_hash(record)
        if common is not None
        else hashlib.sha256(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    return record


def endpoint_with_dependency(
    common: Any,
    *,
    fault_id: str,
    dependency: str,
) -> dict[str, Any]:
    record = endpoint_identity(common, fault_id=fault_id)
    definitions = {
        "qdrant": {
            "port": 23120,
            "health_path": "/readyz",
            "service": "qdrant",
            "container_port": 6333,
            "image": "7",
        },
        "minio": {
            "port": 23121,
            "health_path": "/minio/health/ready",
            "service": "minio",
            "container_port": 9000,
            "image": "8",
        },
    }
    definition = definitions[dependency]
    record[dependency] = {
        "scheme": "http",
        "host": "127.0.0.1",
        "port": definition["port"],
        "endpoint_path": "",
        "health_path": definition["health_path"],
        "health_status": 200,
        "service": definition["service"],
        "container_port": definition["container_port"],
        "container_id": f"{dependency}-container-generalized",
        "image_id": "sha256:" + definition["image"] * 64,
    }
    record["identity_sha256"] = common.stable_hash(
        {
            key: value
            for key, value in record.items()
            if key != "identity_sha256"
        }
    )
    return record


@pytest.mark.parametrize(
    ("dependency", "url"),
    (
        ("qdrant", "http://127.0.0.1:23120"),
        ("minio", "http://127.0.0.1:23121"),
    ),
)
def test_shared_endpoint_binding_supports_exact_typed_optional_dependency(
    common: Any,
    dependency: str,
    url: str,
) -> None:
    fault_id = f"optional-{dependency}-generalized"
    expected = endpoint_with_dependency(
        common,
        fault_id=fault_id,
        dependency=dependency,
    )

    class Controller:
        def bind_endpoints(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs[f"{dependency}_url"] == url
            return expected

    kwargs = {f"{dependency}_url": url}
    bound = common.bind_recovery_endpoints(
        controller=Controller(),
        fault_id=fault_id,
        source_sha="1" * 40,
        api_url="http://127.0.0.1:23117",
        mcp_url="http://127.0.0.1:23118/mcp",
        database_url="postgresql://u:p@127.0.0.1:23119/forwin",
        sentinel_reader=lambda: dict(expected["sentinel"]),
        **kwargs,
    )

    assert bound == expected
    assert sensitive_paths(bound) == []


def test_shared_endpoint_binding_rejects_wrong_optional_service_identity(
    common: Any,
) -> None:
    fault_id = "wrong-qdrant-service-generalized"
    expected = endpoint_with_dependency(
        common,
        fault_id=fault_id,
        dependency="qdrant",
    )
    expected["qdrant"]["service"] = "minio"
    expected["identity_sha256"] = common.stable_hash(
        {
            key: value
            for key, value in expected.items()
            if key != "identity_sha256"
        }
    )

    class Controller:
        def bind_endpoints(self, **_kwargs: Any) -> dict[str, Any]:
            return expected

    with pytest.raises(common.SetupBlocked, match="qdrant endpoint"):
        common.bind_recovery_endpoints(
            controller=Controller(),
            fault_id=fault_id,
            source_sha="1" * 40,
            api_url="http://127.0.0.1:23117",
            mcp_url="http://127.0.0.1:23118/mcp",
            database_url="postgresql://u:p@127.0.0.1:23119/forwin",
            qdrant_url="http://127.0.0.1:23120",
            sentinel_reader=lambda: dict(expected["sentinel"]),
        )


def test_shared_endpoint_binding_requires_exact_loopback_run_and_db_sentinel(
    common: Any,
) -> None:
    fault_id = "endpoint-binding-generalized"
    expected = endpoint_identity(common, fault_id=fault_id)
    calls: list[Any] = []

    class Controller:
        def bind_endpoints(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("controller", kwargs))
            return expected

    def sentinel_reader() -> dict[str, str]:
        calls.append(("sentinel",))
        return dict(expected["sentinel"])

    bound = common.bind_recovery_endpoints(
        controller=Controller(),
        fault_id=fault_id,
        source_sha="1" * 40,
        api_url="http://127.0.0.1:23117",
        mcp_url="http://127.0.0.1:23118/mcp",
        database_url=(
            "postgresql://isolated-user:isolated-password@"
            "127.0.0.1:23119/forwin"
        ),
        sentinel_reader=sentinel_reader,
    )

    assert bound == expected
    assert [item[0] for item in calls] == ["controller", "sentinel"]
    assert sensitive_paths(bound) == []
    assert "isolated-password" not in json.dumps(bound)


@pytest.mark.parametrize("interruption", (KeyboardInterrupt, SystemExit))
def test_shared_controller_owns_fresh_up_interrupt_through_return_boundary(
    common: Any,
    interruption: type[BaseException],
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def execute(args: list[str], **_kwargs: Any) -> Any:
        calls.append(list(args))
        if "fresh-up" in args:
            raise interruption()
        return SimpleNamespace(returncode=0, stdout="{}\n", stderr="")

    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}", encoding="utf-8")
    controller = common.RecoveryController(
        candidate_manifest=candidate,
        evidence_dir=tmp_path / "evidence",
        execute=execute,
        python_executable="/python",
    )

    with pytest.raises(interruption):
        controller.fresh_up("fresh-return-boundary")

    assert [call[2] for call in calls] == [
        "fresh-up",
        "interrupt-cleanup",
    ]


@pytest.mark.parametrize(
    "observed",
    ("", "http://127.0.0.1:23999"),
)
def test_shared_client_endpoint_must_match_bound_configuration(
    common: Any,
    observed: str,
) -> None:
    client = SimpleNamespace(api_url=observed)

    with pytest.raises(common.SetupBlocked, match="client endpoint"):
        common.require_client_endpoint(
            client,
            attribute="api_url",
            expected_url="http://127.0.0.1:23117",
            label="API",
        )


@pytest.mark.parametrize(
    ("api_url", "mcp_url", "database_url"),
    (
        (
            "http://cross-stack.invalid:23117",
            "http://127.0.0.1:23118/mcp",
            "postgresql://u:p@127.0.0.1:23119/forwin",
        ),
        (
            "http://127.0.0.1:23117",
            "http://127.0.0.1:23118/wrong",
            "postgresql://u:p@127.0.0.1:23119/forwin",
        ),
        (
            "http://127.0.0.1:23117",
            "http://127.0.0.1:23118/mcp",
            "postgresql://u:p@127.0.0.1:23129/forwin",
        ),
    ),
)
def test_shared_endpoint_binding_rejects_cross_stack_values_before_db_query(
    common: Any,
    api_url: str,
    mcp_url: str,
    database_url: str,
) -> None:
    fault_id = "endpoint-negative-generalized"
    expected = endpoint_identity(common, fault_id=fault_id)
    sentinel_called = False

    class Controller:
        def bind_endpoints(self, **_kwargs: Any) -> dict[str, Any]:
            return expected

    def sentinel_reader() -> dict[str, str]:
        nonlocal sentinel_called
        sentinel_called = True
        return dict(expected["sentinel"])

    with pytest.raises(common.SetupBlocked):
        common.bind_recovery_endpoints(
            controller=Controller(),
            fault_id=fault_id,
            source_sha="1" * 40,
            api_url=api_url,
            mcp_url=mcp_url,
            database_url=database_url,
            sentinel_reader=sentinel_reader,
        )

    assert sentinel_called is False


def test_shared_endpoint_binding_rejects_sentinel_or_identity_drift(
    common: Any,
) -> None:
    fault_id = "endpoint-sentinel-generalized"
    expected = endpoint_identity(common, fault_id=fault_id)

    class Controller:
        def bind_endpoints(self, **_kwargs: Any) -> dict[str, Any]:
            return expected

    for mutation in ("sentinel", "identity", "project"):
        observed = dict(expected["sentinel"])
        if mutation == "sentinel":
            observed["run_id"] = "f" * 32
        elif mutation == "identity":
            expected["identity_sha256"] = "0" * 64
        else:
            expected["project_name"] = (
                "cross-stack-prefix-" + expected["run_id"]
            )
            expected["identity_sha256"] = common.stable_hash(
                {
                    key: value
                    for key, value in expected.items()
                    if key != "identity_sha256"
                }
            )
        with pytest.raises(common.SetupBlocked):
            common.bind_recovery_endpoints(
                controller=Controller(),
                fault_id=fault_id,
                source_sha="1" * 40,
                api_url="http://127.0.0.1:23117",
                mcp_url="http://127.0.0.1:23118/mcp",
                database_url="postgresql://u:p@127.0.0.1:23119/forwin",
                sentinel_reader=lambda observed=observed: observed,
            )
        expected = endpoint_identity(common, fault_id=fault_id)


def test_shared_http_json_forwards_auth_headers_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
    common: Any,
) -> None:
    observed: dict[str, Any] = {}
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "environment-operator")
    monkeypatch.setenv("FORWIN_HTTP_BASIC_PASSWORD", "environment-password")

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def urlopen(request: Any, *, timeout: float) -> Response:
        observed["headers"] = dict(request.header_items())
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(common.urllib.request, "urlopen", urlopen)
    result = common.http_json(
        "POST",
        "http://forwin.invalid/api",
        json_body={"fixture": "generic"},
        headers={
            "Authorization": "Basic private",
            "X-Forwin-Extension-Key": "private-extension",
        },
        timeout_seconds=7,
    )

    assert result == {"ok": True}
    assert observed["timeout"] == 7
    assert observed["headers"]["Authorization"] == "Basic private"
    assert (
        observed["headers"]["X-forwin-extension-key"]
        == "private-extension"
    )


def test_shared_http_json_adds_complete_environment_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
    common: Any,
) -> None:
    observed: dict[str, Any] = {}
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "release-operator")
    monkeypatch.setenv("FORWIN_HTTP_BASIC_PASSWORD", "release-password")

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def urlopen(request: Any, *, timeout: float) -> Response:
        observed["headers"] = dict(request.header_items())
        return Response()

    monkeypatch.setattr(common.urllib.request, "urlopen", urlopen)

    assert common.http_json("GET", "http://forwin.invalid/api") == {"ok": True}
    expected = "Basic " + base64.b64encode(
        b"release-operator:release-password"
    ).decode("ascii")
    assert observed["headers"]["Authorization"] == expected


def test_shared_http_json_rejects_partial_environment_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
    common: Any,
) -> None:
    monkeypatch.setenv("FORWIN_HTTP_BASIC_USER", "release-operator")
    monkeypatch.delenv("FORWIN_HTTP_BASIC_PASSWORD", raising=False)

    def unexpected_urlopen(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("HTTP request must not be sent")

    monkeypatch.setattr(common.urllib.request, "urlopen", unexpected_urlopen)

    with pytest.raises(common.RunnerError, match="must be set together"):
        common.http_json("GET", "http://forwin.invalid/api")


def claim_response(fixture: Any, client_id: str) -> dict[str, Any]:
    return {
        "found": True,
        "server_time": "2026-07-26T12:00:00+00:00",
        "retry_after_seconds": 0,
        "claim": {
            "execution_mode": "execute",
            "job": {
                "job_id": fixture.job_id,
                "idempotency_key": fixture.logical_key,
                "task_kind": "chapter_upload",
                "platform": fixture.platform_id,
                "content_sha256": fixture.body_sha256,
                "input": {
                    "book_name": fixture.book_name,
                    "chapter_title": fixture.chapter_title,
                    "body": fixture.body,
                    "publish": False,
                    "create_if_missing": False,
                    "upload_url": None,
                    "book_meta": None,
                },
            },
            "attempt": {
                "attempt_id": f"attempt-{fixture.job_id}",
                "attempt_number": 1,
                "lease_epoch": 3,
                "phase": "claimed",
                "lease_expires_at": "2026-07-26T12:05:00+00:00",
                "heartbeat_interval_seconds": 30,
            },
        },
    }


def test_publisher_api_uses_production_claim_pause_resume_and_heartbeat_routes(
    runner: Any,
) -> None:
    fixture = runner.publisher_fixture("publisher_captcha", "fault-api")
    client_id = "publisher-recovery-client-fault-api"
    calls: list[dict[str, Any]] = []
    pause_token = f"attempt-{fixture.job_id}"
    transition = {
        "pause_token": pause_token,
        "pause_reason": "captcha",
        "actor": "basic:recovery-operator",
        "auth_method": "basic",
        "operator_reason": runner.OPERATOR_REASON,
        "transitioned_at": "2026-07-26T12:01:00+00:00",
        "attempt_id": pause_token,
        "attempt_kind": "execute",
        "attempt_phase": "claimed",
        "old_state": "paused",
        "new_state": "pending",
    }
    responses = [
        claim_response(fixture, client_id),
        {
            "disposition": "applied",
            "pause_reason": "captcha",
            "pause_token": pause_token,
            "job_status": "paused",
            "attempt_status": "paused",
            "phase": "claimed",
        },
        {
            "ok": True,
            "disposition": "applied",
            "server_time": "2026-07-26T12:01:00+00:00",
            "job": {"job_id": fixture.job_id},
            "transition": transition,
        },
        {
            "ok": True,
            "disposition": "idempotent",
            "server_time": "2026-07-26T12:01:01+00:00",
            "job": {"job_id": fixture.job_id},
            "transition": transition,
        },
        {
            "ok": True,
            "client_id": client_id,
            "last_heartbeat_at": "2026-07-26T12:01:02+00:00",
            "recent_platforms": ["qidian"],
        },
    ]

    def transport(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "url": url,
                **kwargs,
            }
        )
        return responses.pop(0)

    api = runner.PublisherAPI(
        api_url="http://forwin.invalid",
        extension_key="extension-private",
        operator_username="recovery-operator",
        operator_password="operator-private",
        transport=transport,
    )
    claim = api.claim(fixture, client_id)
    pause = api.pause(
        fixture,
        claim,
        risk_reason="captcha",
        observed_at="2026-07-26T12:00:30+00:00",
    )
    replay = api.resume_twice(
        fixture,
        pause_token=pause_token,
        risk_reason="captcha",
    )
    heartbeat = api.heartbeat(client_id=client_id)

    assert claim["attempt"]["lease_epoch"] == 3
    assert pause["pause_token"] == pause_token
    assert replay["first_disposition"] == "applied"
    assert replay["replay_disposition"] == "idempotent"
    assert heartbeat["status"] == "healthy"
    assert [(call["method"], call["url"]) for call in calls] == [
        (
            "POST",
            "http://forwin.invalid/api/publishers/extension/upload-jobs/claim",
        ),
        (
            "POST",
            "http://forwin.invalid/api/publishers/extension/upload-jobs/"
            f"{fixture.job_id}/attempts/{pause_token}/pause",
        ),
        (
            "POST",
            "http://forwin.invalid/api/publishers/upload-jobs/"
            f"{fixture.job_id}/resume",
        ),
        (
            "POST",
            "http://forwin.invalid/api/publishers/upload-jobs/"
            f"{fixture.job_id}/resume",
        ),
        (
            "GET",
            "http://forwin.invalid/api/publishers/extension/heartbeat-status",
        ),
    ]
    extension_calls = (calls[0], calls[1], calls[4])
    assert all(
        call["headers"] == {
            "X-Forwin-Extension-Key": "extension-private"
        }
        for call in extension_calls
    )
    expected_basic = "Basic " + base64.b64encode(
        b"recovery-operator:operator-private"
    ).decode("ascii")
    assert calls[2]["headers"] == {"Authorization": expected_basic}
    assert calls[3]["headers"] == {"Authorization": expected_basic}
    assert "operator-private" not in json.dumps(replay)
    assert "extension-private" not in json.dumps(
        [claim, pause, replay, heartbeat]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (("attempt", "lease_epoch", 0), "fence"),
        (("attempt", "phase", "mutation_started"), "pre-mutation"),
        (("job", "job_id", "wrong-job"), "job identity"),
    ),
)
def test_claim_rejects_wrong_job_or_pre_mutation_fence(
    runner: Any,
    mutation: tuple[str, str, Any],
    message: str,
) -> None:
    fixture = runner.publisher_fixture("publisher_mfa", "fault-claim-drift")
    payload = claim_response(fixture, "recovery-client")
    section, field, value = mutation
    payload["claim"][section][field] = value
    api = runner.PublisherAPI(
        api_url="http://forwin.invalid",
        extension_key="extension-private",
        operator_username="operator",
        operator_password="private",
        transport=lambda *_args, **_kwargs: payload,
    )

    with pytest.raises(runner.SetupBlocked, match=message):
        api.claim(fixture, "recovery-client")


@pytest.mark.parametrize(
    ("kind", "wrong_reason"),
    (
        ("publisher_captcha", "mfa"),
        ("publisher_mfa", "account_risk"),
        ("publisher_account_risk", "captcha"),
    ),
)
def test_pause_rejects_wrong_typed_risk_reason(
    runner: Any,
    kind: str,
    wrong_reason: str,
) -> None:
    fixture = runner.publisher_fixture(kind, f"fault-{kind}")
    api = runner.PublisherAPI(
        api_url="http://forwin.invalid",
        extension_key="extension-private",
        operator_username="operator",
        operator_password="private",
        transport=lambda *_args, **_kwargs: {},
    )

    with pytest.raises(runner.SetupBlocked, match="typed risk reason"):
        api.pause(
            fixture,
            claim_response(fixture, "recovery-client")["claim"],
            risk_reason=wrong_reason,
            observed_at="2026-07-26T12:00:30+00:00",
        )


def terminal_lock_rows(
    *,
    holder_name: str,
    waiter_name: str = "forwin-recovery-publisher-worker",
    blocking_pids: list[int] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "pid": 4100,
            "granted": True,
            "application_name": holder_name,
            "query": "SELECT pg_advisory_lock($1)",
            "wait_event_type": "",
            "blocking_pids": [],
        },
        {
            "pid": 4200,
            "granted": False,
            "application_name": waiter_name,
            "query": "UPDATE publisher_upload_jobs SET status=$1",
            "wait_event_type": "Lock",
            "blocking_pids": blocking_pids or [4100],
        },
    ]


def test_terminal_barrier_observation_requires_exact_holder_waiter_and_owner(
    runner: Any,
) -> None:
    barrier = SimpleNamespace(
        holder_application_name="publisher_recovery_holder",
        target_table="publisher_upload_jobs",
        job_id="job-generalized",
    )
    observation = runner.terminal_barrier_observation(
        barrier=barrier,
        rows=terminal_lock_rows(
            holder_name=barrier.holder_application_name
        ),
        job_id="job-generalized",
        expected_owner_token="backend:owner-one",
        current_owner_token="backend:owner-one",
    )

    assert observation.waiter_pid == 4200
    assert observation.owner_token == "backend:owner-one"
    assert observation.waiter_application_name == (
        "forwin-recovery-publisher-worker"
    )


@pytest.mark.parametrize(
    ("rows", "current_owner", "message"),
    (
        ([], "backend:owner-one", "one holder and one waiter"),
        (
            terminal_lock_rows(
                holder_name="publisher_recovery_holder",
                waiter_name="forwin-recovery-outbox-worker",
            ),
            "backend:owner-one",
            "publisher-worker",
        ),
        (
            terminal_lock_rows(holder_name="publisher_recovery_holder"),
            "backend:owner-two",
            "owner token drift",
        ),
        (
            terminal_lock_rows(
                holder_name="publisher_recovery_holder",
                blocking_pids=[9999],
            ),
            "backend:owner-one",
            "scoped holder",
        ),
    ),
)
def test_terminal_barrier_rejects_missing_or_drifted_boundary(
    runner: Any,
    rows: list[dict[str, Any]],
    current_owner: str,
    message: str,
) -> None:
    barrier = SimpleNamespace(
        holder_application_name="publisher_recovery_holder",
        target_table="publisher_upload_jobs",
        job_id="job-generalized",
    )

    with pytest.raises(runner.SetupBlocked, match=message):
        runner.terminal_barrier_observation(
            barrier=barrier,
            rows=rows,
            job_id="job-generalized",
            expected_owner_token="backend:owner-one",
            current_owner_token=current_owner,
        )


def test_terminal_barrier_rejects_job_identity_drift(runner: Any) -> None:
    barrier = SimpleNamespace(
        holder_application_name="publisher_recovery_holder",
        target_table="publisher_upload_jobs",
        job_id="other-job",
    )

    with pytest.raises(runner.SetupBlocked, match="job identity drift"):
        runner.terminal_barrier_observation(
            barrier=barrier,
            rows=terminal_lock_rows(
                holder_name=barrier.holder_application_name
            ),
            job_id="job-generalized",
            expected_owner_token="backend:owner-one",
            current_owner_token="backend:owner-one",
        )


@pytest.mark.parametrize(
    "kind",
    (
        "publisher_backend_unavailable",
        "publisher_browser_unavailable",
        "publisher_captcha",
        "publisher_mfa",
        "publisher_account_risk",
    ),
)
def test_fixture_insert_is_the_only_direct_business_setup_and_stays_safe(
    runner: Any,
    kind: str,
) -> None:
    fixture = runner.publisher_fixture(kind, f"fault-sql-{kind}")
    statement, parameters = runner.fixture_insert(fixture)
    normalized_sql = " ".join(statement.split()).lower()

    assert normalized_sql.startswith("insert into publisher_upload_jobs")
    assert "update publisher_upload_jobs" not in normalized_sql
    assert "publisher_upload_attempts" not in normalized_sql
    assert parameters["id"] == fixture.job_id
    assert parameters["project_id"] == ""
    assert parameters["idempotency_key"] == fixture.logical_key
    assert parameters["publish"] is False
    assert parameters["result_payload_json"] == json.dumps(
        fixture.result_payload,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert sensitive_paths(parameters) == []
    assert "receipt" not in parameters["result_payload_json"].lower()
    if kind == "publisher_browser_unavailable":
        assert parameters["preclaim_delay_seconds"] == 3600
    else:
        assert parameters["preclaim_delay_seconds"] == 0


def test_stale_token_probe_requires_exact_production_rejection(
    runner: Any,
) -> None:
    result = runner.validate_stale_token_response(
        job_id="job-generalized",
        stale_owner_token="backend:old",
        current_owner_token="backend:new",
        response={"ok": False, "stale_claim": True},
    )
    assert result["response"] == {"ok": False, "stale_claim": True}

    for response in (
        {"ok": True, "stale_claim": False},
        {"ok": False, "stale_claim": False},
        {"stale_claim": True},
    ):
        with pytest.raises(runner.SetupBlocked, match="stale owner token"):
            runner.validate_stale_token_response(
                job_id="job-generalized",
                stale_owner_token="backend:old",
                current_owner_token="backend:new",
                response=response,
            )

    with pytest.raises(runner.SetupBlocked, match="owner token identity"):
        runner.validate_stale_token_response(
            job_id="job-generalized",
            stale_owner_token="backend:same",
            current_owner_token="backend:same",
            response={"ok": False, "stale_claim": True},
        )


@pytest.mark.parametrize(
    ("username", "password", "message"),
    (
        ("", "private", "Basic credentials"),
        ("operator", "", "Basic credentials"),
    ),
)
def test_operator_resume_cannot_be_configured_without_authentication(
    runner: Any,
    username: str,
    password: str,
    message: str,
) -> None:
    with pytest.raises(runner.SetupBlocked, match=message):
        runner.PublisherAPI(
            api_url="http://forwin.invalid",
            extension_key="extension-private",
            operator_username=username,
            operator_password=password,
        )


def test_shared_evidence_writer_reads_typed_risk_marker_times(
    tmp_path: Path,
    common: Any,
) -> None:
    writer = object.__new__(common.EvidenceWriter)
    writer.evidence_dir = tmp_path
    writer.finalizer = SimpleNamespace(SERVICE_FAULTS={})
    fault_time, recovery_time = writer._fault_times(
        "publisher_account_risk",
        "fault-marker",
        [
            {
                "action": "fault_marked",
                "fault_id": "fault-marker",
                "fault_kind": "publisher_account_risk",
                "fault_time": "2026-07-26T12:00:00+00:00",
            },
            {
                "action": "recovery_marked",
                "fault_id": "fault-marker",
                "fault_kind": "publisher_account_risk",
                "recovery_time": "2026-07-26T12:01:00+00:00",
            },
        ],
    )

    assert fault_time == "2026-07-26T12:00:00+00:00"
    assert recovery_time == "2026-07-26T12:01:00+00:00"


@pytest.mark.parametrize("kind", (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
))
def test_cli_matches_task4_run_shape_for_every_publisher_fault(
    runner: Any,
    kind: str,
) -> None:
    args = runner.parse_args(
        [
            "run",
            "--fault-kind",
            kind,
            "--fault-id",
            f"fault-cli-{kind}",
            "--candidate-manifest",
            "/tmp/candidate.json",
            "--mcp-url",
            "http://127.0.0.1:19096",
            "--api-url",
            "http://127.0.0.1:19099",
            "--database-url-env",
            "FORWIN_RECOVERY_DATABASE_URL",
            "--evidence-dir",
            f"/tmp/evidence-{kind}",
        ]
    )

    assert args.command == "run"
    assert args.fault_kind == kind
    assert args.database_url_env == "FORWIN_RECOVERY_DATABASE_URL"


class FakeWriter:
    def __init__(self, tmp_path: Path, log: list[Any]) -> None:
        self.path = tmp_path / "fault-report.json"
        self.log = log
        self.pass_payload: dict[str, Any] | None = None
        self.blocked_payload: dict[str, Any] | None = None

    def write_pass_report(self, **kwargs: Any) -> Path:
        self.log.append("write_pass")
        self.pass_payload = kwargs
        return self.path

    def write_setup_blocked(self, **kwargs: Any) -> Path:
        self.log.append("write_setup_blocked")
        self.blocked_payload = kwargs
        return self.path


class FakeController:
    def __init__(
        self,
        tmp_path: Path,
        log: list[Any],
        endpoint_record: dict[str, Any],
    ) -> None:
        self.event_log_path = tmp_path / "stack-events.jsonl"
        self.log = log
        self.inventory_count = 0
        self.endpoint_record = endpoint_record
        self.holds: dict[tuple[str, str], tuple[str, str]] = {}

    def fresh_up(self, fault_id: str) -> None:
        self.log.append(("fresh_up", fault_id))

    def bind_endpoints(self, **kwargs: Any) -> dict[str, Any]:
        self.log.append(("bind_endpoints", kwargs["fault_id"]))
        return self.endpoint_record

    def setup_hold(
        self,
        service: str,
        fault_id: str,
        hold_id: str,
        *,
        fault_kind: str,
        purpose: str = "auxiliary",
    ) -> None:
        self.holds[(service, hold_id)] = (fault_kind, purpose)
        self.log.append(
            (
                "setup_hold",
                service,
                fault_id,
                hold_id,
                fault_kind,
                purpose,
            )
        )

    def setup_release(self, service: str, fault_id: str, hold_id: str) -> None:
        self.holds.pop((service, hold_id))
        self.log.append(("setup_release", service, fault_id, hold_id))

    def setup_discard(
        self, service: str, fault_id: str, hold_id: str
    ) -> dict[str, Any]:
        self.log.append(("setup_discard", service, fault_id, hold_id))
        fault_kind, purpose = self.holds.pop((service, hold_id))
        return {
            "action": "setup_service_discarded",
            "fault_id": fault_id,
            "hold_id": hold_id,
            "service": service,
            "fault_kind": fault_kind,
            "purpose": purpose,
            "after": {
                "service": service,
                "exists": True,
                "running": False,
                "container_id": f"{service}-container-generalized",
                "image_id": "sha256:" + "f" * 64,
            },
        }

    def kill(self, service: str, fault_id: str) -> None:
        self.log.append(("kill", service, fault_id))

    def stop(self, service: str, fault_id: str) -> None:
        self.log.append(("stop", service, fault_id))

    def start(self, service: str, fault_id: str) -> None:
        self.log.append(("start", service, fault_id))

    def mark(self, kind: str, phase: str, fault_id: str) -> None:
        self.log.append(("mark", kind, phase, fault_id))

    def file_inventory(
        self, service: str, fault_id: str, root: str
    ) -> dict[str, Any]:
        self.inventory_count += 1
        variant = "orphan" if self.inventory_count == 1 else "final"
        self.log.append(("file_inventory", service, fault_id, variant))
        return {
            "root": root,
            "root_exists": True,
            "files": [
                {
                    "path": f"{variant}.png",
                    "size": 67,
                    "content_sha256": hashlib.sha256(
                        variant.encode()
                    ).hexdigest(),
                }
            ],
        }

    def destroy(self) -> None:
        self.log.append("destroy")

    def abort(self, fault_id: str, stage: str, reason: str) -> None:
        self.log.append(("abort", fault_id, stage, reason))

    def interrupt_cleanup(self, fault_id: str) -> None:
        self.log.append(("interrupt_cleanup", fault_id))


class FakeSQLCollector:
    def __init__(
        self,
        log: list[Any],
        endpoint_record: dict[str, Any],
    ) -> None:
        self.log = log
        self.current_owner = ""
        self.endpoint_record = endpoint_record

    def read_recovery_sentinel(self) -> dict[str, str]:
        self.log.append("read_sentinel")
        return dict(self.endpoint_record["sentinel"])

    def bind_endpoint_identity(
        self,
        endpoint_record: dict[str, Any],
    ) -> None:
        assert endpoint_record == self.endpoint_record
        self.log.append("bind_endpoint_identity")

    def insert_fixture(self, fixture: Any) -> None:
        self.log.append(("insert_fixture", fixture.job_id))

    def wait_backend_owner(
        self, fixture: Any, *, previous_owner_token: str = ""
    ) -> str:
        self.current_owner = (
            "backend:owner-old"
            if not previous_owner_token
            else "backend:owner-new"
        )
        self.log.append(("wait_backend_owner", self.current_owner))
        return self.current_owner

    def owner_token(self, fixture: Any) -> str:
        return self.current_owner

    def backend_snapshot(self, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        self.log.append(("snapshot", stage))
        return {"stage": stage, "fixture": kwargs["fixture"].job_id}

    def browser_snapshot(self, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        self.log.append(("snapshot", stage))
        return {"stage": stage, "fixture": kwargs["fixture"].job_id}

    def risk_snapshot(self, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        self.log.append(("snapshot", stage))
        return {"stage": stage, "fixture": kwargs["fixture"].job_id}

    def risk_terminal_state(self, fixture: Any) -> dict[str, Any]:
        self.log.append(("risk_terminal_state", fixture.job_id))
        return {
            "job": {"job_id": fixture.job_id},
            "attempts": [],
            "receipts": [],
            "resume_actions": [],
        }

    def wait_status(self, fixture: Any, expected: str) -> None:
        self.log.append(("wait_status", expected))


class FakeBarrier:
    def __init__(
        self,
        log: list[Any],
        *,
        fail_wait: bool = False,
        cleanup_residue: bool = False,
    ) -> None:
        self.log = log
        self.fail_wait = fail_wait
        self.cleanup_residue = cleanup_residue
        self.observations: list[dict[str, Any]] = []
        self.last_residue: dict[str, int] | None = None
        self.job_id = ""

    def install(self, *, job_id: str) -> None:
        self.job_id = job_id
        self.log.append(("barrier_install", job_id))

    def wait_for_blocked_terminal(
        self,
        *,
        expected_owner_token: str,
        owner_reader: Any,
    ) -> dict[str, Any]:
        self.log.append(("barrier_wait", expected_owner_token))
        if self.fail_wait:
            raise RuntimeError("missing deterministic barrier")
        assert owner_reader() == expected_owner_token
        observation = {
            "job_id": self.job_id,
            "owner_token": expected_owner_token,
        }
        self.observations.append(observation)
        return observation

    def cleanup(self) -> None:
        self.log.append("barrier_cleanup")
        self.last_residue = {
            "trigger_count": int(self.cleanup_residue),
            "function_count": 0,
            "scope_table_count": 0,
            "advisory_lock_count": 0,
        }
        if self.cleanup_residue:
            raise RuntimeError("barrier cleanup residue")


class FakePublisherAPI:
    api_url = "http://127.0.0.1:23117"

    def __init__(self, log: list[Any]) -> None:
        self.log = log

    def wait_heartbeat(
        self, status: str, *, client_id: str = ""
    ) -> dict[str, str]:
        observed = client_id or "browser-generalized"
        self.log.append(("heartbeat", status, observed))
        return {
            "browser_id": observed,
            "probe": "extension_heartbeat_status",
            "status": status,
        }

    def claim(self, fixture: Any, client_id: str) -> dict[str, Any]:
        self.log.append(("claim", fixture.job_id, client_id))
        return {
            "client_id": client_id,
            "job": {"job_id": fixture.job_id},
            "attempt": {
                "attempt_id": f"attempt-{fixture.job_id}",
                "lease_epoch": 1,
                "phase": "claimed",
            },
        }

    def pause(
        self,
        fixture: Any,
        claim: dict[str, Any],
        *,
        risk_reason: str,
        observed_at: str,
    ) -> dict[str, str]:
        del observed_at
        self.log.append(("pause", fixture.job_id, risk_reason))
        return {"pause_token": claim["attempt"]["attempt_id"]}

    def resume_twice(
        self,
        fixture: Any,
        *,
        pause_token: str,
        risk_reason: str,
    ) -> dict[str, str]:
        self.log.append(
            ("resume_twice", fixture.job_id, pause_token, risk_reason)
        )
        return {
            "pause_token": pause_token,
            "pause_reason": risk_reason,
        }


@pytest.fixture
def runner_module(runner: Any) -> Any:
    return runner


def make_live_runner(
    runner_module: Any,
    tmp_path: Path,
    kind: str,
    log: list[Any],
    *,
    barrier: Any | None = None,
) -> tuple[Any, FakeWriter]:
    writer = FakeWriter(tmp_path, log)
    fault_id = f"fault-live-{kind}"
    endpoint_record = endpoint_identity(
        None,
        fault_id=fault_id,
    )
    return (
        runner_module.LiveRunner(
            fault_kind=kind,
            fault_id=fault_id,
            source_sha="1" * 40,
            database_url="postgresql://u:p@127.0.0.1:23119/forwin",
            mcp_url="http://127.0.0.1:23118/mcp",
            api_url="http://127.0.0.1:23117",
            controller=FakeController(tmp_path, log, endpoint_record),
            sql_collector=FakeSQLCollector(log, endpoint_record),
            api=FakePublisherAPI(log),
            writer=writer,
            barrier_factory=(lambda: barrier) if barrier is not None else None,
            stale_probe=lambda **kwargs: {
                "observation_id": "stale-generalized",
                "job_id": kwargs["job_id"],
                "stale_owner_token": kwargs["stale_owner_token"],
                "current_owner_token": kwargs["current_owner_token"],
                "response": {"ok": False, "stale_claim": True},
            },
        ),
        writer,
    )


def test_backend_live_sequence_kills_only_after_durable_blocked_owner(
    runner_module: Any,
    tmp_path: Path,
) -> None:
    log: list[Any] = []
    barrier = FakeBarrier(log)
    live, writer = make_live_runner(
        runner_module,
        tmp_path,
        "publisher_backend_unavailable",
        log,
        barrier=barrier,
    )

    result = live.run()

    assert result.status == "pass"
    assert writer.pass_payload is not None
    assert list(writer.pass_payload["snapshots"]) == [
        "before",
        "during",
        "after",
    ]
    old_wait = log.index(("barrier_wait", "backend:owner-old"))
    kill = log.index(
        (
            "kill",
            "publisher-worker",
            "fault-live-publisher_backend_unavailable",
        )
    )
    new_wait = log.index(("barrier_wait", "backend:owner-new"))
    assert old_wait < log.index(("snapshot", "before")) < kill
    assert kill < log.index(("snapshot", "during")) < new_wait
    assert new_wait < log.index("barrier_cleanup")
    assert log[-2:] == ["destroy", "write_pass"]


def test_backend_missing_barrier_and_cleanup_residue_are_setup_blocked(
    runner_module: Any,
    tmp_path: Path,
) -> None:
    for suffix, barrier, fragment in (
        (
            "missing",
            FakeBarrier([], fail_wait=True),
            "missing deterministic barrier",
        ),
        (
            "residue",
            FakeBarrier([], cleanup_residue=True),
            "barrier cleanup residue",
        ),
    ):
        log: list[Any] = []
        barrier.log = log
        live, writer = make_live_runner(
            runner_module,
            tmp_path / suffix,
            "publisher_backend_unavailable",
            log,
            barrier=barrier,
        )

        result = live.run()

        assert result.status == "setup_blocked"
        assert writer.blocked_payload is not None
        combined = (
            writer.blocked_payload["failure_reason"]
            + " "
            + " ".join(writer.blocked_payload["cleanup_errors"])
        )
        assert fragment in combined
        assert "write_pass" not in log


def test_browser_live_sequence_is_preclaim_and_heartbeat_only(
    runner_module: Any,
    tmp_path: Path,
) -> None:
    log: list[Any] = []
    live, writer = make_live_runner(
        runner_module,
        tmp_path,
        "publisher_browser_unavailable",
        log,
    )

    result = live.run()

    assert result.status == "pass"
    assert writer.pass_payload is not None
    assert [item for item in log if isinstance(item, tuple) and item[0] == "heartbeat"] == [
        ("heartbeat", "healthy", "browser-generalized"),
        ("heartbeat", "stale", "browser-generalized"),
        ("heartbeat", "healthy", "browser-generalized"),
    ]
    assert not any(
        isinstance(item, tuple) and item[0] in {"claim", "pause", "resume_twice"}
        for item in log
    )


@pytest.mark.parametrize(
    ("kind", "reason"),
    (
        ("publisher_captcha", "captcha"),
        ("publisher_mfa", "mfa"),
        ("publisher_account_risk", "account_risk"),
    ),
)
def test_typed_risk_live_sequences_mark_pause_resume_and_recovery_exactly_once(
    runner_module: Any,
    tmp_path: Path,
    kind: str,
    reason: str,
) -> None:
    log: list[Any] = []
    live, writer = make_live_runner(
        runner_module,
        tmp_path,
        kind,
        log,
    )

    result = live.run()

    assert result.status == "pass"
    fault_id = f"fault-live-{kind}"
    pause = log.index(("pause", live.fixture.job_id, reason))
    fault_mark = log.index(("mark", kind, "fault", fault_id))
    resume = log.index(
        (
            "resume_twice",
            live.fixture.job_id,
            f"attempt-{live.fixture.job_id}",
            reason,
        )
    )
    recovery_mark = log.index(("mark", kind, "recovery", fault_id))
    discard = log.index(
        (
            "setup_discard",
            "publisher-browser",
            fault_id,
            f"risk-fixture-{fault_id}",
        )
    )
    after = log.index(("snapshot", "after"))
    assert pause < fault_mark < log.index(("snapshot", "during"))
    assert (
        log.index(("snapshot", "during"))
        < resume
        < recovery_mark
        < discard
        < after
    )
    assert not any(
        isinstance(item, tuple) and item[0] == "setup_release"
        for item in log
    )
    assert not any(
        isinstance(item, tuple)
        and item[:2] == ("start", "publisher-browser")
        for item in log
    )
    assert sum(
        item == ("mark", kind, "fault", fault_id) for item in log
    ) == 1
    assert sum(
        item == ("mark", kind, "recovery", fault_id) for item in log
    ) == 1
    assert writer.pass_payload is not None


@pytest.mark.parametrize("interruption", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    "point",
    ("fresh_up", "before_fault", "during_fault", "during_cleanup"),
)
def test_interruptions_cleanup_and_reraise_without_emitting_report(
    runner_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
    point: str,
) -> None:
    log: list[Any] = []
    live, writer = make_live_runner(
        runner_module,
        tmp_path,
        "publisher_captcha",
        log,
    )

    if point == "fresh_up":
        monkeypatch.setattr(
            live.controller,
            "fresh_up",
            lambda _fault_id: (_ for _ in ()).throw(interruption()),
        )
    elif point == "before_fault":
        monkeypatch.setattr(
            live.sql,
            "insert_fixture",
            lambda _fixture: (_ for _ in ()).throw(interruption()),
        )
    elif point == "during_fault":
        monkeypatch.setattr(
            live.api,
            "pause",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption()),
        )
    else:
        original_discard = live.controller.setup_discard

        def interrupting_discard(*args: Any, **kwargs: Any) -> dict[str, Any]:
            original_discard(*args, **kwargs)
            raise interruption()

        monkeypatch.setattr(
            live.controller,
            "setup_discard",
            interrupting_discard,
        )

    with pytest.raises(interruption):
        live.run()

    assert ("interrupt_cleanup", live.fault_id) in log
    assert "write_pass" not in log
    assert "write_setup_blocked" not in log
    assert not any(
        isinstance(item, tuple)
        and item[0] == "mark"
        and item[1:3] in {
            (live.fault_kind, "fault"),
            (live.fault_kind, "recovery"),
        }
        for item in log[
            log.index(("interrupt_cleanup", live.fault_id)) + 1 :
        ]
    )


class MemoryPublisherDatabase:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.cover_assets: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.sentinel = endpoint_identity(
            None,
            fault_id="fault-memory-placeholder",
            source_sha="3" * 40,
        )["sentinel"]

    def execute(self, statement: str, parameters: dict[str, Any]) -> int:
        assert "INSERT INTO publisher_upload_jobs" in statement
        self.jobs.append(
            {
                "job_id": parameters["id"],
                "logical_key": parameters["idempotency_key"],
                "task_kind": parameters["task_kind"],
                "project_id": parameters["project_id"],
                "canon_commit_id": "",
                "candidate_id": "",
                "chapter_number": 0,
                "platform_id": parameters["platform_id"],
                "status": "pending",
                "publish": parameters["publish"],
                "book_name": parameters["book_name"],
                "chapter_title": parameters["chapter_title"],
                "body_text": parameters["body_text"],
                "body_sha256": parameters["body_sha256"],
                "owner_token": "",
                "extension_client_id": "",
                "upload_url": "",
                "abort_requested": False,
                "current_attempt_id": "",
                "available_at": "2026-07-22T13:30:00+00:00",
                "reconcile_after": "",
                "claimed_at": "",
                "started_at": "",
                "finished_at": "",
                "deleted_at": "",
                "paused_at": "",
                "pause_reason": "",
                "current_url": "",
                "result_message": "",
                "error_message": "",
                "result_payload_json": parameters["result_payload_json"],
                "created_at": "2026-07-22T12:00:00+00:00",
                "updated_at": "2026-07-22T12:00:00+00:00",
                "database_now": "2026-07-22T12:30:00+00:00",
            }
        )
        return 1

    def query(
        self, statement: str, parameters: Any = ()
    ) -> list[dict[str, Any]]:
        normalized = " ".join(statement.split())
        if "FROM forwin_recovery_run_sentinel" in normalized:
            return [
                {
                    key: value
                    for key, value in self.sentinel.items()
                    if key != "table"
                }
            ]
        if "FROM publisher_upload_jobs" in normalized:
            job_id, logical_key = parameters
            return [
                dict(row)
                for row in self.jobs
                if row["job_id"] == job_id
                or row["logical_key"] == logical_key
            ]
        if "FROM publisher_upload_attempts" in normalized:
            return [
                dict(row)
                for row in self.attempts
                if row["job_id"] == parameters[0]
            ]
        if "FROM publisher_upload_receipts" in normalized:
            return [
                dict(row)
                for row in self.receipts
                if row["job_id"] == parameters[0]
            ]
        if "FROM publisher_cover_assets" in normalized:
            return [
                dict(row)
                for row in self.cover_assets
                if row["asset_id"] in parameters[0]
            ]
        if "FROM publisher_operator_actions" in normalized:
            return [
                dict(row)
                for row in self.actions
                if row["job_id"] == parameters[0]
            ]
        raise AssertionError(f"unexpected SQL: {normalized}")


@pytest.fixture(scope="module")
def evaluator() -> Any:
    return load_module(
        "task6_recovery_evidence",
        ARTIFACT_DIR / "recovery_evidence.py",
    )


def test_sql_collector_rejects_duplicate_fixture_natural_key(
    runner: Any,
) -> None:
    database = MemoryPublisherDatabase()
    collector = runner.SQLCollector(database)
    fixture = runner.publisher_fixture(
        "publisher_browser_unavailable",
        "fault-memory-duplicate",
    )

    collector.insert_fixture(fixture)
    with pytest.raises(runner.SetupBlocked, match="already exists"):
        collector.insert_fixture(fixture)


def test_backend_sql_snapshots_satisfy_strict_evaluator_from_raw_rows(
    runner: Any,
    evaluator: Any,
) -> None:
    database = MemoryPublisherDatabase()
    collector = runner.SQLCollector(database)
    fixture = runner.publisher_fixture(
        "publisher_backend_unavailable",
        "fault-memory-backend",
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="2" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    collector.insert_fixture(fixture)
    job = database.jobs[0]
    job.update(status="running", owner_token="backend:owner-old")
    before = collector.backend_snapshot(
        source_sha="2" * 40,
        fault_id=fixture.fault_id,
        stage="before",
        fixture=fixture,
    )
    orphan_path = ".staging/orphan.part"
    during = collector.backend_snapshot(
        source_sha="2" * 40,
        fault_id=fixture.fault_id,
        stage="during",
        fixture=fixture,
        cover_files=[
            {
                "path": orphan_path,
                "size": 67,
                "content_sha256": hashlib.sha256(b"orphan").hexdigest(),
            }
        ],
    )
    asset_id = "cover-asset-generalized"
    final_relative_path = f"manual/qidian/{asset_id}.png"
    final_path = f"{runner.PUBLISHER_COVER_ROOT}/{final_relative_path}"
    payload = json.loads(job["result_payload_json"])
    payload.update(
        cover_asset_ids=[asset_id],
        selected_cover_asset_id=asset_id,
    )
    job.update(
        status="succeeded",
        owner_token="backend:owner-new",
        result_payload_json=json.dumps(payload, sort_keys=True),
    )
    database.cover_assets = [
        {
            "asset_id": asset_id,
            "file_path": final_path,
            "file_size": 67,
            "mime_type": "image/png",
        }
    ]
    names = runner.barrier_names(fixture.fault_id)
    terminal_writes = [
        {
            "observation_id": f"terminal-{index}",
            "job_id": fixture.job_id,
            "owner_token": owner,
            "holder_pid": 4100,
            "waiter_pid": waiter,
            "waiter_application_name": (
                runner.PUBLISHER_WORKER_APPLICATION_NAME
            ),
            "waiter_role": "forwin",
            "blocking_pids": [4100],
            "trigger_name": names.trigger,
            "function_name": names.function,
            "scope_table": names.scope_table,
            "advisory_key": runner.advisory_key(fixture.fault_id),
        }
        for index, (owner, waiter) in enumerate(
            (
                ("backend:owner-old", 4200),
                ("backend:owner-new", 4300),
            ),
            start=1,
        )
    ]
    stale = runner.validate_stale_token_response(
        job_id=fixture.job_id,
        stale_owner_token="backend:owner-old",
        current_owner_token="backend:owner-new",
        response={"ok": False, "stale_claim": True},
    )
    after = collector.backend_snapshot(
        source_sha="2" * 40,
        fault_id=fixture.fault_id,
        stage="after",
        fixture=fixture,
        cover_files=[
            {
                "path": final_relative_path,
                "size": 67,
                "content_sha256": hashlib.sha256(b"final").hexdigest(),
            }
        ],
        stale_observation=stale,
        terminal_writes=terminal_writes,
        residue={
            "trigger_count": 0,
            "function_count": 0,
            "scope_table_count": 0,
            "advisory_lock_count": 0,
        },
    )
    snapshots = {"before": before, "during": during, "after": after}

    assert evaluator.snapshot_violations(fixture.fault_kind, snapshots) == []
    assertions = evaluator.derive_assertions(fixture.fault_kind, snapshots)
    assert evaluator.assertion_violations(
        fixture.fault_kind,
        assertions,
    ) == []


def test_browser_sql_snapshots_satisfy_strict_evaluator_from_raw_rows(
    runner: Any,
    evaluator: Any,
) -> None:
    database = MemoryPublisherDatabase()
    collector = runner.SQLCollector(database)
    fixture = runner.publisher_fixture(
        "publisher_browser_unavailable",
        "fault-memory-browser",
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="3" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    collector.insert_fixture(fixture)
    snapshots = {
        stage: collector.browser_snapshot(
            source_sha="3" * 40,
            fault_id=fixture.fault_id,
            stage=stage,
            fixture=fixture,
            heartbeat={
                "browser_id": "browser-memory-generalized",
                "probe": "extension_heartbeat_status",
                "status": status,
            },
        )
        for stage, status in (
            ("before", "healthy"),
            ("during", "stale"),
            ("after", "healthy"),
        )
    }

    assert evaluator.snapshot_violations(fixture.fault_kind, snapshots) == []
    assert evaluator.assertion_violations(
        fixture.fault_kind,
        evaluator.derive_assertions(fixture.fault_kind, snapshots),
    ) == []


def test_cover_inventory_requires_the_exact_existing_controller_root(
    runner: Any,
) -> None:
    with pytest.raises(runner.SetupBlocked, match="root"):
        runner.normalize_cover_inventory(
            {
                "root": runner.PUBLISHER_COVER_ROOT,
                "root_exists": False,
                "files": [],
            }
        )


@pytest.mark.parametrize(
    "raw",
    (
        "{not-json",
        "[]",
        '"scalar"',
        "null",
    ),
)
def test_publisher_result_payload_requires_a_json_object(
    runner: Any,
    raw: str,
) -> None:
    with pytest.raises(runner.SetupBlocked, match="JSON object"):
        runner._json_object(raw)


def test_cover_inventory_stays_relative_and_rejects_duplicate_normalized_paths(
    runner: Any,
) -> None:
    digest = hashlib.sha256(b"cover").hexdigest()
    payload = {
        "root": runner.PUBLISHER_COVER_ROOT,
        "root_exists": True,
        "files": [
            {"path": "manual/qidian/cover.png", "size": 5, "content_sha256": digest},
        ],
    }

    assert runner.normalize_cover_inventory(payload) == payload["files"]

    for unsafe_path in (
        "/app/data/publisher_covers/manual/qidian/cover.png",
        "manual//qidian/cover.png",
        "manual/./qidian/cover.png",
        "manual/qidian/../cover.png",
        r"manual\qidian\cover.png",
    ):
        changed = dict(payload)
        changed["files"] = [
            {"path": unsafe_path, "size": 5, "content_sha256": digest}
        ]
        with pytest.raises(runner.SetupBlocked, match="inventory"):
            runner.normalize_cover_inventory(changed)

    duplicated = dict(payload)
    duplicated["files"] = [dict(payload["files"][0]), dict(payload["files"][0])]
    with pytest.raises(runner.SetupBlocked, match="duplicate"):
        runner.normalize_cover_inventory(duplicated)


def test_risk_and_browser_use_one_full_canonical_job_projection(
    runner: Any,
) -> None:
    database = MemoryPublisherDatabase()
    browser_fixture = runner.publisher_fixture(
        "publisher_browser_unavailable",
        "canonical-browser-generalized",
    )
    risk_fixture = runner.publisher_fixture(
        "publisher_captcha",
        "canonical-risk-generalized",
    )
    collector = runner.SQLCollector(database)
    collector.insert_fixture(browser_fixture)
    collector.insert_fixture(risk_fixture)

    browser_job = collector._canonical_job(browser_fixture)
    risk_job = collector.risk_terminal_state(risk_fixture)["job"]

    assert set(risk_job) == set(browser_job)
    assert {
        "available_at",
        "owner_token",
        "current_attempt_id",
        "abort_requested",
        "deleted_at",
        "upload_url",
        "current_url",
        "result_message",
        "error_message",
        "result_payload",
        "created_at",
        "updated_at",
        "database_now",
    } <= set(risk_job)


@pytest.mark.parametrize(
    ("kind", "risk_reason"),
    (
        ("publisher_captcha", "captcha"),
        ("publisher_mfa", "mfa"),
        ("publisher_account_risk", "account_risk"),
    ),
)
def test_risk_sql_snapshots_satisfy_strict_evaluator_from_raw_rows(
    runner: Any,
    evaluator: Any,
    kind: str,
    risk_reason: str,
) -> None:
    database = MemoryPublisherDatabase()
    collector = runner.SQLCollector(database)
    fixture = runner.publisher_fixture(
        kind,
        f"fault-memory-{risk_reason}",
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="4" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    collector.insert_fixture(fixture)
    job = database.jobs[0]
    attempt_id = f"attempt-{risk_reason}-generalized"
    attempt = {
        "attempt_id": attempt_id,
        "job_id": fixture.job_id,
        "attempt_number": 1,
        "attempt_kind": "execute",
        "owner_token": "browser-memory-generalized",
        "lease_epoch": 1,
        "status": "running",
        "phase": "claimed",
        "content_sha256": fixture.body_sha256,
        "error_code": "",
        "result_json": "{}",
    }
    database.attempts = [attempt]
    job.update(
        status="running",
        owner_token="browser-memory-generalized",
    )
    before = collector.risk_snapshot(
        source_sha="4" * 40,
        fault_id=fixture.fault_id,
        stage="before",
        fixture=fixture,
    )
    pause_payload = {
        "pause_token": attempt_id,
        "risk_reason": risk_reason,
        "evidence": {"boundary": "pre-mutation"},
    }
    job.update(
        status="paused",
        owner_token="",
        pause_reason=risk_reason,
        result_payload_json=json.dumps({"risk_pause": pause_payload}),
    )
    attempt.update(
        status="paused",
        error_code=risk_reason,
        result_json=json.dumps(pause_payload),
    )
    during = collector.risk_snapshot(
        source_sha="4" * 40,
        fault_id=fixture.fault_id,
        stage="during",
        fixture=fixture,
    )
    resume_transition = {
        "pause_token": attempt_id,
        "pause_reason": risk_reason,
    }
    job.update(
        status="pending",
        pause_reason="",
        result_payload_json=json.dumps({"risk_resume": resume_transition}),
    )
    action_id = f"action-{risk_reason}-generalized"
    database.actions = [
        {
            "action_id": action_id,
            "job_id": fixture.job_id,
            "action": "resume",
            "pause_token": attempt_id,
            "actor_id": "basic:recovery-operator",
            "auth_method": "basic",
            "reason": runner.OPERATOR_REASON,
            "old_state_json": json.dumps({"status": "paused"}),
            "new_state_json": json.dumps({"status": "pending"}),
        }
    ]
    request_hash = hashlib.sha256(
        f"{kind}:request".encode()
    ).hexdigest()
    transition_hash = hashlib.sha256(
        f"{kind}:transition".encode()
    ).hexdigest()
    pre_discard_state = collector.risk_terminal_state(fixture)
    browser_terminal = {
        "action": "setup_service_discarded",
        "fault_id": fixture.fault_id,
        "hold_id": f"risk-fixture-{fixture.fault_id}",
        "service": "publisher-browser",
        "after": {
            "service": "publisher-browser",
            "exists": True,
                "running": False,
                "container_id": f"browser-{risk_reason}-generalized",
                "image_id": "sha256:" + "d" * 64,
            },
    }
    after = collector.risk_snapshot(
        source_sha="4" * 40,
        fault_id=fixture.fault_id,
        stage="after",
        fixture=fixture,
        replay={
            "pause_token": attempt_id,
            "pause_reason": risk_reason,
            "request_sha256": request_hash,
            "replay_request_sha256": request_hash,
            "first_transition_sha256": transition_hash,
            "replay_transition_sha256": transition_hash,
            "first_disposition": "applied",
            "replay_disposition": "idempotent",
        },
        pre_discard_state=pre_discard_state,
        browser_hold_terminal=browser_terminal,
    )
    snapshots = {"before": before, "during": during, "after": after}

    assert evaluator.snapshot_violations(kind, snapshots) == []
    assert evaluator.assertion_violations(
        kind,
        evaluator.derive_assertions(kind, snapshots),
    ) == []
