from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.runtime import workers


def test_worker_bootstraps_resolve_only_their_owned_runtime(monkeypatch) -> None:
    calls: list[str] = []
    application = object()
    publisher_runtime = SimpleNamespace(backend_jobs=object())
    handlers = {"event": lambda _claim: None}
    session_factory = object()

    class Container:
        def __init__(self, role: str) -> None:
            self.role = role

        def core_services(self):
            calls.append(f"{self.role}.core")
            return SimpleNamespace(
                generation_application=application,
                session_factory=session_factory,
            )

        def generation_services(self):
            raise AssertionError("worker bootstrap eagerly resolved generation graph")

        def publisher_services(self):
            calls.append(f"{self.role}.publisher")
            return SimpleNamespace(publisher_runtime=publisher_runtime)

        def build_outbox_handlers(self):
            calls.append(f"{self.role}.outbox")
            return handlers

        def close(self) -> None:
            calls.append(f"{self.role}.close")

    class Containers:
        @staticmethod
        def for_generation_worker(_config, *, policy):
            return Container("generation")

        @staticmethod
        def for_publisher_worker(_config, *, policy):
            return Container("publisher")

        @staticmethod
        def for_outbox_worker(_config, *, policy):
            return Container("outbox")

    monkeypatch.setattr(workers, "RuntimeContainer", Containers)

    generation = workers.build_generation_worker_runtime(object())  # type: ignore[arg-type]
    publisher = workers.build_publisher_worker_runtime(object())  # type: ignore[arg-type]
    outbox = workers.build_outbox_worker_runtime(object())  # type: ignore[attr-defined,arg-type]

    assert generation.application_service is application
    assert publisher.publisher_runtime is publisher_runtime
    assert outbox.session_factory is session_factory
    assert outbox.handlers is handlers
    assert calls == [
        "generation.core",
        "publisher.publisher",
        "outbox.core",
        "outbox.outbox",
    ]

    generation.close()
    publisher.close()
    outbox.close()
    assert calls[-3:] == ["generation.close", "publisher.close", "outbox.close"]


def test_api_automation_does_not_execute_publisher_backend_jobs(
    monkeypatch,
) -> None:
    from forwin.http import automation

    sentinel = object()
    production_scheduler = object()
    backend_job_calls: list[int] = []
    backend_jobs = SimpleNamespace(
        run_pending_once=lambda *, limit: backend_job_calls.append(limit)
    )
    runtime = SimpleNamespace(
        container=SimpleNamespace(
            core_services=lambda: SimpleNamespace(
                generation_application=object()
            ),
            publisher_services=lambda: SimpleNamespace(
                production_scheduler=production_scheduler,
                publisher_runtime=SimpleNamespace(
                    backend_jobs=backend_jobs
                ),
            ),
        ),
        session_factory=object(),
        config=object(),
        display_timezone=object(),
        project_application=None,
    )

    def fake_scheduler_pass(**kwargs):
        assert kwargs["production_scheduler_factory"] is production_scheduler
        return sentinel

    monkeypatch.setattr(
        automation.api_automation,
        "run_automation_scheduler_pass",
        fake_scheduler_pass,
    )

    assert automation._run_automation_scheduler_pass(runtime) is sentinel
    assert backend_job_calls == []


def test_worker_bootstrap_failure_closes_partially_built_container(monkeypatch) -> None:
    calls: list[str] = []

    class FailingContainer:
        def __init__(self, role: str) -> None:
            self.role = role

        def core_services(self):
            calls.append(f"{self.role}.core")
            if self.role == "generation":
                raise RuntimeError("generation core failed")
            return SimpleNamespace(session_factory=object())

        def publisher_services(self):
            calls.append("publisher.publisher")
            raise RuntimeError("publisher bundle failed")

        def build_outbox_handlers(self):
            calls.append("outbox.outbox")
            raise RuntimeError("outbox handlers failed")

        def close(self) -> None:
            calls.append(f"{self.role}.close")

    class Containers:
        @staticmethod
        def for_generation_worker(_config, *, policy):
            return FailingContainer("generation")

        @staticmethod
        def for_publisher_worker(_config, *, policy):
            return FailingContainer("publisher")

        @staticmethod
        def for_outbox_worker(_config, *, policy):
            return FailingContainer("outbox")

    monkeypatch.setattr(workers, "RuntimeContainer", Containers)

    for factory in (
        workers.build_generation_worker_runtime,
        workers.build_publisher_worker_runtime,
        workers.build_outbox_worker_runtime,
    ):
        with pytest.raises(RuntimeError, match="failed"):
            factory(object())  # type: ignore[arg-type]

    assert calls == [
        "generation.core",
        "generation.close",
        "publisher.publisher",
        "publisher.close",
        "outbox.core",
        "outbox.outbox",
        "outbox.close",
    ]
