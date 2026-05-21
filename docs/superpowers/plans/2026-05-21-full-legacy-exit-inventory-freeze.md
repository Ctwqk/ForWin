# Full Legacy Exit Inventory Freeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 0 inventory audit gate so production legacy references are registered before any deletion work continues.

**Architecture:** Add a small importable audit script that loads `docs/designs/legacy-inventory.yaml`, scans production files for legacy tokens, and reports uncovered paths, deleted-entry residuals, migration-scope violations, and final-mode active debt. Wire the script into architecture tests so CI blocks unregistered legacy references.

**Tech Stack:** Python 3.12, PyYAML, pytest, existing ForWin architecture test suite.

---

## Scope

This plan implements only Phase 0 from `docs/designs/legacy-removal-spec.md`.
It does not delete runtime legacy code. Follow-up plans should handle identity,
world projection, creation status/location, rename-only, and external
compatibility deletion as separate workstreams.

## File Structure

- `pyproject.toml`
  - Add `PyYAML>=6.0` because the inventory source of truth is YAML and the
    audit script runs in CI/developer environments.
- `scripts/audit_legacy_inventory.py`
  - Owns YAML loading, production scanning, issue classification, text/JSON
    reporting, and CLI exit codes.
- `tests/test_legacy_inventory.py`
  - Unit tests for the audit engine against temporary repos and synthetic
    inventories.
- `tests/test_architecture_boundaries.py`
  - Adds the real-repo architecture gate that requires the current inventory to
    cover all production legacy references.
- `docs/designs/legacy-inventory.yaml`
  - Already exists. Only update if the new audit finds a true coverage gap.

## Task 1: Add Failing Audit Engine Tests

**Files:**
- Create: `tests/test_legacy_inventory.py`

- [ ] **Step 1: Create tests for path coverage, deleted residuals, final mode, and CLI output**

Add this complete file:

