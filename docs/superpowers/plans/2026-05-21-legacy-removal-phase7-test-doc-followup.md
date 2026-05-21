# Legacy Removal Phase 7 Test And Doc Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> or superpowers:subagent-driven-development to implement this plan task by
> task.

**Goal:** Remove misleading legacy naming from the current world-model
projection/export facade and close the Phase 7 inventory entry.

**Spec:** `docs/superpowers/specs/2026-05-21-legacy-removal-phase7-test-doc-followup-design.md`

---

## Task 1: Remove Current-Path Legacy Wording

**Files:**

- `forwin/world_model/__init__.py`
- `forwin/world_model/compiler.py`
- `forwin/world_model/README.md`

- [ ] Replace production docstrings/messages that call the current projection
      facade legacy.
- [ ] Keep the deprecation warning: `forwin.world_model` is deprecated as a
      business dependency and BookState is canon.

## Task 2: Delete `legacy_entity_id` Frontmatter Support

**Files:**

- `forwin/world_model/page_repository.py`
- tests covering page identity/import behavior

- [ ] Add or update a test proving `legacy_entity_id` frontmatter is ignored.
- [ ] Remove the `legacy_entity_id` branch from `_source_from_frontmatter()`.
- [ ] Confirm `node_id` and `forwin_id` identity resolution still works.

## Task 3: Inventory

**Files:**

- `docs/designs/legacy-inventory.yaml`

- [ ] Mark `projection.world_model_current_helpers` as:
      `category: deleted`, `removal_phase: complete`, `status: deleted`.
- [ ] Narrow its residual patterns so deleted residuals only match the removed
      Phase 7 strings.

## Task 4: Verification And Commit

- [ ] Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns
python3 -m pytest tests/test_architecture_boundaries.py -q
python3 -m pytest tests/test_world_model_repository.py tests/test_obsidian_importer.py -q
python3 -m compileall -q forwin
git diff --check
```

- [ ] If a named test file does not exist, run the closest actual test files
      and note the substitution.
- [ ] Commit only Phase 7 files:

```bash
git commit -m "refactor: remove world projection legacy labels"
```
