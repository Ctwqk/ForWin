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


@pytest.mark.parametrize("kind", (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
))
def test_fixture_is_canon_published_generic_and_fault_unique(
    runner: Any,
    kind: str,
) -> None:
    first = runner.publisher_fixture(kind, "fault-alpha")
    second = runner.publisher_fixture(kind, "fault-beta")

    assert first.task_kind == "chapter_upload"
    assert first.project_id
    assert first.canon_commit_id
    assert first.candidate_id
    assert first.publish is True
    assert first.platform_id == "qidian"
    assert first.book_name.startswith("Publisher Recovery Fixture")
    assert first.chapter_title.startswith("Recovery Chapter")
    assert first.body.startswith("Generic publisher recovery fixture content.")
    assert first.body_sha256 == hashlib.sha256(first.body.encode()).hexdigest()
    assert first.logical_key == second.logical_key == ""
    assert first.job_id == second.job_id == ""
    assert first.fixture_id != second.fixture_id
    assert first.project_id != second.project_id
    assert first.canon_commit_id != second.canon_commit_id
    assert first.remote_book_id != second.remote_book_id
    assert first.result_payload == {}
    assert sensitive_paths(first.evidence_identity()) == []
    assert sensitive_paths(first.result_payload) == []
    assert "project_id" not in first.result_payload
    assert "receipt" not in json.dumps(first.result_payload).lower()


def test_fixture_validation_rejects_canon_drift_credentials_and_receipt(
    runner: Any,
) -> None:
    fixture = runner.publisher_fixture("publisher_captcha", "fault-safety")

    unsafe = (
        {"project_id": ""},
        {"canon_commit_id": ""},
        {"candidate_id": ""},
        {"publish": False},
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
        {"upload_url": "https://example.invalid/not-publisher"},
        {"job_id": "different-job"},
    )
    for changes in unsafe:
        with pytest.raises(runner.SetupBlocked):
            runner.validate_fixture_spec(fixture.with_changes(**changes))


def test_fixture_identities_are_generalized_not_bound_to_live_task_constants(
    runner: Any,
) -> None:
    observed = {
        (
            runner.publisher_fixture(
            kind,
            f"{kind.replace('_', '-')}-{fault_id}",
            ).project_id,
            runner.publisher_fixture(
                kind,
                f"{kind.replace('_', '-')}-{fault_id}",
            ).canon_commit_id,
        )
        for kind in runner.SUPPORTED_FAULTS
        for fault_id in ("fixture-one", "fixture-two")
    }

    assert len(observed) == len(runner.SUPPORTED_FAULTS) * 2
    assert all("80af1c8" not in identity for pair in observed for identity in pair)
    assert all("d97e58c" not in identity for pair in observed for identity in pair)


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


def test_publisher_api_exposes_only_authenticated_operator_resume(
    runner: Any,
) -> None:
    fixture = runner.publisher_fixture(
        "publisher_captcha",
        "fault-api",
    ).with_changes(
        job_id="job-fault-api",
        logical_key="canon-job:fault-api",
        result_payload={"publisher_identity": "fault-api"},
    )
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
        operator_username="recovery-operator",
        operator_password="operator-private",
        transport=transport,
    )
    replay = api.resume_twice(
        fixture,
        pause_token=pause_token,
        risk_reason="captcha",
    )
    assert not hasattr(api, "claim")
    assert not hasattr(api, "pause")
    assert not hasattr(api, "heartbeat")
    assert replay["first_disposition"] == "applied"
    assert replay["replay_disposition"] == "idempotent"
    assert [(call["method"], call["url"]) for call in calls] == [
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
    ]
    expected_basic = "Basic " + base64.b64encode(
        b"recovery-operator:operator-private"
    ).decode("ascii")
    assert calls[0]["headers"] == {"Authorization": expected_basic}
    assert calls[1]["headers"] == {"Authorization": expected_basic}
    assert "operator-private" not in json.dumps(replay)


