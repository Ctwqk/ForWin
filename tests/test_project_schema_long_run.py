from __future__ import annotations

import pytest
from pydantic import ValidationError

from forwin.api_schema.project import ProjectCreateRequest, ProjectExtendGenerationRequest


def test_project_create_accepts_thousand_chapter_target() -> None:
    req = ProjectCreateRequest(title="长篇", premise="p", target_total_chapters=1000)

    assert req.target_total_chapters == 1000


def test_project_create_rejects_above_contract_limit() -> None:
    with pytest.raises(ValidationError):
        ProjectCreateRequest(title="太长", premise="p", target_total_chapters=5001)


def test_project_extend_accepts_factory_sized_extension() -> None:
    req = ProjectExtendGenerationRequest(additional_chapters=500)

    assert req.additional_chapters == 500
