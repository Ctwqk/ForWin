from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCTION_PATHS = [
    REPO_ROOT / "forwin",
]

BANNED_CURRENT_BOOK_TERMS = (
    "d2338a0e8bfe4e00a068b03ce9e9b0bf",
    "旧城遗档",
    "白塔重置",
    "林澈",
    "沈宴秋",
    "洛庭若",
    "顾临川",
    "林远舟",
    "沈砚",
    "白塔",
    "岫苑",
    "地下旧轨",
    "潮汐钟楼",
    "失忆广场",
    "档案公会",
    "白塔巡检员",
    "林氏",
)

BANNED_CURRENT_BOOK_MECHANISM_TERMS = (
    "记忆重置",
    "终端审计",
    "核心层",
    "档案清理",
    "记忆熔铸",
    "熔铸协议",
)

ALLOWED_PRODUCTION_FILES: set[str] = set()

ALLOWED_PRODUCTION_MECHANISM_FILES: set[str] = {
    # Phase 1 makes the old overfitting visible. Later cleanup phases shrink
    # this allowlist until only explicit legacy/profile data files remain.
    "forwin/api_project_ops.py",
    "forwin/project_ops/common.py",
    "forwin/canon_quality/rule_profile.py",
    "forwin/canon_quality/countdown_ledger.py",
    "forwin/canon_quality/countdown/filters.py",
    "forwin/canon_quality/countdown/keys.py",
    "forwin/canon_quality/countdown/retrospective.py",
    "forwin/canon_quality/final_completion.py",
    "forwin/orchestrator/loop.py",
    "forwin/orchestrator_loop_core/repair_loop.py",
    "forwin/planning/future_plan_auditor.py",
    "forwin/planning/future_plan_audit/helpers.py",
    "forwin/writer/prompts.py",
}


def _python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _files_with_suffixes(paths: list[Path], suffixes: set[str]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in suffixes
            and "__pycache__" not in path.parts
            and "node_modules" not in path.parts
        )
    return sorted(files)


def _violations(paths: list[Path], *, terms: tuple[str, ...]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        relative = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for term in terms:
            if term in text:
                violations.append(f"{relative}: contains {term!r}")
    return violations


def test_production_code_has_no_current_book_hardcoding() -> None:
    inspected_files: list[Path] = []
    for path in _python_files(PRODUCTION_PATHS):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED_PRODUCTION_FILES:
            continue
        inspected_files.append(path)
    assert _violations(inspected_files, terms=BANNED_CURRENT_BOOK_TERMS) == []


def test_production_code_has_no_untracked_current_book_mechanism_hardcoding() -> None:
    inspected_files: list[Path] = []
    for path in _python_files(PRODUCTION_PATHS):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED_PRODUCTION_MECHANISM_FILES:
            continue
        inspected_files.append(path)
    assert _violations(inspected_files, terms=BANNED_CURRENT_BOOK_MECHANISM_TERMS) == []


def test_python_tests_do_not_reintroduce_current_book_fixture() -> None:
    inspected_files = [
        path
        for path in _python_files([REPO_ROOT / "tests"])
        if path.name != "test_no_story_specific_hardcoding.py"
    ]
    assert _violations(inspected_files, terms=BANNED_CURRENT_BOOK_TERMS) == []


def test_extension_tests_do_not_reintroduce_current_book_fixture() -> None:
    inspected_files = _files_with_suffixes(
        [REPO_ROOT / "browser_extension" / "forwin-publisher" / "tests"],
        {".js", ".ts", ".tsx"},
    )
    assert _violations(inspected_files, terms=BANNED_CURRENT_BOOK_TERMS) == []
