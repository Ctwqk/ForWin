from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from forwin.models.project import Project
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import (
    ProjectPolicyMissing,
    ProjectPolicyStore,
    ProjectPolicyVersionConflict,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Project.__table__.create(engine)
    with Session(engine) as value:
        yield value


def test_project_policy_store_round_trips_and_increments_version(session: Session) -> None:
    project = Project(
        id="p1",
        title="T",
        premise="P",
        runtime_policy_json="",
        runtime_policy_version=0,
    )
    session.add(project)
    store = ProjectPolicyStore(session)

    created = store.initialize(project, RuntimePolicy.for_profile("standard"))
    updated = store.save(
        project,
        created.policy.with_user_settings(gate_delegate="spark"),
        expected_version=1,
    )

    assert created.version == 1
    assert updated.version == 2
    assert store.load(project).policy.pause.gate_delegate == "spark"


def test_project_policy_store_rejects_stale_and_missing_policy(session: Session) -> None:
    project = Project(
        id="p2",
        title="T",
        premise="P",
        runtime_policy_json="",
        runtime_policy_version=0,
    )
    session.add(project)
    store = ProjectPolicyStore(session)

    with pytest.raises(ProjectPolicyMissing):
        store.load(project)
    store.initialize(project, RuntimePolicy.for_profile("standard"))
    with pytest.raises(ProjectPolicyVersionConflict):
        store.save(project, RuntimePolicy.for_profile("pulp"), expected_version=0)
