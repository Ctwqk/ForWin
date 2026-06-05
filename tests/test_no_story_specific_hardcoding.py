from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from forwin.book_genesis_core.fallbacks import _fallback_map, _fallback_world

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
    REPO_ROOT / "tests" / "test_chapter18_repair_routing_regression.py",
    REPO_ROOT / "tests" / "test_form_coercion_dict_bool.py",
    REPO_ROOT / "tests" / "test_repair_loop_detection.py",
    REPO_ROOT / "tests" / "test_repair_scope_router_dispatch.py",
    REPO_ROOT / "tests" / "test_subworld_admission_auto_population.py",
]

LOCAL_REWRITE_STORY_TERMS = (
    "旧城通道",
    "韩青",
)

ALLOWED_PRODUCTION_FILES: set[str] = set()

ALLOWED_PRODUCTION_MECHANISM_FILES: set[str] = {
    "forwin/api_project_ops.py",
    "forwin/project_ops/common.py",
    "forwin/canon_quality/rule_profile.py",
    "forwin/orchestrator/loop.py",
    "forwin/orchestrator_loop_core/repair_loop.py",
    "forwin/orchestrator_loop_core/repair_patches.py",
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
    inspected_files = [REPO_ROOT / "forwin" / "reviser" / "local_rewrite_executor.py"]

    assert _violations(inspected_files, terms=LOCAL_REWRITE_STORY_TERMS) == []


def test_genesis_deterministic_fallback_has_no_case_specific_story_anchors() -> None:
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
    serialized = json.dumps({"world": world, "map": map_atlas}, ensure_ascii=False)

    for banned in ("陆明", "韩青", "主舞台", "权力中心", "危险边缘", "待补充", "默认占位"):
        assert banned not in serialized
    assert "宁望舒" in serialized
