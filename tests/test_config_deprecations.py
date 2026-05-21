from __future__ import annotations

import pytest

from forwin.config import Config


def test_review_engine_live_cutover_env_is_deprecated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST", "project-1")

    with pytest.warns(
        DeprecationWarning,
        match="review engine is globally live",
    ):
        config = Config.from_env()

    assert config.review_engine_live_cutover_enabled is True
    assert config.review_engine_live_cutover_project_allowlist == ["project-1"]