def test_runner_cannot_claim_or_pause_on_behalf_of_browser(runner: Any) -> None:
    api = runner.PublisherAPI(
        api_url="http://forwin.invalid",
        operator_username="operator",
        operator_password="private",
        transport=lambda *_args, **_kwargs: {},
    )

    assert not hasattr(api, "claim")
    assert not hasattr(api, "pause")
    assert not hasattr(api, "heartbeat")


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
    with pytest.raises(runner.SetupBlocked, match="typed risk reason"):
        runner.validate_detector_evidence(
            {
                "detector": "publisher-risk-v1",
                "boundary": "pre-mutation",
                "selector": "body",
                "matched_text": kind,
                "risk_reason": wrong_reason,
            },
            expected_reason=runner.RISK_REASONS[kind],
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
def test_canon_provisioner_is_the_only_business_setup_and_releases_exact_job(
    runner: Any,
    kind: str,
) -> None:
    fixture = runner.publisher_fixture(kind, f"fault-sql-{kind}")
    calls: list[tuple[str, Any]] = []

    class CanonJobs:
        def materialize(self, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(("materialize", kwargs))
            return [{
                "task_kind": "chapter_upload",
                "job_id": f"job-{fixture.fixture_id}",
                "project_id": fixture.project_id,
                "canon_commit_id": fixture.canon_commit_id,
                "candidate_id": fixture.candidate_id,
                "chapter_number": fixture.chapter_number,
                "idempotency_key": f"canon-job:{fixture.canon_idempotency_key}",
                "body_sha256": fixture.body_sha256,
                "platform": fixture.platform_id,
                "status": "scheduled",
                "publish": True,
                "book_name": fixture.book_name,
                "chapter_title": fixture.chapter_title,
                "body": fixture.body,
                "upload_url": fixture.upload_url,
                "result_payload": {"publisher_identity": fixture.fixture_id},
            }]

        def release(self, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(("release", kwargs))
            return [{
                "job_id": kwargs["job_ids"][0],
                "status": "pending",
                "publish": True,
            }]

    provisioner = runner.CanonPublisherProvisioner(
        runtime=SimpleNamespace(canon_jobs=CanonJobs()),
        source_writer=lambda value: calls.append(("source", value.fixture_id)),
    )
    materialized = provisioner.materialize(fixture)
    provisioner.release(materialized)

    assert [call[0] for call in calls] == ["source", "materialize", "release"]
    request = calls[1][1]
    assert request["canon_commit_id"] == fixture.canon_commit_id
    assert request["candidate_id"] == fixture.candidate_id
    assert request["project_id"] == fixture.project_id
    assert request["publish"] is True
    assert request["bindings"] == [{
        "platform": fixture.platform_id,
        "book_name": fixture.book_name,
        "upload_url": fixture.upload_url,
        "create_if_missing": False,
        "publisher_compliance_required": False,
    }]
    assert materialized.task_kind == "chapter_upload"
    assert materialized.job_id
    assert materialized.logical_key
    assert calls[2][1] == {
        "project_id": fixture.project_id,
        "job_ids": [materialized.job_id],
        "publish": True,
        "actor_type": "recovery_evidence",
    }


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

    def stop(self, service: str, fault_id: str) -> dict[str, str]:
        self.log.append(("stop", service, fault_id))
        return {
            "action": "fault_service_stopped",
            "service": service,
            "fault_id": fault_id,
        }

    def start(self, service: str, fault_id: str) -> dict[str, str]:
        self.log.append(("start", service, fault_id))
        return {
            "action": "fault_service_recovered",
            "service": service,
            "fault_id": fault_id,
        }

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

    def wait_status(self, fixture: Any, expected: str) -> None:
        self.log.append(("wait_status", expected))


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
    del barrier
    writer = FakeWriter(tmp_path, log)
    fault_id = f"fault-live-{kind}"
    endpoint_record = endpoint_identity(
        None,
        fault_id=fault_id,
    )
    state = GeneralizedFlowState()
    adapters = GeneralizedFlowAdapters(state)
    adapters.calls = log
    adapters.api_url = "http://127.0.0.1:23117"
    sql_collector = FakeSQLCollector(log, endpoint_record)
    sql_collector.wait_retryable_attempt = adapters.wait_retryable_attempt
    sql_collector.wait_status = adapters.wait_status
    sql_collector.wait_risk_pause = adapters.wait_risk_pause
    sql_collector.recovery_snapshot = adapters.recovery_snapshot
    return (
        runner_module.LiveRunner(
            fault_kind=kind,
            fault_id=fault_id,
            source_sha="1" * 40,
            database_url="postgresql://u:p@127.0.0.1:23119/forwin",
            mcp_url="http://127.0.0.1:23118/mcp",
            api_url="http://127.0.0.1:23117",
            controller=FakeController(tmp_path, log, endpoint_record),
            sql_collector=sql_collector,
            api=adapters,
            writer=writer,
            provisioner=adapters,
            browser=adapters,
        ),
        writer,
    )


def test_backend_live_sequence_materializes_then_replays_terminal_journal(
    runner_module: Any,
    tmp_path: Path,
) -> None:
    log: list[Any] = []
    live, writer = make_live_runner(
        runner_module,
        tmp_path,
        "publisher_backend_unavailable",
        log,
    )

    result = live.run()

    assert result.status == "pass"
    assert writer.pass_payload is not None
    assert list(writer.pass_payload["snapshots"]) == [
        "before",
        "during",
        "after",
    ]
    assert log.index("canon.materialize") < log.index("canon.release")
    assert log.index("canon.release") < log.index(
        "browser.fault:backend_unavailable"
    )
    assert log.count("browser.dispatch") == 2
    assert not any(
        isinstance(item, tuple)
        and item[:2] in {("kill", "publisher-worker"), ("stop", "publisher-worker")}
        for item in log
    )
    assert log[-2:] == ["destroy", "write_pass"]


def test_missing_adapter_and_cleanup_failure_are_setup_blocked(
    runner_module: Any,
    tmp_path: Path,
) -> None:
    for suffix, fragment in (
        ("missing", "browser driver is missing"),
        ("cleanup", "publisher browser cleanup"),
    ):
        log: list[Any] = []
        live, writer = make_live_runner(
            runner_module,
            tmp_path / suffix,
            "publisher_backend_unavailable",
            log,
        )
        if suffix == "missing":
            live.browser = None
        else:
            live.browser.close = lambda: (_ for _ in ()).throw(
                RuntimeError("cleanup failed")
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


def test_browser_live_sequence_stops_only_after_durable_journal(
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
    assert log.index("browser.fault:browser_shutdown_barrier") < log.index(
        "browser.disconnect"
    )
    assert log.index("browser.disconnect") < log.index((
        "stop",
        "publisher-browser",
        "fault-live-publisher_browser_unavailable",
    ))
    assert log.index((
        "start",
        "publisher-browser",
        "fault-live-publisher_browser_unavailable",
    )) < log.index("browser.reconnect")
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
    detector = log.index("browser.dispatch")
    fault_mark = log.index(("mark", kind, "fault", fault_id))
    clear_risk = log.index("browser.clear_risk")
    resume = log.index("operator.resume")
    recovery_mark = log.index(("mark", kind, "recovery", fault_id))
    assert detector < fault_mark < clear_risk < resume < recovery_mark
    assert log.count("browser.dispatch") == 2
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
            live.provisioner,
            "materialize",
            lambda _fixture: (_ for _ in ()).throw(interruption()),
        )
    elif point == "during_fault":
        monkeypatch.setattr(
            live.browser,
            "trigger_dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption()),
        )
    else:
        monkeypatch.setattr(
            live.browser,
            "close",
            lambda: (_ for _ in ()).throw(interruption()),
            raising=False,
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
        self.actions: list[dict[str, Any]] = []
        self.canon_sources: list[dict[str, Any]] = []
        self.sentinel = endpoint_identity(
            None,
            fault_id="fault-memory-placeholder",
            source_sha="3" * 40,
        )["sentinel"]

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
        if "FROM projects AS project" in normalized:
            return [
                dict(row)
                for row in self.canon_sources
                if (
                    row["project_id"],
                    row["chapter_plan_id"],
                    row["draft_id"],
                    row["candidate_id"],
                    row["canon_commit_id"],
                ) == tuple(parameters)
            ]
        if "FROM publisher_operator_actions" in normalized:
            return [
                dict(row)
                for row in self.actions
                if row["job_id"] == parameters[0]
            ]
        raise AssertionError(f"unexpected SQL: {normalized}")


def seed_canon_job(
    database: MemoryPublisherDatabase,
    fixture: Any,
) -> Any:
    fixture = fixture.with_changes(
        job_id=f"job-{fixture.fixture_id}",
        logical_key=f"canon-publisher:{fixture.canon_idempotency_key}",
        result_payload={
            "canon_commit_id": fixture.canon_commit_id,
            "candidate_id": fixture.candidate_id,
            "chapter_number": fixture.chapter_number,
            "upload_url": fixture.upload_url,
        },
    )
    database.canon_sources.append(
        {
            "project_id": fixture.project_id,
            "chapter_plan_id": fixture.chapter_plan_id,
            "draft_id": fixture.draft_id,
            "candidate_id": fixture.candidate_id,
            "canon_commit_id": fixture.canon_commit_id,
            "canon_idempotency_key": fixture.canon_idempotency_key,
            "canon_status": "committed",
            "candidate_status": "accepted",
            "candidate_canon_status": "committed",
            "chapter_status": "accepted",
            "chapter_number": fixture.chapter_number,
            "body_text": fixture.body,
            "body_hash": fixture.body_sha256,
        }
    )
    database.jobs.append(
        {
            "job_id": fixture.job_id,
            "logical_key": fixture.logical_key,
            "task_kind": "chapter_upload",
            "project_id": fixture.project_id,
            "canon_commit_id": fixture.canon_commit_id,
            "candidate_id": fixture.candidate_id,
            "chapter_number": fixture.chapter_number,
            "platform_id": fixture.platform_id,
            "status": "pending",
            "publish": True,
            "book_name": fixture.book_name,
            "chapter_title": fixture.chapter_title,
            "body_text": fixture.body,
            "body_sha256": fixture.body_sha256,
            "upload_url": fixture.upload_url,
            "abort_requested": False,
            "owner_token": "",
            "extension_client_id": "",
            "current_attempt_id": "",
            "available_at": "2026-07-22T13:30:00+00:00",
            "reconcile_after": "",
            "claimed_at": "",
            "started_at": "",
            "finished_at": "",
            "deleted_at": "",
            "paused_at": "",
            "pause_reason": "",
            "current_url": fixture.upload_url,
            "result_message": "",
            "error_message": "",
            "result_payload_json": json.dumps(
                fixture.result_payload,
                sort_keys=True,
            ),
            "created_at": "2026-07-22T12:00:00+00:00",
            "updated_at": "2026-07-22T12:00:00+00:00",
            "database_now": "2026-07-22T12:30:00+00:00",
        }
    )
    return fixture


def collect_recovery_snapshot(
    collector: Any,
    fixture: Any,
    *,
    stage: str,
    source_sha: str,
    external: dict[str, Any],
) -> dict[str, Any]:
    snapshot = collector.recovery_snapshot(
        stage=stage,
        fixture=fixture,
        external=external,
    )
    snapshot["source_sha"] = source_sha
    return snapshot


def receipt_row(fixture: Any, attempt_id: str) -> dict[str, str]:
    return {
        "receipt_id": f"receipt-{fixture.fixture_id}",
        "job_id": fixture.job_id,
        "attempt_id": attempt_id,
        "natural_key": f"qidian:{fixture.remote_book_id}:{fixture.remote_chapter_id}",
        "idempotency_key": fixture.logical_key,
        "platform_id": fixture.platform_id,
        "remote_book_id": fixture.remote_book_id,
        "remote_chapter_id": fixture.remote_chapter_id,
        "remote_url": fixture.upload_url,
        "official_state": "published",
        "content_sha256": fixture.body_sha256,
        "source": "extension",
    }


def upload_effect(fixture: Any, count: int) -> dict[str, Any]:
    key = f"forwin-recovery-effect:{fixture.fixture_id}"
    return {
        "fixture_id": fixture.fixture_id,
        "effect_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "upload_effect_count": count,
    }


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
    fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            "publisher_browser_unavailable",
            "fault-memory-duplicate",
        ),
    )

    duplicate = dict(database.jobs[0])
    duplicate["job_id"] = f"duplicate-{fixture.job_id}"
    database.jobs.append(duplicate)

    with pytest.raises(runner.SetupBlocked, match="duplicate or missing"):
        collector._exact_row(fixture)


def test_backend_sql_snapshots_satisfy_strict_evaluator_from_raw_rows(
    runner: Any,
    evaluator: Any,
) -> None:
    database = MemoryPublisherDatabase()
    collector = runner.SQLCollector(database)
    fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            "publisher_backend_unavailable",
            "fault-memory-backend",
        ),
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="2" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    job = database.jobs[0]
    before = collect_recovery_snapshot(
        collector,
        fixture,
        stage="before",
        source_sha="2" * 40,
        external={
            "browser": {
                "browser_id": "browser-memory-generalized",
                "status": "healthy",
                "probe": "extension_service_worker_cdp",
            },
            **upload_effect(fixture, 0),
        },
    )
    attempt_id = f"attempt-{fixture.fixture_id}"
    database.attempts = [
        {
            "attempt_id": attempt_id,
            "job_id": fixture.job_id,
            "attempt_number": 1,
            "attempt_kind": "execute",
            "owner_token": "browser-memory-generalized",
            "lease_epoch": 1,
            "status": "running",
            "phase": "mutation_started",
            "content_sha256": fixture.body_sha256,
            "error_code": "",
            "result_json": "{}",
        }
    ]
    job.update(
        status="running",
        owner_token="browser-memory-generalized",
        extension_client_id="browser-memory-generalized",
        current_attempt_id=attempt_id,
        started_at="2026-07-22T12:15:00+00:00",
    )
    journal = {
        "job_id": fixture.job_id,
        "attempt_id": attempt_id,
        "journal_phase": "ack_pending",
        "receipt_key": f"qidian:{fixture.remote_book_id}:{fixture.remote_chapter_id}",
        "content_sha256": fixture.body_sha256,
        "fault_observed_at": "2026-07-22T12:16:00+00:00",
        "fault_request_url": (
            "http://forwin.invalid/api/publishers/extension/upload-jobs/"
            f"{fixture.job_id}/attempts/{attempt_id}/receipt"
        ),
    }
    during = collect_recovery_snapshot(
        collector,
        fixture,
        stage="during",
        source_sha="2" * 40,
        external={
            "terminal_fault": {
                "job_id": fixture.job_id,
                "mode": "backend_unavailable",
                "installed_at": "2026-07-22T12:14:00+00:00",
                "observed_at": journal["fault_observed_at"],
                "request_url": journal["fault_request_url"],
            },
            "journal": journal,
            **upload_effect(fixture, 1),
        },
    )
    database.attempts[0].update(
        status="succeeded",
        phase="result_submitted",
    )
    job.update(
        status="succeeded",
        finished_at="2026-07-22T12:17:00+00:00",
        result_message="published",
    )
    database.receipts = [receipt_row(fixture, attempt_id)]
    after = collect_recovery_snapshot(
        collector,
        fixture,
        stage="after",
        source_sha="2" * 40,
        external={
            "journal_replay": {"attempt_id": attempt_id},
            **upload_effect(fixture, 1),
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
    fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            "publisher_browser_unavailable",
            "fault-memory-browser",
        ),
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="3" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    before = collect_recovery_snapshot(
        collector,
        fixture,
        stage="before",
        source_sha="3" * 40,
        external={
            "browser": {
                "browser_id": "browser-memory-generalized",
                "probe": "extension_service_worker_cdp",
                "status": "healthy",
            },
            **upload_effect(fixture, 0),
        },
    )
    attempt_id = f"attempt-{fixture.fixture_id}"
    database.attempts = [
        {
            "attempt_id": attempt_id,
            "job_id": fixture.job_id,
            "attempt_number": 1,
            "attempt_kind": "execute",
            "owner_token": "browser-memory-generalized",
            "lease_epoch": 1,
            "status": "running",
            "phase": "mutation_started",
            "content_sha256": fixture.body_sha256,
            "error_code": "",
            "result_json": "{}",
        }
    ]
    database.jobs[0].update(
        status="running",
        owner_token="browser-memory-generalized",
        extension_client_id="browser-memory-generalized",
        current_attempt_id=attempt_id,
        started_at="2026-07-22T12:15:00+00:00",
    )
    fault_url = (
        "http://forwin.invalid/api/publishers/extension/upload-jobs/"
        f"{fixture.job_id}/attempts/{attempt_id}/receipt"
    )
    during = collect_recovery_snapshot(
        collector,
        fixture,
        stage="during",
        source_sha="3" * 40,
        external={
            "terminal_fault": {
                "job_id": fixture.job_id,
                "mode": "browser_shutdown_barrier",
                "installed_at": "2026-07-22T12:14:00+00:00",
                "observed_at": "2026-07-22T12:16:00+00:00",
                "request_url": fault_url,
            },
            "journal": {
                "job_id": fixture.job_id,
                "attempt_id": attempt_id,
                "journal_phase": "ack_pending",
                "receipt_key": (
                    f"qidian:{fixture.remote_book_id}:{fixture.remote_chapter_id}"
                ),
                "content_sha256": fixture.body_sha256,
                "fault_observed_at": "2026-07-22T12:16:00+00:00",
                "fault_request_url": fault_url,
            },
            "browser_fault": {
                "action": "fault_service_stopped",
                "service": "publisher-browser",
                "fault_id": fixture.fault_id,
            },
            **upload_effect(fixture, 1),
        },
    )
    database.attempts[0].update(
        status="succeeded",
        phase="result_submitted",
    )
    database.jobs[0].update(
        status="succeeded",
        finished_at="2026-07-22T12:17:00+00:00",
        result_message="published",
    )
    database.receipts = [receipt_row(fixture, attempt_id)]
    after = collect_recovery_snapshot(
        collector,
        fixture,
        stage="after",
        source_sha="3" * 40,
        external={
            "browser_recovery": {
                "action": "fault_service_recovered",
                "service": "publisher-browser",
                "fault_id": fixture.fault_id,
            },
            "journal_replay": {"attempt_id": attempt_id},
            **upload_effect(fixture, 1),
        },
    )
    snapshots = {"before": before, "during": during, "after": after}

    assert evaluator.snapshot_violations(fixture.fault_kind, snapshots) == []
    assert evaluator.assertion_violations(
        fixture.fault_kind,
        evaluator.derive_assertions(fixture.fault_kind, snapshots),
    ) == []


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


def test_risk_and_browser_use_one_full_canonical_job_projection(
    runner: Any,
) -> None:
    database = MemoryPublisherDatabase()
    browser_fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            "publisher_browser_unavailable",
            "canonical-browser-generalized",
        ),
    )
    risk_fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            "publisher_captcha",
            "canonical-risk-generalized",
        ),
    )
    collector = runner.SQLCollector(database)

    browser_job = collector._canonical_job(browser_fixture)
    risk_job = collector._canonical_job(risk_fixture)

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
    fixture = seed_canon_job(
        database,
        runner.publisher_fixture(
            kind,
            f"fault-memory-{risk_reason}",
        ),
    )
    bound_endpoint = endpoint_identity(
        None,
        fault_id=fixture.fault_id,
        source_sha="4" * 40,
    )
    database.sentinel = dict(bound_endpoint["sentinel"])
    collector.bind_endpoint_identity(bound_endpoint)
    job = database.jobs[0]
    before = collect_recovery_snapshot(
        collector,
        fixture,
        stage="before",
        source_sha="4" * 40,
        external={
            "browser": {
                "browser_id": "browser-memory-generalized",
                "probe": "extension_service_worker_cdp",
                "status": "healthy",
            },
            **upload_effect(fixture, 0),
        },
    )
    attempt_id = f"attempt-{risk_reason}-generalized"
    evidence = {
        "detector": "publisher-risk-v1",
        "boundary": "pre-mutation",
        "selector": f"#publisher-recovery-{risk_reason}",
        "matched_text": f"{risk_reason} fixture",
        "message": f"publisher {risk_reason} detected",
    }
    pause_payload = {
        "pause_token": attempt_id,
        "risk_reason": risk_reason,
        "client_observed_at": "2026-07-22T12:15:00+00:00",
        "evidence": evidence,
    }
    attempt = {
        "attempt_id": attempt_id,
        "job_id": fixture.job_id,
        "attempt_number": 1,
        "attempt_kind": "execute",
        "owner_token": "browser-memory-generalized",
        "lease_epoch": 1,
        "status": "paused",
        "phase": "claimed",
        "content_sha256": fixture.body_sha256,
        "error_code": risk_reason,
        "result_json": json.dumps(pause_payload),
    }
    database.attempts = [attempt]
    job.update(
        status="paused",
        owner_token="",
        current_attempt_id=attempt_id,
        paused_at="2026-07-22T12:15:00+00:00",
        pause_reason=risk_reason,
        result_payload_json=json.dumps({"risk_pause": pause_payload}),
    )
    during = collect_recovery_snapshot(
        collector,
        fixture,
        stage="during",
        source_sha="4" * 40,
        external={
            "detector_evidence": {
                **evidence,
                "risk_reason": risk_reason,
                "observed_at": pause_payload["client_observed_at"],
                "attempt_id": attempt_id,
            },
            **upload_effect(fixture, 0),
        },
    )
    resumed_attempt_id = f"attempt-resumed-{risk_reason}-generalized"
    database.attempts.append(
        {
            "attempt_id": resumed_attempt_id,
            "job_id": fixture.job_id,
            "attempt_number": 2,
            "attempt_kind": "execute",
            "owner_token": "browser-memory-generalized",
            "lease_epoch": 2,
            "status": "succeeded",
            "phase": "result_submitted",
            "content_sha256": fixture.body_sha256,
            "error_code": "",
            "result_json": "{}",
        }
    )
    job.update(
        status="succeeded",
        current_attempt_id=resumed_attempt_id,
        paused_at="",
        pause_reason="",
        finished_at="2026-07-22T12:17:00+00:00",
        result_message="published",
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
    database.receipts = [receipt_row(fixture, resumed_attempt_id)]
    after = collect_recovery_snapshot(
        collector,
        fixture,
        stage="after",
        source_sha="4" * 40,
        external={
            "operator_resume": {
                "pause_token": attempt_id,
                "pause_reason": risk_reason,
                "first_disposition": "applied",
                "replay_disposition": "idempotent",
            },
            **upload_effect(fixture, 1),
        },
    )
    snapshots = {"before": before, "during": during, "after": after}

    assert evaluator.snapshot_violations(kind, snapshots) == []
    assert evaluator.assertion_violations(
        kind,
        evaluator.derive_assertions(kind, snapshots),
    ) == []


@pytest.mark.parametrize("fault_kind", (
    "publisher_backend_unavailable",
    "publisher_browser_unavailable",
    "publisher_captcha",
    "publisher_mfa",
    "publisher_account_risk",
))
def test_generalized_fault_uses_canon_published_chapter_upload(
    runner: Any,
    fault_kind: str,
) -> None:
    fixture = runner.publisher_fixture(fault_kind, f"generalized-{fault_kind}")

    assert fixture.task_kind == "chapter_upload"
    assert fixture.publish is True
    assert all((
        fixture.project_id,
        fixture.chapter_plan_id,
        fixture.candidate_id,
        fixture.canon_commit_id,
        fixture.canon_idempotency_key,
    ))
    assert fixture.chapter_number > 0
    assert fixture.body_sha256 == runner.candidate_body_hash(fixture.body)
    assert fixture.upload_url.startswith("https://write.qq.com/")
    assert fixture.remote_book_id in fixture.upload_url
    assert fixture.remote_chapter_id in fixture.upload_url
    assert fixture.job_id == ""
    assert fixture.logical_key == ""
    assert fixture.result_payload == {}


def test_generalized_faults_do_not_share_canon_or_remote_identity(
    runner: Any,
) -> None:
    fixtures = [
        runner.publisher_fixture(kind, f"isolated-{index}-{kind}")
        for index, kind in enumerate(runner.SUPPORTED_FAULTS, start=1)
    ]
    for attribute in (
        "fixture_id",
        "project_id",
        "chapter_plan_id",
        "candidate_id",
        "canon_commit_id",
        "canon_idempotency_key",
        "remote_book_id",
        "remote_chapter_id",
    ):
        assert len({getattr(item, attribute) for item in fixtures}) == len(fixtures)


@pytest.mark.parametrize(
    ("risk_reason", "expected_fragment"),
    (
        ("captcha", 'id="publisher-recovery-captcha"'),
        ("mfa", 'autocomplete="one-time-code"'),
        ("account_risk", "data-risk-control"),
    ),
)
def test_controlled_page_exposes_production_detector_selectors(
    runner: Any,
    risk_reason: str,
    expected_fragment: str,
) -> None:
    fixture = runner.publisher_fixture(
        f"publisher_{risk_reason}",
        f"detector-{risk_reason}",
    )
    page = runner.publisher_page_html(fixture, risk_reason=risk_reason)

    assert expected_fragment in page
    assert fixture.fixture_id in page
    assert fixture.body not in page
    assert "publisher-recovery-v1" not in page
    assert "fetch(" not in page


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"detector": "publisher-recovery-v1"}, "production detector"),
        ({"boundary": "post-action"}, "pre-mutation"),
        ({"risk_reason": "mfa"}, "typed risk reason"),
        ({"selector": ""}, "selector"),
        ({"matched_text": ""}, "matched text"),
    ),
)
def test_detector_evidence_requires_production_browser_path(
    runner: Any,
    mutation: dict[str, str],
    message: str,
) -> None:
    evidence = {
        "detector": "publisher-risk-v1",
        "boundary": "pre-mutation",
        "selector": "#publisher-recovery-captcha",
        "matched_text": "Human verification required",
        "risk_reason": "captcha",
        **mutation,
    }
    with pytest.raises(runner.SetupBlocked, match=message):
        runner.validate_detector_evidence(evidence, expected_reason="captcha")


