from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from forwin.application.projects import genesis as project_genesis
from forwin.audit.events import DecisionEventType
from forwin.config import InfrastructureConfig
from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.genesis.handoff.map_bootstrap import GenesisMapBootstrapError
from forwin.http.request_support import _active_genesis_revision, _require_genesis_project
from forwin.map.protocol import BookMapGenerationResult, MapValidationReport
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater
from tests.helpers.genesis_seed import seeded_map_atlas, seeded_story_engine, seeded_world_seed
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


_GATE_TIMEOUT_SECONDS = 60


class _NoApiKeyClient:
    api_key = ""
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "fake-model"
    base_url = ""
    last_call_result = None

    def chat(self, *_args, **_kwargs):  # pragma: no cover - the launch-arc call is patched
        raise AssertionError("handoff tests must not call the LLM")


def _fake_launch_arc_call(_owner, *, messages, fallback, stage_key, temperature=0.45, max_tokens=None):
    trace = {
        "effective_system_prompt": "",
        "prompt_layers": [],
        "input_snapshot": {"stage_key": stage_key},
        "model_profile": {"model": "fake-model"},
        "attempts": [{"attempt": 1, "status": "success"}],
        "output_summary": {"mode": "success"},
    }
    if str(stage_key).startswith("launch_arc_"):
        chapters = [{"title": f"Chapter {n}", "one_line": f"Beat {n}.", "goals": [f"goal {n}"]} for n in (1, 2, 3)]
        return {"chapters": chapters}, trace
    return fallback, trace


@pytest.fixture
def db():
    url = postgres_test_url("handoff-atomicity")
    engine = get_engine(url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    config = InfrastructureConfig(
        database_url=url,
        minimax_api_key="saved-key",
        minimax_base_url="https://api.minimaxi.com/v1",
        minimax_model="MiniMax-M2.7",
    )
    harness = HttpRuntimeHarness(session_factory=session_factory, config=config, engine=engine)
    with patch("forwin.genesis.BookGenesisService._call_json_with_trace", new=_fake_launch_arc_call):
        yield SimpleNamespace(session_factory=session_factory, harness=harness, config=config)
    engine.dispose()


def _genesis_service() -> BookGenesisService:
    return BookGenesisService(llm_client=_NoApiKeyClient())


def _ready_project(session_factory, project_id: str) -> str:
    service = _genesis_service()
    with session_factory() as session:
        updater = StateUpdater(session)
        project = Project(id=project_id, title="Handoff", premise="Premise.", genre="test", target_total_chapters=3)
        ProjectPolicyStore(session).initialize(project, RuntimePolicy.for_profile("standard"))
        revision = service.create_initial_revision(session=session, updater=updater, project=project)
        revision = service.patch_pack(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
            patch={
                "world": {
                    "world_bible": {"overview": "Overview."},
                    "map_atlas": seeded_map_atlas("Atlas."),
                    "story_engine": seeded_story_engine(["Arc."]),
                },
                "book_arc_blueprint": {
                    "summary": "Blueprint",
                    "arcs": [
                        {
                            "arc_number": 1,
                            "title": "Arc 1",
                            "arc_synopsis": "Synopsis.",
                            "chapter_start": 1,
                            "chapter_end": 3,
                            "chapter_count": 3,
                            "target_size": 3,
                            "soft_min": 2,
                            "soft_max": 4,
                        }
                    ],
                },
                "world_seed": seeded_world_seed(),
            },
        )
        for stage_key in ("brief", "world", "map", "story_engine", "book_blueprint", "world_seed"):
            revision = service.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key=stage_key
            )
        session.commit()
    return project_id


def _start_writing(db, project_id: str, *, get_session=None, genesis_service=None):
    return project_genesis.start_project_writing(
        project_id,
        get_session=get_session or db.session_factory,
        config=db.config,
        build_genesis_service=lambda _config, **_kwargs: genesis_service or _genesis_service(),
        close_genesis_service=lambda _service: None,
        require_genesis_project=_require_genesis_project,
        active_genesis_revision=_active_genesis_revision,
        project_has_active_generation_task=db.harness._project_has_active_generation_task,
        generation_task_conflict_message=lambda project_id: f"conflict {project_id}",
        create_continue_generation_task=db.harness._create_continue_generation_task,
    )


