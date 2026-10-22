from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.application.runtime_policy import get_project_policy, update_project_policy
from forwin.api_schema.policy import RuntimePolicyUpdateRequest
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Project.__table__.create(engine)
    DecisionEvent.__table__.create(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_project_create_initializes_standard_runtime_policy(session_factory) -> None:
    with session_factory.begin() as session:
        project = StateUpdater(session).create_project(
            title="T",
            premise="P",
            genre="G",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )

    response = get_project_policy(project.id, session_factory=session_factory)

    assert response.version == 1
    assert response.policy.quality_profile == "standard"
    assert not hasattr(response.policy, "operation_mode")


def test_project_policy_update_requires_expected_version_and_reason(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        project = StateUpdater(session).create_project(
            title="T",
            premise="P",
            genre="G",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
    request = RuntimePolicyUpdateRequest(
        expected_version=1,
        quality_profile="standard",
        model_profile_id="env-kimi",
        min_chapter_chars=2500,
        target_chapter_chars=2800,
        max_chapter_chars=3200,
        review_interval_chapters=0,
        manual_checkpoints=True,
        band_checkpoint_action="pause_on_warn",
        gate_delegate="spark",
        reason="delegate optional pauses",
    )

    response = update_project_policy(
        project.id,
        request,
        session_factory=session_factory,
    )

    assert response.version == 2
    assert response.policy.pause.gate_delegate == "spark"
    with pytest.raises(HTTPException) as exc:
        update_project_policy(project.id, request, session_factory=session_factory)
    assert exc.value.status_code == 409


def _policy_request(**updates) -> RuntimePolicyUpdateRequest:
    return RuntimePolicyUpdateRequest.model_validate({
        "expected_version": 1,
        "quality_profile": "standard",
        "model_profile_id": "env-kimi",
        "min_chapter_chars": 2500,
        "target_chapter_chars": 2800,
        "max_chapter_chars": 3200,
        "review_interval_chapters": 0,
        "manual_checkpoints": True,
        "band_checkpoint_action": "pause_on_warn",
        "gate_delegate": "human",
        "reason": "choose conflict reviewer",
        **updates,
    })


def test_update_world_conflict_delegate_round_trip(session_factory) -> None:
    with session_factory.begin() as session:
        project = StateUpdater(session).create_project(
            title="T", premise="P", genre="G",
            runtime_policy=RuntimePolicy.for_profile("standard"),
        )
    for version, delegate in [(1, "human"), (2, "codex")]:
        response = update_project_policy(
            project.id,
            _policy_request(expected_version=version, world_conflict_delegate=delegate),
            session_factory=session_factory,
        )
        assert response.version == version + 1
        assert response.policy.world.conflict_delegate == delegate
        stored = get_project_policy(project.id, session_factory=session_factory)
        assert stored.policy.world.conflict_delegate == delegate


def test_gate_delegate_update_keeps_world_conflict_delegate(session_factory) -> None:
    with session_factory.begin() as session:
        project = StateUpdater(session).create_project(
            title="T", premise="P", genre="G",
            runtime_policy=RuntimePolicy.for_profile("standard").with_user_settings(
                world_conflict_delegate="human"
            ),
        )
    request = _policy_request(gate_delegate="spark", quality_profile="pulp")
    assert request.world_conflict_delegate is None
    response = update_project_policy(project.id, request, session_factory=session_factory)
    assert response.policy.pause.gate_delegate == "spark"
    assert response.policy.world.conflict_delegate == "human"
    assert response.policy.world.max_actor_turns_per_tick == 3
    stored = get_project_policy(project.id, session_factory=session_factory)
    assert stored.policy.world.conflict_delegate == "human"
