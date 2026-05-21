from __future__ import annotations

import pytest
from pydantic import ValidationError

from forwin.api_schema.project import ProjectCreateRequest, ProjectExtendGenerationRequest
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from tests.postgres import postgres_test_url


def test_project_create_accepts_thousand_chapter_target() -> None:
    req = ProjectCreateRequest(title="长篇", premise="p", target_total_chapters=1000)

    assert req.target_total_chapters == 1000


def test_project_create_rejects_above_contract_limit() -> None:
    with pytest.raises(ValidationError):
        ProjectCreateRequest(title="太长", premise="p", target_total_chapters=5001)


def test_project_extend_accepts_factory_sized_extension() -> None:
    req = ProjectExtendGenerationRequest(additional_chapters=500)

    assert req.additional_chapters == 500


def test_project_model_default_target_total_is_long_run_ready() -> None:
    engine = get_engine(postgres_test_url("project-target-default"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(id="project-default-target", title="长篇", premise="p", genre="都市")
            session.add(project)
            session.flush()
            assert project.target_total_chapters == 50
    finally:
        engine.dispose()