class GeneralizedFlowState:
    def __init__(self) -> None:
        self.fixture: Any = None
        self.status = "missing"
        self.attempts: list[dict[str, Any]] = []
        self.receipts: list[dict[str, str]] = []
        self.actions: list[dict[str, str]] = []
        self.detector: dict[str, str] = {}

    def attempt(self) -> str:
        if not self.attempts:
            self.attempts.append({
                "attempt_id": f"attempt-{self.fixture.fixture_id}",
                "attempt_number": 1,
                "status": "running",
                "phase": "mutation_started",
            })
        self.status = "running"
        return str(self.attempts[-1]["attempt_id"])

    def succeed(self, resumed: bool = False) -> None:
        if resumed:
            self.attempts.append({
                "attempt_id": f"attempt-resumed-{self.fixture.fixture_id}",
                "attempt_number": 2,
                "status": "succeeded",
                "phase": "result_submitted",
            })
        else:
            self.attempt()
            self.attempts[0].update(status="succeeded", phase="result_submitted")
        self.status = "succeeded"
        if not self.receipts:
            self.receipts.append({
                "receipt_id": f"receipt-{self.fixture.fixture_id}",
                "attempt_id": str(self.attempts[-1]["attempt_id"]),
            })

    def view(self) -> dict[str, Any]:
        return {
            "job_id": self.fixture.job_id,
            "status": self.status,
            "attempts": [dict(row) for row in self.attempts],
            "receipts": [dict(row) for row in self.receipts],
            "resume_actions": [dict(row) for row in self.actions],
            "detector_evidence": dict(self.detector),
        }


