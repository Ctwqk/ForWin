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
        generation_audit_interval=6,
        generation_audit_pauses=False,
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
