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
        "note = 'legacy repair scope RepairLoopDetector'\n",
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


def test_deleted_entry_residual_fails_when_symbol_reappears_in_other_covered_path(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "current.py").write_text(
        "note = 'legacy repair scope RepairLoopDetector'\n",
        encoding="utf-8",
    )
    inventory = root / "legacy-inventory.yaml"
    _write_inventory(
        inventory,
        """
  - id: current.repair_scope_name
    category: rename_only
    owner_area: reviewer
    paths:
      - forwin/current.py
    allow_patterns:
      - legacy repair scope
    reason: Synthetic current path with misleading naming.
    removal_phase: phase_y
    verification:
      - unit
    delete_when:
      - renamed
    status: planned

  - id: deleted.repair_loop_detector
    category: deleted
    owner_area: reviewer
    paths:
      - forwin/deleted.py
    allow_patterns:
      - RepairLoopDetector
    reason: Deleted repair-loop detector must not reappear anywhere.
    removal_phase: complete
    verification:
      - architecture
    delete_when:
      - already deleted
    status: deleted
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["deleted_residual"]
    assert result.issues[0].entry_id == "deleted.repair_loop_detector"
    assert result.issues[0].path == "forwin/current.py"
    assert "RepairLoopDetector" in result.issues[0].line


def test_deleted_entry_residual_fails_when_deleted_symbol_has_no_legacy_token(tmp_path: Path) -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    root = tmp_path
    (root / "forwin").mkdir()
    (root / "forwin" / "current.py").write_text(
        "class RepairLoopDetector:\n"
        "    pass\n",
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
      - forwin/deleted.py
    allow_patterns:
      - RepairLoopDetector
    reason: Deleted repair-loop detector must not reappear anywhere.
    removal_phase: complete
    verification:
      - architecture
    delete_when:
      - already deleted
    status: deleted
""",
    )

    result = audit_inventory(root=root, inventory_path=inventory)

    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["deleted_residual", "uncovered"]
    assert result.issues[0].entry_id == "deleted.repair_loop_detector"
    assert result.issues[0].path == "forwin/current.py"
    assert result.issues[0].line == "class RepairLoopDetector:"


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
