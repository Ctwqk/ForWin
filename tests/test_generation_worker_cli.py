from __future__ import annotations

import logging

from sqlalchemy import select

from forwin.config import Config
from forwin.cli import _get_config
from forwin.generation.worker import GenerationWorkerResult
from forwin.generation.worker_cli import run_generation_worker_loop
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from tests.postgres import postgres_test_url


def test_generation_worker_cli_config_loads_container_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "FORWIN_DATABASE_URL",
        "postgresql+psycopg://forwin:forwin@postgres:5432/forwin",
    )
    monkeypatch.setenv("FORWIN_QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-env")

    class Args:
        database_url = None
        api_key = None
        model = None
        base_url = None

    config = _get_config(Args())

    assert config.database_url == "postgresql+psycopg://forwin:forwin@postgres:5432/forwin"
    assert config.qdrant_url == "http://qdrant:6333"
    assert config.minimax_api_key == "sk-env"


def test_generation_worker_loop_once_exits_when_no_task(caplog) -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    _enable_worker_cli_logging(caplog, logging.DEBUG)
    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=True,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["worker_id"] == "worker-test"
    messages = [record.getMessage() for record in caplog.records]
    assert any("Generation worker starting" in message for message in messages)
    assert any("No claimable generation task" in message for message in messages)
    assert any("Generation worker stopping" in message for message in messages)


def test_generation_worker_loop_no_claim_does_not_write_decision_events(caplog) -> None:
    database_url = postgres_test_url("generation-worker-cli-no-claim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)

    def fake_run_once(**_kwargs):
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    try:
        _enable_worker_cli_logging(caplog, logging.DEBUG)
        exit_code = run_generation_worker_loop(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key="sk-test"),
            worker_id="worker-test",
            lease_seconds=300,
            poll_interval=0,
            once=True,
            run_once=fake_run_once,
        )

        assert exit_code == 0
        with Session() as session:
            count = len(session.execute(select(DecisionEvent)).scalars().all())
        assert count == 0
    finally:
        engine.dispose()


def test_generation_worker_loop_polls_until_stop_after_claim(caplog) -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return GenerationWorkerResult(
                claimed=True,
                task_id="task-1",
                project_id="project-1",
                resume_from_chapter=7,
                executed=True,
            )
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    _enable_worker_cli_logging(caplog, logging.DEBUG)
    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=False,
        max_loops=2,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert any("Generation worker executed task task-1" in message for message in messages)
    assert any("project_id=project-1" in message for message in messages)
    assert any("resume_from_chapter=7" in message for message in messages)


def test_generation_worker_loop_logs_exception_before_raising(caplog) -> None:
    def fake_run_once(**_kwargs):
        raise RuntimeError("loop failed")

    _enable_worker_cli_logging(caplog, logging.ERROR)
    try:
        run_generation_worker_loop(
            session_factory=lambda: None,
            config=Config(minimax_api_key="sk-test"),
            worker_id="worker-test",
            lease_seconds=300,
            poll_interval=0,
            once=True,
            run_once=fake_run_once,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("run_generation_worker_loop should propagate loop failure")

    assert any("Generation worker loop failed" in record.getMessage() for record in caplog.records)


def _enable_worker_cli_logging(caplog, level: int) -> None:
    logging.disable(logging.NOTSET)
    logger = logging.getLogger("forwin.generation.worker_cli")
    logger.disabled = False
    logger.propagate = True
    caplog.set_level(level, logger="forwin.generation.worker_cli")
