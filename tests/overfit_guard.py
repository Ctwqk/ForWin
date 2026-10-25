"""AST scanner that keeps specific story content out of ForWin runtime code.

Design: docs/designs/2026-10-20-world-engine-overhaul.md §7.2. Runtime code may only
handle structure; story content comes from Genesis, world_seed, BookState or LLM
output. Every current hit must be classified in ``tests/overfit_allowlist.toml``.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PHASE_ORDER = ("P0", "P1", "P2", "P3", "P4")
CATEGORIES = (
    "language_lexicon",
    "genre_lexicon",
    "catalog",
    "prompt_text",
    "numeric_policy",
    "eval_fixture",
    "pending_removal",
)
CHECKS = (
    "story_id_literal",
    "chapter_number_literal",
    "premise_sniff",
    "cjk_collection",
    "cjk_regex",
    "cjk_membership",
    "cjk_inline_iteration",
    "fixture_term",
)
ALLOWLIST_RELATIVE_PATH = Path("tests") / "overfit_allowlist.toml"
FIXTURE_TERM_SYMBOL = "<file>"
MODULE_SYMBOL = "<module>"

_CJK = re.compile(r"[㐀-鿿]")
_HARVEST_TERM = re.compile(r"[㐀-鿿]{2,6}")
_STORY_ID_PREFIXES = r"(?:line|gap|world_line|hidden_line|fact|thread|intent|agenda)_"
_STORY_ID = re.compile(_STORY_ID_PREFIXES + r"[a-z0-9_]+")
_STORY_ID_PREFIX = re.compile(_STORY_ID_PREFIXES)
# world_line_id=, gap_id= and compounds such as related_gap_id= / primary_world_line_id=
_STORY_ID_KEYWORD = re.compile(r"(?:[a-z0-9]+_)*" + _STORY_ID_PREFIXES + r"id")
_LEADING_ID_TOKEN = re.compile(r"[a-z0-9_]+")
_CHAPTER_NAME = re.compile(
    r"chapter(?:_number|_start|_end|_hint)?|current_chapter|target_chapter"
    r"|deadline_chapter|latest_chapter"
)
_PREMISE_NAME = re.compile(r".*(?:premise|all_text|synopsis|setting|title|genre).*")
_KEY_ACCESSORS = frozenset({"get", "pop"})
_NAME_ACCESSORS = frozenset({"getattr", "setattr", "hasattr"})
_REGEX_FUNCS = frozenset(
    {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "split"}
)
_COLLECTIONS = (ast.List, ast.Tuple, ast.Set, ast.Dict)
_MEMBERSHIP_OPS = (ast.In, ast.NotIn)


@dataclass(frozen=True)
class Hit:
    check: str
    path: str
    symbol: str
    line: int
    literal: str


# --- single-file scan ---------------------------------------------------------


def scan_source(text: str, path: str, *, allowed_ids: Collection[str] = ()) -> list[Hit]:
    """Run every AST check on one file. ``fixture_term`` needs repo data; see scan_runtime."""
    scanner = _Scanner(path, frozenset(allowed_ids))
    scanner.visit(_FoldStringLiterals().visit(ast.parse(text, filename=path)))
    return scanner.hits


class _FoldStringLiterals(ast.NodeTransformer):
    """Expose explicit literal concatenation to the same checks as adjacent literals."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Add) and _is_str(node.left) and _is_str(node.right):
            return ast.copy_location(ast.Constant(value=node.left.value + node.right.value), node)
        return node


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_cjk_str(node: ast.AST) -> bool:
    return _is_str(node) and bool(_CJK.search(node.value))


def _cjk_units(node: ast.AST) -> Iterator[ast.expr]:
    """CJK string literals under ``node``; an f-string counts as one literal."""
    if isinstance(node, ast.JoinedStr):
        if any(_is_cjk_str(part) for part in node.values):
            yield node
        return
    if _is_cjk_str(node):
        yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from _cjk_units(child)


def _collection_units(node: ast.AST, *, inside: bool = False) -> Iterator[ast.expr]:
    """CJK literals nested in a list/tuple/set/dict display, plus set("…")/frozenset("…")."""
    if isinstance(node, (ast.JoinedStr, ast.Constant)):
        if inside:
            yield from _cjk_units(node)
        return
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"set", "frozenset"}
        and len(node.args) == 1
        and _is_cjk_str(node.args[0])
    ):
        yield node.args[0]
        return
    inside = inside or isinstance(node, _COLLECTIONS)
    for child in ast.iter_child_nodes(node):
        yield from _collection_units(child, inside=inside)