def _project_tasks(session, project_id: str) -> list[GenerationTask]:
    return list(session.execute(select(GenerationTask).where(GenerationTask.project_id == project_id)).scalars())


def test_failed_handoff_leaves_no_generation_task(db) -> None:
    project_id = _ready_project(db.session_factory, "proj-handoff-commit-fails")

    def session_whose_commit_fails():
        session = db.session_factory()

        @event.listens_for(session, "before_commit")
        def _fail(committing):
            # Savepoints inside the handoff commit too; only the outer commit fails.
            if not committing.in_nested_transaction():
                raise RuntimeError("handoff commit failed")

        return session

    with pytest.raises(RuntimeError, match="handoff commit failed"):
        _start_writing(db, project_id, get_session=session_whose_commit_fails)

    with db.session_factory() as session:
        assert _project_tasks(session, project_id) == []
        assert session.get(Project, project_id).creation_status == "genesis_ready"
        event_types = session.execute(
            select(DecisionEvent.event_type).where(DecisionEvent.project_id == project_id)
        ).scalars()
        assert DecisionEventType.START_WRITING_REQUESTED not in set(event_types)


class _GatedHandoff:
    """Holds the first request inside its handoff until the second one has answered."""

    def __init__(self, inner, *, entered: threading.Event, release: threading.Event) -> None:
        self.inner = inner
        self.entered = entered
        self.release = release

    def start_writing(self, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=_GATE_TIMEOUT_SECONDS):
            raise AssertionError("the second start-writing request never answered")
        return self.inner.start_writing(**kwargs)


def test_concurrent_start_writing_second_gets_409(db) -> None:
    project_id = _ready_project(db.session_factory, "proj-handoff-concurrent")
    entered, release = threading.Event(), threading.Event()
    gated = SimpleNamespace(handoff=_GatedHandoff(_genesis_service().handoff, entered=entered, release=release))
    first: dict[str, object] = {}

    def run_first() -> None:
        try:
            first["result"] = _start_writing(db, project_id, genesis_service=gated)
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            first["result"] = exc
            entered.set()

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(timeout=_GATE_TIMEOUT_SECONDS)
        with pytest.raises(HTTPException) as second:
            _start_writing(db, project_id)
    finally:
        release.set()
        thread.join(timeout=_GATE_TIMEOUT_SECONDS * 2)

    assert (second.value.status_code, second.value.detail) == (409, "项目正被其他操作占用，请稍后重试。")
    assert not thread.is_alive()
    assert not isinstance(first["result"], BaseException), first["result"]
    with db.session_factory() as session:
        tasks = _project_tasks(session, project_id)
        assert [task.id for task in tasks] == [first["result"].task_id]
        assert session.get(Project, project_id).creation_status == "writing"


def test_failure_event_has_no_dangling_parent(db) -> None:
    project_id = _ready_project(db.session_factory, "proj-handoff-failure-parent")
    invalid_map = BookMapGenerationResult(
        project_id=project_id,
        validation_report=MapValidationReport(valid=False, errors=["anchor node unreachable"]),
    )
    with db.session_factory() as session:
        with (
            patch("forwin.genesis.handoff.map_bootstrap.create_or_update_book_map", return_value=invalid_map),
            pytest.raises(GenesisMapBootstrapError),
        ):
            _genesis_service().handoff.start_writing(
                session=session,
                updater=StateUpdater(session),
                command=StartWritingCommand(project_id=project_id, actor_type="manual_ui"),
            )
        session.commit()
        failure = session.execute(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == DecisionEventType.MAP_GENERATION_FAILED,
            )
        ).scalar_one()

    assert failure.parent_event_id == ""