```python
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def _write_inventory(path: Path, entries: str) -> None:
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "policy:",
                "  scan_roots:",
                "    - forwin",
                "  excluded_roots:",
                "    - tests",
                "  token_patterns:",
                "    - legacy",
                "    - Legacy",
                "    - LEGACY",
                "  included_extensions:",
                "    - .py",
                "  final_allowed_categories:",
                "    - migration_history_keep",
                "    - test_doc_followup",
                "    - deleted",
                "categories:",
                "  runtime_delete: Runtime old-project compatibility.",
                "  rename_only: Rename current production behavior.",
                "  external_compat_delete: Remove external compatibility.",
                "  migration_history_keep: Historical migration content.",
                "  test_doc_followup: Test or documentation follow-up.",
                "  deleted: Already deleted.",
                "entries:",
                entries.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_audit_reports_uncovered_production_legacy_path(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "current.py").write_text('VALUE = "legacy"\n', encoding="utf-8")
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(inventory, "")

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["uncovered"]
    assert result.issues[0].path == "forwin/current.py"
    assert "VALUE = \"legacy\"" in result.issues[0].line


def test_audit_accepts_registered_path(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "registered.py").write_text('VALUE = "legacy"\n', encoding="utf-8")
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(
        inventory,
        """
  - id: sample.registered
    category: runtime_delete
    owner_area: sample
    paths:
      - forwin/registered.py
    allow_patterns:
      - legacy
    reason: Registered synthetic legacy reference.
    removal_phase: phase_x
    verification:
      - unit
    delete_when:
      - synthetic condition
    status: planned
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is True
    assert result.hit_count == 1
    assert result.issues == []


def test_deleted_entry_residual_fails_only_when_deleted_pattern_matches(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "repair.py").write_text(
        "note = 'legacy repair scope'\n"
        "symbol = 'RepairLoopDetector'\n",
        encoding="utf-8",
    )
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(
        inventory,
        """
  - id: deleted.repair_loop_detector
    category: deleted
    owner_area: reviewer
    paths:
      - forwin/repair.py
    allow_patterns:
      - RepairLoopDetector
    reason: Deleted repair-loop detector must not reappear.
    removal_phase: complete
    verification:
      - architecture
    delete_when:
      - already deleted
    status: deleted

  - id: current.repair_scope_name
    category: rename_only
    owner_area: reviewer
    paths:
      - forwin/repair.py
    allow_patterns:
      - legacy repair scope
    reason: Synthetic current path with misleading naming.
    removal_phase: phase_y
    verification:
      - unit
    delete_when:
      - renamed
    status: planned
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["deleted_residual"]
    assert result.issues[0].entry_id == "deleted.repair_loop_detector"
    assert "RepairLoopDetector" in result.issues[0].line


def test_migration_history_entry_is_restricted_to_its_paths(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin" / "migrations" / "versions").mkdir(parents=True)
    (root / "forwin" / "runtime").mkdir()
    (root / "forwin" / "migrations" / "versions" / "0001.py").write_text(
        'revision = "legacy_revision"\n',
        encoding="utf-8",
    )
    (root / "forwin" / "runtime" / "bad.py").write_text(
        'revision = "legacy_revision"\n',
        encoding="utf-8",
    )
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(
        inventory,
        """
  - id: migration.history
    category: migration_history_keep
    owner_area: database
    paths:
      - forwin/migrations/versions
    allow_patterns:
      - legacy
    reason: Historical migration content.
    removal_phase: keep
    verification:
      - inventory_check
    delete_when:
      - never
    status: retained
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["uncovered"]
    assert result.issues[0].path == "forwin/runtime/bad.py"


def test_final_mode_fails_active_delete_categories_even_without_hits(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(
        inventory,
        """
  - id: sample.active
    category: runtime_delete
    owner_area: sample
    paths:
      - forwin/missing.py
    allow_patterns:
      - legacy
    reason: Active debt should block final mode.
    removal_phase: phase_x
    verification:
      - unit
    delete_when:
      - removed
    status: planned
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory, final=True)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["final_active_entry"]
    assert result.issues[0].entry_id == "sample.active"


def test_cli_returns_nonzero_for_uncovered_hit(tmp_path: Path) -> None:
    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "current.py").write_text('VALUE = "legacy"\n', encoding="utf-8")
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(inventory, "")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/audit_legacy_inventory.py",
            "--root",
            str(root),
            "--inventory",
            str(inventory),
            "--check",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "uncovered" in result.stdout
    assert "forwin/current.py" in result.stdout
```

- [ ] **Step 2: Run the tests and confirm they fail because the script does not exist**

Run:

```bash
python3 -m pytest tests/test_legacy_inventory.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.audit_legacy_inventory'` or CLI failure because `scripts/audit_legacy_inventory.py` is absent.

## Task 2: Implement The Inventory Audit Script

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/audit_legacy_inventory.py`
- Test: `tests/test_legacy_inventory.py`

- [ ] **Step 1: Add PyYAML as a project dependency**

Modify `pyproject.toml` dependencies to include:

```toml
    "PyYAML>=6.0",
