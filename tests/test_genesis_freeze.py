from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.genesis_workspace.service import GenesisWorkspaceService


def test_genesis_workspace_rejects_mutation_after_writing_handoff() -> None:
    service = GenesisWorkspaceService(owner=object())
    project = SimpleNamespace(id="project-1", creation_status="writing")
    revision = SimpleNamespace(id="revision-1", status="locked")

    with pytest.raises(ValueError, match="Genesis 已冻结"):
        service.patch_pack(
            session=None,
            updater=None,
            project=project,
            revision=revision,
            patch={"book_brief": {"one_line": "late mutation"}},
        )