class GeneralizedFlowAdapters:
    def __init__(self, state: GeneralizedFlowState) -> None:
        self.state = state
        self.calls: list[str] = []
        self.effect = 0
        self.mode = ""
        self.risk = ""
        self.resumed = False

    def materialize(self, fixture: Any) -> Any:
        self.calls.append("canon.materialize")
        fixture = fixture.with_changes(
            job_id=f"job-{fixture.fixture_id}",
            logical_key=f"canon-publisher:{fixture.canon_idempotency_key}",
            result_payload={"publisher_identity": fixture.fixture_id},
        )
        self.state.fixture = fixture
        self.state.status = "scheduled"
        return fixture

    def release(self, fixture: Any) -> Any:
        self.calls.append("canon.release")
        self.state.status = "pending"
        return fixture

    def prepare(self, fixture: Any, *, risk_reason: str = "") -> dict[str, str]:
        self.calls.append(f"browser.prepare:{risk_reason or 'safe'}")
        self.risk = risk_reason
        return {"browser_id": fixture.fixture_id, "status": "healthy"}

    def external_effect(self, fixture: Any) -> dict[str, Any]:
        return {"fixture_id": fixture.fixture_id, "upload_effect_count": self.effect}

    def install_terminal_fault(self, fixture: Any, *, mode: str) -> dict[str, str]:
        self.calls.append(f"browser.fault:{mode}")
        self.mode = mode
        return {"mode": mode, "job_id": fixture.job_id}

    def trigger_dispatch(self, _fixture: Any) -> None:
        self.calls.append("browser.dispatch")
        if self.risk and not self.resumed:
            attempt_id = self.state.attempt()
            self.state.status = "paused"
            self.state.attempts[0].update(status="paused", phase="claimed")
            self.state.detector = {
                "detector": "publisher-risk-v1",
                "boundary": "pre-mutation",
                "selector": f"#publisher-recovery-{self.risk}",
                "matched_text": f"{self.risk} fixture",
                "risk_reason": self.risk,
                "pause_token": attempt_id,
            }
        elif self.mode:
            self.state.attempt()
            self.effect = 1
        else:
            self.effect = 1
            self.state.succeed(self.resumed)

    def wait_terminal_journal(self, fixture: Any) -> dict[str, str]:
        return {
            "job_id": fixture.job_id,
            "attempt_id": str(self.state.attempts[0]["attempt_id"]),
            "journal_phase": "ack_pending",
        }

    def wait_retryable_attempt(self, _fixture: Any, attempt_id: str) -> dict[str, Any]:
        assert attempt_id == self.state.attempts[0]["attempt_id"]
        assert self.state.status == "running"
        return self.state.view()

    def wait_status(self, _fixture: Any, expected: str, **_kwargs: Any) -> None:
        assert self.state.status == expected

    def wait_risk_pause(self, _fixture: Any, expected_reason: str) -> dict[str, Any]:
        assert self.state.status == "paused"
        assert self.state.detector["risk_reason"] == expected_reason
        return {
            "pause_token": self.state.detector["pause_token"],
            "detector_evidence": dict(self.state.detector),
        }

    def recovery_snapshot(
        self,
        *,
        stage: str,
        fixture: Any,
        external: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "fixture": fixture.evidence_identity(),
            "database": self.state.view(),
            "external": dict(external),
        }

    def restore_terminal_backend(self, _fixture: Any) -> None:
        self.calls.append("browser.restore_backend")
        self.mode = ""

    def disconnect(self) -> None:
        self.calls.append("browser.disconnect")

    def reconnect(self, _fixture: Any) -> None:
        self.calls.append("browser.reconnect")
        self.mode = ""

    def clear_risk(self, _fixture: Any) -> None:
        self.calls.append("browser.clear_risk")
        self.risk = ""
        self.resumed = True

    def resume_twice(
        self,
        _fixture: Any,
        *,
        pause_token: str,
        risk_reason: str,
    ) -> dict[str, str]:
        self.calls.append("operator.resume")
        self.state.status = "pending"
        self.state.actions[:] = [{
            "action": "resume",
            "pause_token": pause_token,
            "pause_reason": risk_reason,
        }]
        return {
            "pause_token": pause_token,
            "pause_reason": risk_reason,
            "first_disposition": "applied",
            "replay_disposition": "idempotent",
        }

    def stop(self, service: str, fault_id: str) -> dict[str, str]:
        self.calls.append(f"controller.stop:{service}")
        return {
            "action": "fault_service_stopped",
            "service": service,
            "fault_id": fault_id,
        }

    def start(self, service: str, fault_id: str) -> dict[str, str]:
        self.calls.append(f"controller.start:{service}")
        return {
            "action": "fault_service_recovered",
            "service": service,
            "fault_id": fault_id,
        }

    def mark(self, fault_kind: str, phase: str, _fault_id: str) -> None:
        self.calls.append(f"controller.mark:{fault_kind}:{phase}")