```

Place it near the other runtime utility dependencies, for example after
`"pydantic>=2.9",`.

- [ ] **Step 2: Create the audit script**

Create `scripts/audit_legacy_inventory.py` with this complete implementation:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import fnmatch
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import yaml


DEFAULT_INVENTORY = Path("docs/designs/legacy-inventory.yaml")


@dataclass(frozen=True)
class InventoryEntry:
    id: str
    category: str
    owner_area: str
    paths: tuple[str, ...]
    allow_patterns: tuple[str, ...]
    status: str
    removal_phase: str


@dataclass(frozen=True)
class LegacyHit:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class AuditIssue:
    kind: str
    path: str = ""
    line_number: int = 0
    line: str = ""
    entry_id: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line_number": self.line_number,
            "line": self.line,
            "entry_id": self.entry_id,
            "message": self.message,
        }


@dataclass
class AuditResult:
    hit_count: int
    entry_count: int
    issues: list[AuditIssue] = field(default_factory=list)
    hits_by_entry: dict[str, int] = field(default_factory=dict)
    warnings: list[AuditIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "hit_count": self.hit_count,
            "entry_count": self.entry_count,
            "hits_by_entry": dict(sorted(self.hits_by_entry.items())),
            "issues": [issue.as_dict() for issue in self.issues],
            "warnings": [warning.as_dict() for warning in self.warnings],
        }

    def to_text(self) -> str:
        lines = [
            f"legacy inventory audit: {'PASS' if self.ok else 'FAIL'}",
            f"entries: {self.entry_count}",
            f"legacy hits: {self.hit_count}",
            f"issues: {len(self.issues)}",
            f"warnings: {len(self.warnings)}",
        ]
        if self.hits_by_entry:
            lines.append("hits by entry:")
            for entry_id, count in sorted(self.hits_by_entry.items()):
                lines.append(f"  {entry_id}: {count}")
        if self.issues:
            lines.append("issues:")
            for issue in self.issues:
                location = f"{issue.path}:{issue.line_number}" if issue.path else issue.entry_id
                detail = issue.message or issue.line
                lines.append(f"  [{issue.kind}] {location} {detail}".rstrip())
        if self.warnings:
            lines.append("warnings:")
            for warning in self.warnings:
                location = f"{warning.path}:{warning.line_number}" if warning.path else warning.entry_id
                detail = warning.message or warning.line
                lines.append(f"  [{warning.kind}] {location} {detail}".rstrip())
        return "\n".join(lines)


def load_inventory(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Inventory file not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Inventory root must be a mapping.")
    if not isinstance(raw.get("policy"), dict):
        raise ValueError("Inventory must define a policy mapping.")
    if raw.get("entries") is None:
        raw["entries"] = []
    if not isinstance(raw.get("entries"), list):
        raise ValueError("Inventory must define an entries list.")
    return raw


def parse_entries(raw_entries: Iterable[object]) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    seen: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("Each inventory entry must be a mapping.")
        entry_id = _required_text(raw_entry, "id")
        if entry_id in seen:
            raise ValueError(f"Duplicate inventory entry id: {entry_id}")
        seen.add(entry_id)
        entries.append(
            InventoryEntry(
                id=entry_id,
                category=_required_text(raw_entry, "category"),
                owner_area=_required_text(raw_entry, "owner_area"),
                paths=tuple(_string_list(raw_entry, "paths")),
                allow_patterns=tuple(_string_list(raw_entry, "allow_patterns")),
                status=_required_text(raw_entry, "status"),
                removal_phase=_required_text(raw_entry, "removal_phase"),
            )
        )
    return entries


def audit_inventory(
    *,
    root: Path = Path("."),
    inventory_path: Path = DEFAULT_INVENTORY,
    final: bool = False,
    strict_patterns: bool = False,
) -> AuditResult:
    root = root.resolve()
    inventory_path = inventory_path if inventory_path.is_absolute() else root / inventory_path
    inventory = load_inventory(inventory_path)
    policy = inventory["policy"]
    entries = parse_entries(inventory["entries"])
    hits = scan_legacy_hits(root=root, policy=policy)
    issues: list[AuditIssue] = []
    warnings: list[AuditIssue] = []
    hits_by_entry = {entry.id: 0 for entry in entries}

    for hit in hits:
        matching_entries = [entry for entry in entries if _entry_matches_path(entry, hit.path)]
        if not matching_entries:
            issues.append(
                AuditIssue(
                    kind="uncovered",
                    path=hit.path,
                    line_number=hit.line_number,
                    line=hit.line,
                    message="legacy reference is not covered by any inventory path",
                )
            )
            continue
        for entry in matching_entries:
            hits_by_entry[entry.id] += 1
        deleted_matches = [
            entry
            for entry in matching_entries
            if entry.category == "deleted" and _entry_matches_line(entry, hit.line)
        ]
        for entry in deleted_matches:
            issues.append(
                AuditIssue(
                    kind="deleted_residual",
                    path=hit.path,
                    line_number=hit.line_number,
                    line=hit.line,
                    entry_id=entry.id,
                    message="deleted inventory entry still has a production match",
                )
            )
        if strict_patterns and not any(_entry_matches_line(entry, hit.line) for entry in matching_entries):
            warnings.append(
                AuditIssue(
                    kind="pattern_unmatched",
                    path=hit.path,
                    line_number=hit.line_number,
                    line=hit.line,
                    message="path is covered but no allow_pattern matched this line",
                )
            )

    if final:
        allowed = set(_string_list(policy, "final_allowed_categories"))
        for entry in entries:
            if entry.category not in allowed and entry.status != "deleted":
                issues.append(
                    AuditIssue(
                        kind="final_active_entry",
                        entry_id=entry.id,
                        message=f"{entry.category} entry is still {entry.status}",
                    )
                )

    return AuditResult(
        hit_count=len(hits),
        entry_count=len(entries),
        issues=issues,
        hits_by_entry={entry_id: count for entry_id, count in hits_by_entry.items() if count},
        warnings=warnings,
    )


def scan_legacy_hits(*, root: Path, policy: dict[str, Any]) -> list[LegacyHit]:
    scan_roots = _string_list(policy, "scan_roots")
    token_patterns = _string_list(policy, "token_patterns")
    included_extensions = set(_string_list(policy, "included_extensions"))
    excluded_roots = _string_list(policy, "excluded_roots")
    token_re = re.compile("|".join(re.escape(pattern) for pattern in token_patterns))
    hits: list[LegacyHit] = []
    for scan_root in scan_roots:
        absolute_scan_root = root / scan_root
        if not absolute_scan_root.exists():
            continue
        for path in sorted(absolute_scan_root.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root).as_posix()
            if included_extensions and path.suffix not in included_extensions:
                continue
            if _is_excluded(rel_path, excluded_roots):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if token_re.search(line):
                    hits.append(LegacyHit(path=rel_path, line_number=line_number, line=line.strip()))
    return hits


def _entry_matches_path(entry: InventoryEntry, rel_path: str) -> bool:
    normalized = rel_path.strip("/")
    for raw_path in entry.paths:
        entry_path = raw_path.strip("/")
        if normalized == entry_path or normalized.startswith(entry_path + "/"):
            return True
    return False


def _entry_matches_line(entry: InventoryEntry, line: str) -> bool:
    return any(pattern and pattern in line for pattern in entry.allow_patterns)


def _is_excluded(rel_path: str, excluded_roots: Iterable[str]) -> bool:
    normalized = rel_path.strip("/")
    for raw_pattern in excluded_roots:
        pattern = raw_pattern.strip("/")
        if not pattern:
            continue
        if normalized == pattern or normalized.startswith(pattern + "/"):
            return True
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if value is None:
        raise ValueError(f"Missing required inventory key: {key}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"Inventory key {key} must not be empty")
    return text


def _string_list(mapping: dict[str, Any], key: str) -> list[str]:
    value = mapping.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"Inventory key {key} must be a list")
    result = [str(item).strip() for item in value if str(item).strip()]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit production legacy references against the legacy inventory.")
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY), help="Inventory YAML path.")
    parser.add_argument("--check", action="store_true", help="Return non-zero when blocking issues exist.")
    parser.add_argument("--final", action="store_true", help="Require all active removal entries to be deleted.")
    parser.add_argument("--strict-patterns", action="store_true", help="Warn for covered paths whose lines match no allow_pattern.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    result = audit_inventory(
        root=Path(args.root),
        inventory_path=Path(args.inventory),
        final=bool(args.final),
        strict_patterns=bool(args.strict_patterns),
    )
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(result.to_text())
    if args.check and not result.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the unit tests and confirm they pass**

Run:

```bash
python3 -m pytest tests/test_legacy_inventory.py -q
```

Expected: PASS, all tests in `tests/test_legacy_inventory.py` pass.

- [ ] **Step 4: Run the script against the real repository**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check
```

