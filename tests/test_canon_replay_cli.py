from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import canon_replay


class FakeReplayClient:
    def __init__(self, profiles):
        self.profiles = profiles
        self.api_key = "primary-key"
        self.base_url = "https://primary.example/v1"
        self.model = "primary-model"
        self.profile_id = ""
        self.profile_name = ""
        self.fallback_profiles = list(profiles)

    def _request_profiles(self):
        return [
            {
                "id": "",
                "name": "default",
                "api_key": self.api_key,
                "base_url": self.base_url,
                "model": self.model,
            },
            *self.profiles,
            *self.fallback_profiles,
        ]


class FakeConfigWithProfiles:
    llm_env_profiles = [
        {
            "id": "env-deepseek",
            "name": "DeepSeek",
            "api_key": "key",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        }
    ]
    llm_fallback_profiles = []


def test_parse_args_defaults_to_dry_run() -> None:
    args = canon_replay.parse_args(["--project-id", "p1", "--from-chapter", "1"])

    assert args.project_id == "p1"
    assert args.from_chapter == 1
    assert args.to_chapter is None
    assert args.dry_run is True
    assert args.persist is False


def test_parse_args_rejects_dry_run_and_persist_together() -> None:
    with pytest.raises(SystemExit):
        canon_replay.parse_args(
            ["--project-id", "p1", "--from-chapter", "1", "--dry-run", "--persist"]
        )


def test_emit_json_line_prints_one_json_object(capsys) -> None:
    canon_replay.emit_json_line({"status": "ok", "chapter_number": 1})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "ok", "chapter_number": 1}
    assert captured.out.endswith("\n")


def test_build_llm_client_for_replay_selects_complete_profile() -> None:
    client = canon_replay.build_llm_client_for_replay(
        object(),
        requested_profile="env-deepseek",
        client_builder=lambda _config: FakeReplayClient(
            [
                {
                    "id": "env-deepseek",
                    "name": "DeepSeek",
                    "api_key": "key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                }
            ]
        ),
    )

    assert client.profile_id == "env-deepseek"
    assert client.api_key == "key"
    assert client.base_url == "https://api.deepseek.com/v1"
    assert client.model == "deepseek-chat"
    assert client.fallback_profiles == []


def test_build_llm_client_for_replay_merges_config_env_profiles() -> None:
    client = canon_replay.build_llm_client_for_replay(
        FakeConfigWithProfiles(),
        requested_profile="env-deepseek",
        client_builder=lambda _config: FakeReplayClient([]),
    )

    assert client.profile_id == "env-deepseek"
    assert client.api_key == "key"
    assert client.fallback_profiles == []


def test_build_llm_client_for_replay_uses_runtime_container_default_builder(monkeypatch) -> None:  # noqa: ANN001
    from forwin.runtime.container import RuntimeContainer

    sentinel = object()
    monkeypatch.setattr(RuntimeContainer, "_build_llm_client", staticmethod(lambda _config: sentinel))

    assert canon_replay.build_llm_client_for_replay(object()) is sentinel


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "bad", "name": "bad", "api_key": "", "base_url": "https://api.example/v1", "model": "m"},
        {"id": "bad", "name": "bad", "api_key": "key", "base_url": "", "model": "m"},
        {"id": "bad", "name": "bad", "api_key": "key", "base_url": "https://api.example/v1", "model": ""},
    ],
)
def test_build_llm_client_for_replay_rejects_incomplete_profile(profile) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit, match="LLM profile not found or incomplete"):
        canon_replay.build_llm_client_for_replay(
            object(),
            requested_profile="bad",
            client_builder=lambda _config: FakeReplayClient([profile]),
        )


def test_build_llm_client_for_replay_rejects_unknown_profile() -> None:
    with pytest.raises(SystemExit, match="LLM profile not found or incomplete"):
        canon_replay.build_llm_client_for_replay(
            object(),
            requested_profile="missing",
            client_builder=lambda _config: FakeReplayClient([]),
        )


def test_clear_state_requires_confirm_clear(tmp_path: Path) -> None:
    state_path = tmp_path / "canon_replay" / "p1" / "1-2.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")

    result = canon_replay.clear_state_if_requested(
        clear_state=True,
        confirm_clear=False,
        state_path=state_path,
    )

    assert result["status"] == "error"
    assert state_path.exists()


def test_clear_state_deletes_when_confirmed(tmp_path: Path) -> None:
    state_path = tmp_path / "canon_replay" / "p1" / "1-2.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")

    result = canon_replay.clear_state_if_requested(
        clear_state=True,
        confirm_clear=True,
        state_path=state_path,
    )

    assert result["status"] == "cleared"
    assert not state_path.exists()


def test_missing_cost_cap_returns_structured_error() -> None:
    result = canon_replay.validate_cost_cap_args(cost_cap_usd=None, no_cost_cap=False)

    assert result == {
        "status": "error",
        "error": "missing_cost_cap",
        "message": "Pass --cost-cap-usd <N> or --no-cost-cap.",
    }


def test_schema_version_warning_when_pinned_version_differs() -> None:
    warning = canon_replay.schema_version_warning(
        requested_schema_version="chapter_review_form.v2",
        current_schema_version="chapter_review_form.v1",
    )

    assert warning == {
        "status": "warning",
        "warning": "schema_version_mismatch",
        "requested_schema_version": "chapter_review_form.v2",
        "current_schema_version": "chapter_review_form.v1",
    }


def test_schema_version_warning_empty_when_unpinned_or_matching() -> None:
    assert canon_replay.schema_version_warning(
        requested_schema_version="",
        current_schema_version="chapter_review_form.v1",
    ) == {}
    assert canon_replay.schema_version_warning(
        requested_schema_version="chapter_review_form.v1",
        current_schema_version="chapter_review_form.v1",
    ) == {}