def make_generalized_flow(runner: Any, fault_kind: str) -> tuple[Any, Any, Any]:
    state = GeneralizedFlowState()
    adapters = GeneralizedFlowAdapters(state)
    flow = runner.PublisherRecoveryFlow(
        fixture=runner.publisher_fixture(fault_kind, f"flow-{fault_kind}"),
        source_sha="a" * 40,
        provisioner=adapters,
        browser=adapters,
        sql_collector=adapters,
        api=adapters,
        controller=adapters,
    )
    return flow, state, adapters


def test_backend_outage_replays_same_canon_attempt_once(runner: Any) -> None:
    flow, state, adapters = make_generalized_flow(
        runner,
        "publisher_backend_unavailable",
    )
    snapshots = flow.run()

    assert snapshots["during"]["database"]["status"] == "running"
    assert snapshots["during"]["external"]["terminal_fault"]["mode"] == "backend_unavailable"
    assert snapshots["after"]["database"]["status"] == "succeeded"
    assert len(snapshots["after"]["database"]["attempts"]) == 1
    assert len(snapshots["after"]["database"]["receipts"]) == 1
    assert snapshots["after"]["external"]["upload_effect_count"] == 1
    assert state.fixture.task_kind == "chapter_upload"
    assert not any("pause" in call for call in adapters.calls)
    assert adapters.calls[-1].endswith(":recovery")


