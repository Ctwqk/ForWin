from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_api_app_constructs_runtime_container_with_explicit_bootstrap_policy() -> None:
    source = (ROOT / "forwin" / "api_core" / "app.py").read_text(encoding="utf-8")

    assert "role=\"api\"" in source
    assert "policy=RuntimePolicy.for_profile(\"standard\")" in source
    assert "ChapterPipeline(api_state._config)" not in source


def test_cli_constructs_runtime_only_through_explicit_policy_containers() -> None:
    source = (ROOT / "forwin" / "cli.py").read_text(encoding="utf-8")

    assert "ChapterPipeline(config)" not in source
    assert source.count("policy=RuntimePolicy.for_profile(\"standard\")") >= 2
    assert "role=\"publisher_worker\"" in source


def test_llm_eval_uses_explicit_runtime_policy() -> None:
    source = (ROOT / "forwin" / "llm_eval" / "runner.py").read_text(
        encoding="utf-8"
    )

    assert "ChapterPipeline(" not in source
    assert "RuntimePolicy.for_profile" in source
