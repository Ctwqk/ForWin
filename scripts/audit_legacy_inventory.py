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
    deleted_entries = [entry for entry in entries if entry.category == "deleted"]
    deleted_residual_patterns = [
        pattern
        for entry in deleted_entries
        for pattern in entry.allow_patterns
    ]
    hits = scan_legacy_hits(root=root, policy=policy, extra_token_patterns=deleted_residual_patterns)
    issues: list[AuditIssue] = []
    warnings: list[AuditIssue] = []
    hits_by_entry = {entry.id: 0 for entry in entries}
    deleted_issue_keys: set[tuple[str, str, int]] = set()

    for hit in hits:
        for entry in deleted_entries:
            if not _entry_matches_line(entry, hit.line):
                continue
            issue_key = (entry.id, hit.path, hit.line_number)
            if issue_key in deleted_issue_keys:
                continue
            deleted_issue_keys.add(issue_key)
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
        if strict_patterns and not any(_entry_matches_line(entry, hit.line) for entry in matching_entries):
            issues.append(
                AuditIssue(
                    kind="pattern_unmatched",
                    path=hit.path,
                    line_number=hit.line_number,
                    line=hit.line,
                    message=f"path is covered but no allow_pattern matched this line: {hit.line}",
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


def scan_legacy_hits(
    *,
    root: Path,
    policy: dict[str, Any],
    extra_token_patterns: Iterable[str] = (),
) -> list[LegacyHit]:
    scan_roots = _string_list(policy, "scan_roots")
    token_patterns = _string_list(policy, "token_patterns") + [
        pattern.strip() for pattern in extra_token_patterns if pattern.strip()
    ]
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
    return [str(item).strip() for item in value if str(item).strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit production legacy references against the legacy inventory.")
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY), help="Inventory YAML path.")
    parser.add_argument("--check", action="store_true", help="Return non-zero when blocking issues exist.")
    parser.add_argument("--final", action="store_true", help="Require all active removal entries to be deleted.")
    parser.add_argument(
        "--strict-patterns",
        action="store_true",
        help="Fail covered paths whose lines match no allow_pattern.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    try:
        result = audit_inventory(
            root=Path(args.root),
            inventory_path=Path(args.inventory),
            final=bool(args.final),
            strict_patterns=bool(args.strict_patterns),
        )
    except ValueError as exc:
        print(f"legacy inventory audit: ERROR\n{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(result.to_text())
    if args.check and not result.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