def test_browser_outage_replays_journal_without_second_upload(runner: Any) -> None:
    flow, _state, adapters = make_generalized_flow(
        runner,
        "publisher_browser_unavailable",
    )
    snapshots = flow.run()

    assert snapshots["during"]["external"]["journal"]["journal_phase"] == "ack_pending"
    assert len(snapshots["after"]["database"]["attempts"]) == 1
    assert len(snapshots["after"]["database"]["receipts"]) == 1
    assert snapshots["after"]["external"]["upload_effect_count"] == 1
    assert adapters.calls.index("browser.disconnect") < adapters.calls.index("controller.stop:publisher-browser")
    assert adapters.calls.index("controller.start:publisher-browser") < adapters.calls.index("browser.reconnect")


@pytest.mark.parametrize(
    ("fault_kind", "risk_reason"),
    (
        ("publisher_captcha", "captcha"),
        ("publisher_mfa", "mfa"),
        ("publisher_account_risk", "account_risk"),
    ),
)
def test_browser_detector_pause_and_operator_resume_upload_once(
    runner: Any,
    fault_kind: str,
    risk_reason: str,
) -> None:
    flow, _state, adapters = make_generalized_flow(runner, fault_kind)
    snapshots = flow.run()

    during = snapshots["during"]
    after = snapshots["after"]
    evidence = during["database"]["detector_evidence"]
    assert during["database"]["status"] == "paused"
    assert evidence["detector"] == "publisher-risk-v1"
    assert evidence["boundary"] == "pre-mutation"
    assert evidence["risk_reason"] == risk_reason
    assert during["external"]["upload_effect_count"] == 0
    assert after["database"]["status"] == "succeeded"
    assert len(after["database"]["attempts"]) == 2
    assert len(after["database"]["resume_actions"]) == 1
    assert len(after["database"]["receipts"]) == 1
    assert after["external"]["upload_effect_count"] == 1
    assert adapters.calls.count("operator.resume") == 1