Expected: PASS with output containing:

```text
legacy inventory audit: PASS
issues: 0
```

If this fails with an `uncovered` issue, update `docs/designs/legacy-inventory.yaml` by adding the missing production path to the correct existing entry or by adding a new entry with a specific category and owner. Do not add a broad catch-all entry.

- [ ] **Step 5: Commit the audit engine**

```bash
git add pyproject.toml scripts/audit_legacy_inventory.py tests/test_legacy_inventory.py docs/designs/legacy-inventory.yaml
git commit -m "test: add legacy inventory audit gate"
```

## Task 3: Wire The Audit Into Architecture Tests

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Add the architecture test**

Append this test near the other architecture boundary tests in
`tests/test_architecture_boundaries.py`:

```python
def test_legacy_inventory_covers_current_production_references() -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    result = audit_inventory(
        root=ROOT,
        inventory_path=ROOT / "docs/designs/legacy-inventory.yaml",
        strict_patterns=True,
    )

    assert result.ok, result.to_text()
```

The `strict_patterns=True` flag produces warnings for broad path coverage but
does not fail the test. This gives maintainers visibility while the deletion
work is still active.

- [ ] **Step 2: Run the new architecture test**

Run:

```bash
python3 -m pytest tests/test_architecture_boundaries.py::test_legacy_inventory_covers_current_production_references -q
```

