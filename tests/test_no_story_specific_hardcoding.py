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

ALLOWED_PRODUCTION_FILES: set[str] = set()


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


def _violations(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        relative = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for term in BANNED_CURRENT_BOOK_TERMS:
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
    assert _violations(inspected_files) == []


def test_python_tests_do_not_reintroduce_current_book_fixture() -> None:
    inspected_files = [
        path
        for path in _python_files([REPO_ROOT / "tests"])
        if path.name != "test_no_story_specific_hardcoding.py"
    ]
    assert _violations(inspected_files) == []


def test_extension_tests_do_not_reintroduce_current_book_fixture() -> None:
    inspected_files = _files_with_suffixes(
        [REPO_ROOT / "browser_extension" / "forwin-publisher" / "tests"],
        {".js", ".ts", ".tsx"},
    )
    assert _violations(inspected_files) == []
