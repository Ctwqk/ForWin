from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = ROOT / "forwin"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "chapter_review_form"
FIXTURE_ROOTS = [
    ROOT / "tests" / "fixtures" / "chapter_review_form",
    ROOT / "tests" / "fixtures" / "repair_routing",
]


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


FORBIDDEN_FIXTURE_TERMS = [
    "\u6d1b\u5ead\u82e5",
    "\u6797\u6f88",
    "\u987e\u4e34\u5ddd",
    "\u6c88\u5bb4\u79cb",
    "\u767d\u5854",
    "\u65e7\u57ce",
    "\u9057\u5fd8\u4e4b\u4e95",
    "\u9ec4\u94dc\u94a5\u5319",
    "\u6f6e\u6c50\u949f\u697c",
    "\u6863\u6848\u516c\u4f1a",
    "\u5b88\u4ed3\u9619\u5fae\u9611",
    "\u793c\u5ddd\u8bf8\u5dde",
]

REPAIR_TIME_CANON_STATE_FILES = [
    "forwin/planning/countdown_drift_pre_audit.py",
    "forwin/canon_quality/active_rules_handler.py",
    "forwin/reviewer/repair_handlers/active_rules.py",
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


def test_chapter_review_form_fixture_notes_are_nonempty() -> None:
    assert FIXTURE_ROOT.exists()
    notes = sorted(FIXTURE_ROOT.glob("*/notes.md"))
    assert notes
    empty_notes = [
        str(path.relative_to(ROOT))
        for path in notes
        if not path.read_text(encoding="utf-8").strip()
    ]

    assert empty_notes == []


def test_chapter_review_form_fixtures_do_not_use_story_specific_terms() -> None:
    assert all(path.exists() for path in FIXTURE_ROOTS)
    offenders: list[str] = []
    for root in FIXTURE_ROOTS:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if any(term in text for term in FORBIDDEN_FIXTURE_TERMS):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_repair_time_canonical_state_reads_do_not_bypass_bookstate_query_interface() -> None:
    offenders: list[str] = []
    for relative in REPAIR_TIME_CANON_STATE_FILES:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        if "world_model" in text:
            offenders.append(relative)

    assert offenders == []