def _literal_text(node: ast.expr) -> str:
    return node.value if _is_str(node) else ast.unparse(node)


def _ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _sniffed_name(node: ast.AST) -> str | None:
    """Name of the inspected text, looking through method calls such as ``text.lower()``."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        node = node.func.value
    return _ref_name(node)


def _is_int(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, allowed_ids: frozenset[str]) -> None:
        self.path = path
        self.allowed_ids = allowed_ids
        self.hits: list[Hit] = []
        self._scope: list[str] = []
        self._function_depth = 0
        self._assign_target: str | None = None
        self._story_exempt: set[int] = set()
        self._cjk_claimed: set[int] = set()

    # scope tracking

    def _symbol(self) -> str:
        if self._function_depth:
            return ".".join(self._scope)
        parts = [*self._scope, *([self._assign_target] if self._assign_target else [])]
        return ".".join(parts) or MODULE_SYMBOL

    def _hit(self, check: str, node: ast.AST, literal: str) -> None:
        self.hits.append(Hit(check, self.path, self._symbol(), node.lineno, literal))

    def _cjk_hits(self, check: str, units: Iterable[ast.expr]) -> None:
        for unit in units:
            if id(unit) in self._cjk_claimed:
                continue
            self._cjk_claimed.add(id(unit))
            self._hit(check, unit, _literal_text(unit))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        self._visit_assignment(node, node.targets[0], node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_assignment(node, node.target, node.value)

    def _visit_assignment(self, node: ast.stmt, target: ast.expr, value: ast.expr | None) -> None:
        self._assign_target = target.id if isinstance(target, ast.Name) else ast.unparse(target)
        # Children first: regex/membership checks claim their literals before the
        # collection check counts what is left.
        self.generic_visit(node)
        if value is not None:
            self._cjk_hits("cjk_collection", _collection_units(value))
        self._assign_target = None

    # story_id_literal

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            isinstance(node.value, str)
            and id(node) not in self._story_exempt
            and node.value not in self.allowed_ids
            and _STORY_ID.fullmatch(node.value)
        ):
            self._hit("story_id_literal", node, node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        first = node.values[0] if node.values else None
        if (
            first is not None
            and _is_str(first)
            and _STORY_ID_PREFIX.match(first.value)
            and _LEADING_ID_TOKEN.match(first.value).group() not in self.allowed_ids
        ):
            self._hit("story_id_literal", node, ast.unparse(node))
        self._story_exempt.update(id(part) for part in node.values)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        self._story_exempt.update(id(key) for key in node.keys if key is not None)
        self._visit_collection(node)

    def _visit_collection(self, node: ast.expr) -> None:
        self.generic_visit(node)
        if self._function_depth:
            self._cjk_hits("cjk_collection", _collection_units(node))

    visit_List = _visit_collection
    visit_Tuple = _visit_collection
    visit_Set = _visit_collection

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._story_exempt.add(id(node.slice))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _KEY_ACCESSORS and node.args:
            self._story_exempt.add(id(node.args[0]))
        if isinstance(func, ast.Name) and func.id in _NAME_ACCESSORS and len(node.args) >= 2:
            self._story_exempt.add(id(node.args[1]))
        for keyword in node.keywords:
            self._check_keyword(keyword)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
            and func.attr in _REGEX_FUNCS
            and node.args
        ):
            pattern = node.args[0]
            units = list(_cjk_units(pattern))
            if units:
                self._cjk_claimed.update(id(unit) for unit in units)
                self._hit("cjk_regex", pattern, _literal_text(pattern))
        if isinstance(func, ast.Attribute) and func.attr in {"startswith", "endswith"} and node.args:
            self._cjk_hits("cjk_membership", _cjk_units(node.args[0]))
        if self._function_depth and isinstance(func, ast.Name) and func.id in {"any", "all"}:
            for arg in node.args:
                if isinstance(arg, _COLLECTIONS):
                    self._cjk_hits("cjk_inline_iteration", _cjk_units(arg))
        self.generic_visit(node)

    def _check_keyword(self, keyword: ast.keyword) -> None:
        name, value = keyword.arg, keyword.value
        if name is None:
            return
        if name == "chapter_hint" and _is_int(value):
            self._hit("chapter_number_literal", value, str(value.value))
        if (
            _STORY_ID_KEYWORD.fullmatch(name)
            and _is_str(value)
            and value.value
            and value.value not in self.allowed_ids
        ):
            self._story_exempt.add(id(value))
            self._hit("story_id_literal", value, value.value)

    # comparisons: chapter numbers, premise sniffing, must_not_reveal, CJK membership

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        if any(
            (name := _ref_name(operand)) is not None and _CHAPTER_NAME.fullmatch(name)
            for operand in operands
        ):
            for operand in operands:
                if _is_int(operand) and operand.value >= 2:
                    self._hit("chapter_number_literal", operand, str(operand.value))
        for left, op, right in zip(operands, node.ops, node.comparators):
            if isinstance(op, _MEMBERSHIP_OPS):
                self._check_membership(left, right)
        self.generic_visit(node)

    def _check_membership(self, left: ast.expr, right: ast.expr) -> None:
        if _is_str(left):
            sniffed = _sniffed_name(right)
            if sniffed is not None and _PREMISE_NAME.fullmatch(sniffed):
                self._hit("premise_sniff", left, left.value)
            if _ref_name(right) == "must_not_reveal" and id(left) not in self._story_exempt:
                self._story_exempt.add(id(left))
                self._hit("story_id_literal", left, left.value)
        if _is_cjk_str(left):
            self._cjk_hits("cjk_membership", [left])
        if isinstance(right, _COLLECTIONS):
            self._cjk_hits("cjk_membership", _cjk_units(right))

    # inline iteration inside functions

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        if self._function_depth and isinstance(node.iter, _COLLECTIONS):
            self._cjk_hits("cjk_inline_iteration", _cjk_units(node.iter))
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if self._function_depth and isinstance(node.iter, _COLLECTIONS):
            self._cjk_hits("cjk_inline_iteration", _cjk_units(node.iter))
        self.generic_visit(node)


# --- repository scan ----------------------------------------------------------


def runtime_files(repo_root: Path) -> list[Path]:
    versions = repo_root / "forwin" / "migrations" / "versions"
    return sorted(
        path
        for path in (repo_root / "forwin").rglob("*.py")
        if "__pycache__" not in path.parts and versions not in path.parents
    )


def canary_terms(repo_root: Path, canary: CanaryConfig) -> frozenset[str]:
    """Seed blacklist plus CJK names harvested from test fixtures, minus generic words."""
    harvested: set[str] = set()
    keys = set(canary.harvest_keys)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and _HARVEST_TERM.fullmatch(item.strip()):
                    harvested.add(item.strip())
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for fixture in sorted((repo_root / "tests" / "fixtures").rglob("*.json")):
        walk(json.loads(fixture.read_text(encoding="utf-8")))
    return frozenset((set(canary.seed_terms) | harvested) - set(canary.ignore))


def scan_fixture_terms(text: str, path: str, terms: Collection[str]) -> list[Hit]:
    ordered = sorted(terms)
    return [
        Hit("fixture_term", path, FIXTURE_TERM_SYMBOL, line_number, term)
        for line_number, line in enumerate(text.splitlines(), start=1)
        for term in ordered
        if term in line
    ]


def scan_runtime(repo_root: Path, *, allowlist: Allowlist | None = None) -> list[Hit]:
    """Scan ``forwin/**/*.py`` (minus migrations/versions) with every check."""
    if allowlist is None:
        allowlist = load_allowlist(repo_root / ALLOWLIST_RELATIVE_PATH)
    terms = canary_terms(repo_root, allowlist.canary)
    hits: list[Hit] = []
    for file in runtime_files(repo_root):
        relative = file.relative_to(repo_root).as_posix()
        text = file.read_text(encoding="utf-8")
        hits.extend(scan_source(text, relative, allowed_ids=allowlist.allowed_ids))
        hits.extend(scan_fixture_terms(text, relative, terms))
    return hits


# --- allowlist ----------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    path: str
    symbol: str
    checks: tuple[str, ...]
    category: str
    justification: str
    max_literals: int
    remove_in: str = ""
    literals: tuple[str, ...] = ()

    def matches(self, hit: Hit) -> bool:
        return (
            hit.path == self.path
            and hit.check in self.checks
            and (not self.symbol or self.symbol == hit.symbol)
            and (not self.literals or hit.literal in self.literals)
        )

    def budget_exceeded(self, count: int) -> bool:
        """pending_removal budgets are exact, so every trim forces an entry update."""
        if self.category == "pending_removal":
            return count != self.max_literals
        return count > self.max_literals


@dataclass(frozen=True)
class CanaryConfig:
    seed_terms: tuple[str, ...]
    harvest_keys: tuple[str, ...]
    ignore: tuple[str, ...]


@dataclass(frozen=True)
class Allowlist:
    completed_phase: str
    allowed_ids: frozenset[str]
    canary: CanaryConfig
    entries: tuple[Entry, ...]

    def _assign(self, hits: Iterable[Hit]) -> tuple[list[list[Hit]], list[Hit]]:
        """Give each hit to its most specific entry: symbol before file-level, and an entry
        restricted by ``literals`` before an unrestricted one."""
        by_path: dict[str, list[int]] = {}
        for index, entry in enumerate(self.entries):
            by_path.setdefault(entry.path, []).append(index)
        for indexes in by_path.values():
            indexes.sort(
                key=lambda index: (not self.entries[index].symbol, not self.entries[index].literals)
            )
        assigned: list[list[Hit]] = [[] for _ in self.entries]
        unallowed: list[Hit] = []
        for hit in hits:
            index = next(
                (i for i in by_path.get(hit.path, ()) if self.entries[i].matches(hit)), None
            )
            if index is None:
                unallowed.append(hit)
            else:
                assigned[index].append(hit)
        return assigned, unallowed

    def unallowed(self, hits: Iterable[Hit]) -> list[Hit]:
        return self._assign(hits)[1]

    def stale_entries(self, hits: Iterable[Hit]) -> list[Entry]:
        assigned, _ = self._assign(hits)
        return [entry for entry, matched in zip(self.entries, assigned) if not matched]

    def over_budget(self, hits: Iterable[Hit]) -> list[tuple[Entry, int]]:
        assigned, _ = self._assign(hits)
        return [
            (entry, len(matched))
            for entry, matched in zip(self.entries, assigned)
            if entry.budget_exceeded(len(matched))
        ]


_ENTRY_REQUIRED = {"path", "checks", "category", "justification", "max_literals"}
_ENTRY_OPTIONAL = {"symbol", "remove_in", "literals"}


def _load_entry(raw: dict[str, object]) -> Entry:
    label = f"{raw.get('path')} {raw.get('symbol', '')}".strip()
    missing = _ENTRY_REQUIRED - raw.keys()
    unknown = raw.keys() - _ENTRY_REQUIRED - _ENTRY_OPTIONAL
    if missing or unknown:
        raise ValueError(
            f"allowlist entry {label}: missing {sorted(missing)}, unknown {sorted(unknown)}"
        )
    checks = tuple(raw["checks"])
    if not checks or set(checks) - set(CHECKS):
        raise ValueError(f"allowlist entry {label}: invalid checks {list(checks)}")
    if not str(raw["justification"]).strip():
        raise ValueError(f"allowlist entry {label}: empty justification")
    literals = raw.get("literals", [])
    if "literals" in raw and (
        not isinstance(literals, list)
        or not literals
        or not all(isinstance(item, str) and item for item in literals)
    ):
        raise ValueError(f"allowlist entry {label}: literals must be a non-empty list of strings")
    return Entry(
        path=str(raw["path"]),
        symbol=str(raw.get("symbol", "")),
        checks=checks,
        category=str(raw["category"]),
        justification=str(raw["justification"]),
        max_literals=int(raw["max_literals"]),
        remove_in=str(raw.get("remove_in", "")),
        literals=tuple(literals),
    )


def load_allowlist(path: Path) -> Allowlist:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    canary = data.get("canary", {})
    return Allowlist(
        completed_phase=str(data.get("meta", {}).get("completed_phase", "")),
        allowed_ids=frozenset(data.get("story_id_literal", {}).get("allowed_values", [])),
        canary=CanaryConfig(
            seed_terms=tuple(canary.get("seed_terms", [])),
            harvest_keys=tuple(canary.get("harvest_keys", [])),
            ignore=tuple(canary.get("ignore", [])),
        ),
        entries=tuple(_load_entry(raw) for raw in data.get("entry", [])),
    )