Expected: PASS.

- [ ] **Step 3: Run the architecture and audit tests together**

Run:

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit the architecture test**

```bash
git add tests/test_architecture_boundaries.py
git commit -m "test: enforce legacy inventory coverage"
```

## Task 4: Add Report Verification Commands To The Plan Docs

**Files:**
- Modify: `docs/designs/legacy-removal-spec.md`
- Modify: `docs/designs/legacy-inventory.yaml` only if Task 2 found coverage gaps

- [ ] **Step 1: Confirm the existing Phase 0 acceptance command is accurate**

Open `docs/designs/legacy-removal-spec.md` and confirm Phase 0 includes:

```bash
python3 scripts/audit_legacy_inventory.py --check
python3 -m pytest tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

If it is missing the dedicated unit test file, extend the second command to:

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

- [ ] **Step 2: Run the Phase 0 acceptance commands**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check
python3 -m pytest tests/test_architecture_boundaries.py tests/test_legacy_inventory.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected: both commands exit 0. The audit command prints `legacy inventory audit: PASS`.

- [ ] **Step 3: Commit any doc adjustment**

If the spec command was updated:

```bash
git add docs/designs/legacy-removal-spec.md docs/designs/legacy-inventory.yaml
git commit -m "docs: document legacy inventory acceptance checks"
```

If no doc changes were needed, do not create an empty commit.

## Task 5: Final Phase 0 Verification

**Files:**
- Verify all files changed by Tasks 1-4.

- [ ] **Step 1: Run focused verification**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m pytest tests/test_legacy_inventory.py tests/test_architecture_boundaries.py tests/review_engine/test_legacy_compatibility_audit.py -q
```

Expected:

- First command exits 0.
- Second command exits 0; warnings are acceptable because `strict_patterns` is advisory in Phase 0.
- Pytest exits 0.

- [ ] **Step 2: Run formatting/diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected:

- `git diff --check` exits 0.
- `git status --short` shows no uncommitted files after all commits are made.

- [ ] **Step 3: Record implementation notes for follow-up deletion plans**

Append a short note to the final response listing:

- Total inventory entries.
- Audit hit count from `python3 scripts/audit_legacy_inventory.py --check`.
- Whether `--strict-patterns` produced warnings.
- The next deletion plan to write first: canonical identity (`legacy_entity_id`) or low-risk Phase 1 deletes.

Do not start deleting legacy runtime paths in this Phase 0 implementation branch.

## Self-Review Checklist

- Spec coverage:
  - Phase 0 script creation is covered by Task 2.
  - Architecture test is covered by Task 3.
  - Current inventory validation is covered by Tasks 2 and 5.
  - Long-term final mode is covered by `--final` in Task 2 tests.
  - Deletion work is intentionally deferred to follow-up plans.
- Placeholder scan:
  - No step uses incomplete placeholder language.
  - All code-creating steps include concrete code.
- Type consistency:
  - Tests import `audit_inventory` from `scripts.audit_legacy_inventory`.
  - Script exposes `audit_inventory`, `AuditResult.ok`, and `AuditResult.to_text()`.
  - CLI flags match the spec: `--check`, `--final`, `--strict-patterns`, and `--json`.
