from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from forwin.config import InfrastructureConfig
from forwin.http.runtime import HttpRuntime
from forwin.production.executor import ProductionExecutor
from forwin.publish_hub.client import PublishHubClient
from forwin.publishers.manager import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy

URL = "http://host.docker.internal:18930"
KEY = "wiring-test-key-not-a-secret"


def _config(monkeypatch, tmp_path, **env) -> InfrastructureConfig:
    environ = {"FORWIN_ENV_FILE": str(tmp_path / "no-env-file"), **env}
    monkeypatch.setattr(os, "environ", environ)
    return InfrastructureConfig.from_env()


def _no_database():
    raise AssertionError("constructing the publisher runtime must not open a database")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    return _config(
        monkeypatch,
        tmp_path,
        FORWIN_PUBLISH_HUB_URL=URL,
        FORWIN_PUBLISH_HUB_API_KEY=KEY,
        FORWIN_PUBLISH_HUB_TIMEOUT_SECONDS="31",
    )


@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    return _config(monkeypatch, tmp_path)


def _runtime_from(path, config, monkeypatch):
    if path == "container":
        container = RuntimeContainer.for_publisher_worker(
            config, policy=RuntimePolicy.for_profile("standard")
        )
        return container._build_publisher_runtime(
            SimpleNamespace(
                infrastructure=config, session_factory=_no_database, observability=None
            )
        )
    if path == "http":
        monkeypatch.setattr(
            PublisherManager, "recover_interrupted_upload_attempts", lambda self: None
        )
        return HttpRuntime(
            config=config, session_factory=_no_database
        ).get_publisher_manager().runtime
    manager = ProductionExecutor(
        generation_application=None, session_factory=_no_database, config=config
    )._publisher_manager()
    return manager.runtime


@pytest.mark.parametrize("path", ["container", "http", "executor"])
def test_publish_hub_settings_reach_the_publisher_runtime(
    path, configured, monkeypatch
) -> None:
    runtime = _runtime_from(path, configured, monkeypatch)

    assert runtime.publish_hub_url == URL
    assert runtime.publish_hub_api_key == KEY
    assert runtime.publish_hub_timeout_seconds == 31
    client = runtime.publish_hub_client
    assert isinstance(client, PublishHubClient)
    assert runtime.publish_hub_client is client, "the client is built once"
    assert client._http.timeout.read == 31
    assert KEY not in repr(runtime.publish_hub_client)


@pytest.mark.parametrize("path", ["container", "http", "executor"])
def test_without_hub_settings_the_runtime_has_no_client(
    path, unconfigured, monkeypatch
) -> None:
    runtime = _runtime_from(path, unconfigured, monkeypatch)

    assert runtime.publish_hub_client is None
    assert runtime.publish_hub_timeout_seconds == 20


@pytest.mark.parametrize(
    ("url", "key"), [(URL, ""), ("", KEY), ("  ", "  ")]
)
def test_a_half_configured_hub_gives_no_client(url, key) -> None:
    from forwin.publisher_runtime.service import PublisherRuntimeService

    runtime = PublisherRuntimeService(
        session_factory=_no_database,
        extension_api_key="",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
        publish_hub_url=url,
        publish_hub_api_key=key,
    )

    assert runtime.publish_hub_client is None
