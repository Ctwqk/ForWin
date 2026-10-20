from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from forwin.arc_sizing import allocate_arc_chapter_sizes
from forwin.genesis.fallbacks import (
    _fallback_blueprint,
    _fallback_map,
    _fallback_story_engine,
    _fallback_world,
)
from tests.overfit_guard import canary_terms, load_allowlist

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "tests" / "overfit_allowlist.toml"

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
    "守仓阙微阑",
    "礼川诸州",
    "裴星野",
)

BANNED_CURRENT_BOOK_MECHANISM_TERMS = (
    "记忆重置",
    "终端审计",
    "核心层",
    "档案清理",
    "记忆熔铸",
    "熔铸协议",
    "隐藏子程序倒计时",
)

CHAPTER_REPAIR_STORY_TERMS = BANNED_CURRENT_BOOK_TERMS + (
    # This name still appears in older generic-story fixtures. Keep the chapter
    # repair guard strict without broadening this follow-up into a legacy
    # fixture migration.
    "韩青",
)

CHAPTER_REPAIR_TEST_PATHS = [
    REPO_ROOT / "tests" / "fixtures" / "repair_routing",
    REPO_ROOT / "tests" / "test_active_rule_store.py",
    REPO_ROOT / "tests" / "test_active_rules_auto_registration.py",
    REPO_ROOT / "tests" / "test_form_coercion_dict_bool.py",
]

LOCAL_REWRITE_STORY_TERMS = (
    "旧城通道",
    "韩青",
)

ALLOWED_PRODUCTION_FILES: set[str] = set()

ALLOWED_PRODUCTION_MECHANISM_FILES: set[str] = set()


def _python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _files_with_suffixes(paths: list[Path], suffixes: set[str]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        candidates = [root] if root.is_file() else root.rglob("*")
        files.extend(
            path
            for path in candidates
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


def test_chapter_repair_tests_do_not_reintroduce_case_specific_terms() -> None:
    inspected_files = _files_with_suffixes(CHAPTER_REPAIR_TEST_PATHS, {".json", ".md", ".py"})

    assert _violations(inspected_files, terms=CHAPTER_REPAIR_STORY_TERMS) == []
    assert _violations(inspected_files, terms=BANNED_CURRENT_BOOK_MECHANISM_TERMS) == []


def test_local_rewrite_executor_has_no_case_specific_placeholder_defaults() -> None:
    inspected_files = [
        REPO_ROOT / "forwin" / "review" / "repair" / "local_rewrite_executor.py"
    ]

    assert _violations(inspected_files, terms=LOCAL_REWRITE_STORY_TERMS) == []


def test_fallback_stages_are_structural_shells() -> None:
    project = SimpleNamespace(
        title="六十章最终审计压力测试·星门余烬",
        genre="科幻悬疑",
        premise=(
            "主角：宁望舒，星门守门人。边境星门在一次静默潮汐后失去坐标，"
            "记忆审计局试图抹除失踪舰队的真相。"
        ),
        setting_summary="近未来边境城邦、失控星门、记忆审计机构、舰队遗民与潮汐灾变。",
        target_total_chapters=60,
    )
    pack = {
        "book_brief": {
            "title": project.title,
            "premise": project.premise,
            "genre": project.genre,
            "setting_seed": project.setting_summary,
            "target_total_chapters": project.target_total_chapters,
        }
    }

    world = _fallback_world(project, pack)
    fallback_pack = {**pack, "world": world}
    map_atlas = _fallback_map(fallback_pack)
    story_engine = _fallback_story_engine(fallback_pack)
    blueprint = _fallback_blueprint(project, fallback_pack)

    # The world overview is the author's own setting summary, carried verbatim; every
    # other field is empty structure.
    world_bible = world["world_bible"]
    assert world_bible == {
        "overview": project.setting_summary,
        "axioms": [],
        "history_slice": "",
        "naming_style": "",
        "forbidden_zones": [],
        "culture_profiles": [],
    }

    assert [item["id"] for item in map_atlas["submaps"]] == ["subworld-1"]
    assert [item["id"] for item in map_atlas["regions"]] == ["region-1"]
    assert [item["id"] for item in map_atlas["nodes"]] == ["node-1"]
    assert map_atlas["edges"] == []
    for row in (*map_atlas["submaps"], *map_atlas["regions"], *map_atlas["nodes"]):
        assert row["name"] == ""

    assert story_engine == {
        "core_cast": [],
        "factions": [],
        "opposition": [],
        "relationship_axes": [],
        "reader_promises": [],
        "long_arcs": [],
    }

    sizes = list(allocate_arc_chapter_sizes(project.target_total_chapters))
    assert [arc["chapter_count"] for arc in blueprint["arcs"]] == sizes
    assert [arc["target_size"] for arc in blueprint["arcs"]] == sizes
    for arc in blueprint["arcs"]:
        for key in ("title", "arc_synopsis", "goal", "stakes", "payoff_direction"):
            assert arc[key] == ""

    # Nothing from the premise and nothing from the seed blacklist may be fabricated into
    # the shells. The world root's template libraries are a catalog the fallback does not
    # write, so only the fallback's own world_bible is inspected.
    shells = {
        "world_bible": {**world_bible, "overview": ""},
        "map": map_atlas,
        "story_engine": story_engine,
        "blueprint": blueprint,
    }
    serialized = json.dumps(shells, ensure_ascii=False)
    for name in ("宁望舒", "星门"):
        assert name not in serialized
    blacklist = canary_terms(REPO_ROOT, load_allowlist(ALLOWLIST_PATH).canary)
    assert sorted(term for term in blacklist if term in serialized) == []
