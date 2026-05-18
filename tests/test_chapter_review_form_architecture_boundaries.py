from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = ROOT / "forwin"


DELETED_MODULE_PATHS = [
    "forwin/canon_quality/character_state.py",
    "forwin/canon_quality/countdown_ledger.py",
    "forwin/canon_quality/countdown",
    "forwin/canon_quality/identity.py",
    "forwin/canon_quality/final_completion.py",
    "forwin/canon_quality/prompt_json",
    "forwin/planning/prompt_json",
    "forwin/gate/prompt_json",
]


FORBIDDEN_IMPORT_SNIPPETS = [
    "canon_quality.character_state",
    "canon_quality.countdown_ledger",
    "canon_quality.identity",
    "canon_quality.final_completion",
    "canon_quality.prompt_json",
    "planning.prompt_json",
    "gate.prompt_json",
]


def test_deleted_legacy_analyzer_paths_are_gone() -> None:
    existing = [path for path in DELETED_MODULE_PATHS if (ROOT / path).exists()]

    assert existing == []


def test_production_code_does_not_import_deleted_analyzers() -> None:
    offenders: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if any(snippet in text for snippet in FORBIDDEN_IMPORT_SNIPPETS):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_prompt_json_directories_are_not_reintroduced() -> None:
    dirs = [
        str(path.relative_to(ROOT))
        for path in PRODUCTION_ROOT.rglob("prompt_json")
        if path.is_dir() and "__pycache__" not in path.parts
    ]

    assert dirs == []


def test_production_code_has_no_fixture_specific_story_names() -> None:
    fixture_specific_name = "\u6d1b" + "\u5ead" + "\u82e5"
    offenders: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if fixture_specific_name in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
