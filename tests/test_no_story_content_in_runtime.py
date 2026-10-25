"""Guard: ForWin runtime code handles structure, never a specific story's content.

Design: docs/designs/2026-10-20-world-engine-overhaul.md §7.2. The scanner lives in
``tests/overfit_guard.py``; every current hit is classified in
``tests/overfit_allowlist.toml``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.overfit_guard import (
    CATEGORIES,
    PHASE_ORDER,
    Hit,
    load_allowlist,
    scan_runtime,
    scan_source,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "tests" / "overfit_allowlist.toml"


def _scan(source: str, **kwargs: object) -> list[Hit]:
    return scan_source(textwrap.dedent(source), "forwin/sample.py", **kwargs)


def _literals(hits: list[Hit], check: str) -> list[str]:
    return [hit.literal for hit in hits if hit.check == check]


# --- scanner unit tests (in-memory sources, independent of the repository) ---


def test_scanner_detects_story_id_literal_constants() -> None:
    assert _literals(_scan('x = "line_colony_defense"\n'), "story_id_literal") == [
        "line_colony_defense"
    ]


def test_scanner_detects_story_id_fstring_prefix() -> None:
    hits = _scan(
        """
        def build(n):
            return f"fact_sample_ch{n}"
        """
    )
    assert _literals(hits, "story_id_literal") == ["f'fact_sample_ch{n}'"]


def test_scanner_exempts_story_id_keys_and_accessors() -> None:
    hits = _scan(
        """
        def read(d, obj):
            a = d["fact_type"]
            b = d.get("thread_id")
            c = getattr(obj, "world_line_id", None)
            return {"fact_id": a, "x": b, "y": c}
        """
    )
    assert _literals(hits, "story_id_literal") == []


def test_scanner_detects_story_id_keyword_literal_once() -> None:
    hits = _scan('foo(world_line_id="line_a")\nfoo(gap_id="g1")\nfoo(gate_id="canon_quality")\n')
    assert _literals(hits, "story_id_literal") == ["line_a", "g1"]


def test_scanner_detects_compound_story_id_keywords() -> None:
    hits = _scan(
        """
        foo(related_gap_id="secret")
        foo(primary_world_line_id="colony")
        foo(gate_id="x")
        foo(timeline_id="x")
        """
    )
    assert _literals(hits, "story_id_literal") == ["secret", "colony"]


def test_scanner_detects_must_not_reveal_membership() -> None:
    hits = _scan(
        """
        def check(intent):
            return "father_sieged" in intent.must_not_reveal
        """
    )
    assert _literals(hits, "story_id_literal") == ["father_sieged"]


def test_scanner_ignores_generic_main_id() -> None:
    assert _literals(_scan('x = "main"\n'), "story_id_literal") == []
    hits = _scan('foo(world_line_id="main")\n', allowed_ids={"main"})
    assert _literals(hits, "story_id_literal") == []


def test_scanner_applies_allowed_ids_to_fstring_id_token() -> None:
    source = 'a = f"fact_{new_id()}"\nb = f"fact_node:{row.id}"\n'
    assert len(_literals(_scan(source), "story_id_literal")) == 2
    hits = _scan(source, allowed_ids={"fact_", "fact_node"})
    assert _literals(hits, "story_id_literal") == []


def test_scanner_detects_chapter_number_literals() -> None:
    hits = _scan(
        """
        def gate(plan, chapter_number, target_total_chapters):
            if plan.chapter_number == 23:
                return 1
            if chapter_number > 1:
                return 2
            if target_total_chapters >= 150:
                return 3
            return foo(chapter_hint=22)
        """
    )
    assert sorted(_literals(hits, "chapter_number_literal")) == ["22", "23"]


def test_scanner_detects_premise_sniffing() -> None:
    hits = _scan(
        """
        def sniff(all_text, body):
            return "母星" in all_text or "homeworld" in all_text.lower() or "x" in body
        """
    )
    assert _literals(hits, "premise_sniff") == ["母星", "homeworld"]


def test_scanner_detects_cjk_collections() -> None:
    hits = _scan(
        """
        import logging
        logger = logging.getLogger(__name__)

        WORDS = ("一炷香", "时辰")
        SURNAMES = set("赵钱孙")

        class Catalog:
            TABLE = {"键": "值"}

        def run():
            logger.info("开始处理")
        """
    )
    collection = [hit for hit in hits if hit.check == "cjk_collection"]
    assert [(hit.symbol, hit.literal) for hit in collection] == [
        ("WORDS", "一炷香"),
        ("WORDS", "时辰"),
        ("SURNAMES", "赵钱孙"),
        ("Catalog.TABLE", "键"),
        ("Catalog.TABLE", "值"),
    ]
    assert [hit for hit in hits if hit.symbol == "run"] == []


def test_scanner_detects_cjk_regex() -> None:
    hits = _scan(
        """
        import re
        PATTERN = re.compile(r"被(关押|羁押)")
        """
    )
    assert _literals(hits, "cjk_regex") == ["被(关押|羁押)"]
    assert _literals(hits, "cjk_collection") == []


def test_scanner_detects_cjk_membership() -> None:
    hits = _scan(
        """
        def judge(text, x):
            if "救父" in text:
                return True
            return x in ("被捕", "羁押")
        """
    )
    assert _literals(hits, "cjk_membership") == ["救父", "被捕", "羁押"]


def test_scanner_detects_cjk_inline_iteration() -> None:
    hits = _scan(
        """
        def scan(t):
            for w in ("档案", "后门"):
                print(w)
            return any(w in t for w in ("刻痕",))
        """
    )
    assert _literals(hits, "cjk_inline_iteration") == ["档案", "后门", "刻痕"]


def test_scanner_symbols_name_constant_or_function_qualname() -> None:
    hits = _scan(
        """
        WORDS = ["时辰"]

        class Foo:
            def bar(self, text):
                return "救父" in text
        """
    )
    assert {(hit.check, hit.symbol) for hit in hits} == {
        ("cjk_collection", "WORDS"),
        ("cjk_membership", "Foo.bar"),
    }


def test_scanner_detects_fixture_terms(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "case.json").write_text(
        json.dumps({"cast": [{"name": "雾港"}, {"name": "主角"}, {"name": "角色A"}]}),
        encoding="utf-8",
    )
    runtime = tmp_path / "forwin"
    runtime.mkdir()
    (runtime / "leak.py").write_text('A = "雾港"\nB = "主角"\nC = "母星"\n', encoding="utf-8")
    allowlist_path = tmp_path / "tests" / "overfit_allowlist.toml"
    allowlist_path.write_text(
        textwrap.dedent(
            """
            [meta]
            completed_phase = ""

            [story_id_literal]
            allowed_values = []

            [canary]
            seed_terms = ["母星"]
            harvest_keys = ["name"]
            ignore = ["主角"]
            """
        ),
        encoding="utf-8",
    )

    hits = scan_runtime(tmp_path)

    assert [(hit.symbol, hit.line, hit.literal) for hit in hits if hit.check == "fixture_term"] == [
        ("<file>", 1, "雾港"),
        ("<file>", 3, "母星"),
    ]


def test_allowlist_matching_prefers_symbol_entries(tmp_path: Path) -> None:
    allowlist_path = tmp_path / "allowlist.toml"
    allowlist_path.write_text(
        textwrap.dedent(
            """
            [meta]
            completed_phase = ""

            [story_id_literal]
            allowed_values = []

            [canary]
            seed_terms = []
            harvest_keys = []
            ignore = []

            [[entry]]
            path = "forwin/a.py"
            symbol = "WORDS"
            checks = ["cjk_collection"]
            category = "language_lexicon"
            justification = "test"
            max_literals = 1

            [[entry]]
            path = "forwin/a.py"
            checks = ["cjk_collection", "cjk_regex"]
            category = "catalog"
            justification = "test"
            max_literals = 5

            [[entry]]
            path = "forwin/b.py"
            checks = ["cjk_regex"]
            category = "catalog"
            justification = "test"
            max_literals = 5
            """
        ),
        encoding="utf-8",
    )
    allowlist = load_allowlist(allowlist_path)
    hits = [
        Hit("cjk_collection", "forwin/a.py", "WORDS", 1, "一"),
        Hit("cjk_collection", "forwin/a.py", "WORDS", 1, "二"),
        Hit("cjk_collection", "forwin/a.py", "OTHER", 2, "三"),
        Hit("cjk_membership", "forwin/a.py", "OTHER", 3, "四"),
    ]

    assert allowlist.unallowed(hits) == [hits[3]]
    assert [(entry.symbol, count) for entry, count in allowlist.over_budget(hits)] == [("WORDS", 2)]
    assert [entry.path for entry in allowlist.stale_entries(hits)] == ["forwin/b.py"]


def test_allowlist_literals_split_entries_and_pending_budgets_are_exact(tmp_path: Path) -> None:
    allowlist_path = tmp_path / "allowlist.toml"
    allowlist_path.write_text(
        textwrap.dedent(
            """
            [meta]
            completed_phase = ""

            [story_id_literal]
            allowed_values = []

            [canary]
            seed_terms = []
            harvest_keys = []
            ignore = []

            [[entry]]
            path = "forwin/a.py"
            symbol = "<file>"
            checks = ["fixture_term"]
            literals = ["追踪器"]
            category = "pending_removal"
            remove_in = "P0"
            justification = "test"
            max_literals = 3

            [[entry]]
            path = "forwin/a.py"
            symbol = "<file>"
            checks = ["fixture_term"]
            category = "pending_removal"
            remove_in = "P2"
            justification = "test"
            max_literals = 1

            [[entry]]
            path = "forwin/b.py"
            checks = ["cjk_collection"]
            category = "language_lexicon"
            justification = "test"
            max_literals = 3
            """
        ),
        encoding="utf-8",
    )
    allowlist = load_allowlist(allowlist_path)
    p0, p2, lexicon = allowlist.entries
    hits = [
        Hit("fixture_term", "forwin/a.py", "<file>", 1, "追踪器"),
        Hit("fixture_term", "forwin/a.py", "<file>", 2, "追踪器"),
        Hit("fixture_term", "forwin/a.py", "<file>", 3, "羁押室"),
        Hit("cjk_collection", "forwin/b.py", "WORDS", 1, "一"),
    ]

    assert allowlist.unallowed(hits) == []
    # 2 of 3 P0 literals left: a trimmed pending_removal entry must be updated.
    # The lexicon entry stays under its budget, which is fine.
    assert allowlist.over_budget(hits) == [(p0, 2)]
    assert allowlist.stale_entries(hits) == []
    assert (p2.literals, lexicon.literals) == ((), ())


# --- repository guard ---


@pytest.fixture(scope="module")
def allowlist():
    return load_allowlist(ALLOWLIST_PATH)


@pytest.fixture(scope="module")
def runtime_hits(allowlist) -> list[Hit]:
    return scan_runtime(REPO_ROOT, allowlist=allowlist)


def test_runtime_hits_are_all_allowlisted(allowlist, runtime_hits) -> None:
    unallowed = allowlist.unallowed(runtime_hits)
    lines = [
        f"{hit.path}:{hit.line} [{hit.check}] {hit.symbol} {hit.literal!r}" for hit in unallowed
    ]
    assert unallowed == [], (
        "运行代码中出现未登记的故事内容命中：\n"
        + "\n".join(lines)
        + "\n登记到 tests/overfit_allowlist.toml 并写明 category / justification；"
        "剧本内容不得登记，只能删除。"
    )


def test_allowlist_has_no_stale_entries(allowlist, runtime_hits) -> None:
    stale = allowlist.stale_entries(runtime_hits)
    assert stale == [], "allowlist 条目已无命中，请删除：\n" + "\n".join(
        f"{entry.path} {entry.symbol or '<file>'} {list(entry.checks)}" for entry in stale
    )


def test_allowlist_entries_stay_within_budget(allowlist, runtime_hits) -> None:
    over = allowlist.over_budget(runtime_hits)
    assert over == [], (
        "命中数与 max_literals 不符（pending_removal 必须相等，其它类别不得超过），需要更新条目或复审：\n"
        + "\n".join(
            f"{entry.path} {entry.symbol or '<file>'} [{entry.category}]: "
            f"{count} vs max_literals {entry.max_literals}"
            for entry, count in over
        )
    )


def test_pending_removal_entries_name_a_future_phase(allowlist) -> None:
    completed = (
        PHASE_ORDER.index(allowlist.completed_phase) if allowlist.completed_phase else -1
    )
    problems: list[str] = []
    for entry in allowlist.entries:
        label = f"{entry.path} {entry.symbol or '<file>'}"
        if entry.category != "pending_removal":
            if entry.remove_in:
                problems.append(f"{label}: remove_in only applies to pending_removal")
            continue
        if entry.remove_in not in PHASE_ORDER:
            problems.append(f"{label}: remove_in {entry.remove_in!r} not in {PHASE_ORDER}")
        elif PHASE_ORDER.index(entry.remove_in) <= completed:
            problems.append(
                f"{label}: remove_in {entry.remove_in} is not after completed phase "
                f"{allowlist.completed_phase}"
            )
    assert problems == []


def test_allowlist_categories_are_known(allowlist) -> None:
    unknown = [
        f"{entry.path} {entry.symbol or '<file>'}: {entry.category!r}"
        for entry in allowlist.entries
        if entry.category not in CATEGORIES
    ]
    assert unknown == []


def test_scanner_detects_function_local_cjk_collections():
    hits = _scan('''
        def choose(text):
            words: list[str] = ["山门", "灵根"]
            return any(word in text for word in words)
    ''')
    assert _literals(hits, "cjk_collection") == ["山门", "灵根"]
    assert _literals(_scan('def choose(): return ["山门"]'), "cjk_collection") == ["山门"]


def test_scanner_detects_cjk_startswith_tuple():
    hits = _scan('''
        def choose(text):
            return text.startswith(("宗门", "掌门"))
    ''')
    assert _literals(hits, "cjk_membership") == ["宗门", "掌门"]


def test_scanner_detects_concatenated_literals():
    hits = _scan('''
        def choose(text):
            words = ["宗" + "门", "灵" "根"]
            return ("秘" + "密") in text
    ''')
    assert _literals(hits, "cjk_collection") == ["宗门", "灵根"]
    assert _literals(hits, "cjk_membership") == ["秘密"]
    assert _literals(_scan('x = "line_" + "story"'), "story_id_literal") == ["line_story"]
