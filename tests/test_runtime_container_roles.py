from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_http_runtime_constructs_container_with_explicit_bootstrap_policy() -> None:
    source = (ROOT / "forwin" / "http" / "runtime.py").read_text(encoding="utf-8")

    assert "role=\"api\"" in source
    assert "policy=RuntimePolicy.for_profile(\"standard\")" in source
    assert "self.container.build_chapter_pipeline()" in source
    assert "ChapterPipeline(self.config)" not in source


def test_cli_delegates_worker_runtime_construction_to_role_factories() -> None:
    cli_source = (ROOT / "forwin" / "cli.py").read_text(encoding="utf-8")
    worker_source = (ROOT / "forwin" / "runtime" / "workers.py").read_text(
        encoding="utf-8"
    )

    assert "build_generation_worker_runtime" in cli_source
    assert "build_publisher_worker_runtime" in cli_source
    assert "RuntimeContainer" not in cli_source
    assert worker_source.count("policy=RuntimePolicy.for_profile(\"standard\")") == 2
    assert "RuntimeContainer.for_generation_worker" in worker_source
    assert "RuntimeContainer.for_publisher_worker" in worker_source


def test_llm_eval_uses_explicit_runtime_policy() -> None:
    source = (ROOT / "forwin" / "llm_eval" / "runner.py").read_text(
        encoding="utf-8"
    )

    assert "ChapterPipeline(" not in source
    assert "RuntimePolicy.for_profile" in source
    assert "role=\"generation_worker\"" in source
