# Legacy Removal Phase 4B Metadata Source Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove runtime reads and writes of SubWorld `legacy_source` metadata.

**Architecture:** Replace the old provenance key with current `region_source` at both read and write boundaries. Then mark the inventory entry deleted and run the static gate.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, legacy inventory audit.

---

## Scope

Implements `docs/superpowers/specs/2026-05-21-legacy-removal-phase4b-metadata-source-fields-design.md`.

Do not rename review-engine legacy audit terms, Genesis facade aliases, or external API/config compatibility in this phase.

## File Structure

- `forwin/state/repo.py`: read persisted `MapRegionRow.metadata_json["region_source"]`, not `legacy_source`.
- `forwin/subworld_manager.py`: write `region_source` when persisting region seeds.
- `docs/designs/legacy-inventory.yaml`: mark `metadata.legacy_source_fields` deleted and narrow allow patterns.
- `tests/test_subworld_control.py`: add regression coverage for persisted map region source metadata.

## Task 1: Write Failing Tests

- [ ] **Step 1: Add StateRepository read assertion**

In `tests/test_subworld_control.py`, add or update a test that creates an
active SubWorld and a persisted `MapRegionRow` with:

```python
metadata_json=json.dumps({"region_source": "runtime_generated"}, ensure_ascii=False)
```

Then call:

```python
drafts = StateRepository(session).get_active_subworld_region_drafts(project.id, 1)
```

Assert:

```python
self.assertEqual(drafts[0]["region_source"], "runtime_generated")
```

Before implementation this fails because the repository reads `legacy_source`
and returns `"map_regions"`.

- [ ] **Step 2: Add SubWorldManager write assertion**

In the existing re-arc/subworld region seed test, query the persisted
`MapRegionRow` for the new subworld and assert its metadata contains
`region_source == "runtime_generated"` and does not contain `legacy_source`.

- [ ] **Step 3: Run red tests**

Run:

```bash
python3 -m pytest tests/test_subworld_control.py -q
```

Expected before implementation: at least one failure showing
`"map_regions" != "runtime_generated"` or missing `region_source`.

## Task 2: Update Runtime Metadata

- [ ] **Step 1: Update `forwin/state/repo.py`**

Change the persisted map region payload source line to:

```python
"region_source": metadata.get("region_source", "map_regions"),
```

- [ ] **Step 2: Update `forwin/subworld_manager.py`**

Change `_persist_region_seeds()` metadata construction to:

```python
metadata={
    **payload,
    "region_source": "runtime_generated",
}
```

Do not write `legacy_source`.

- [ ] **Step 3: Run green tests**

Run:

```bash
python3 -m pytest tests/test_subworld_control.py -q
```

Expected: pass.

## Task 3: Update Inventory

- [ ] **Step 1: Mark entry deleted**

In `docs/designs/legacy-inventory.yaml`, change
`metadata.legacy_source_fields` to:

```yaml
category: deleted
removal_phase: complete
status: deleted
```

Remove `region_source` from `allow_patterns` because it is current.

- [ ] **Step 2: Run audit and residual grep**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
git grep -n -E 'legacy_source|_record_subworld_legacy_compatibility|legacy_identifier|legacy entity id|legacy entity row' -- forwin/state/repo.py forwin/subworld_manager.py
```

Expected: audit passes and grep returns no hits.

## Task 4: Verification And Commit

- [ ] **Step 1: Run final verification**

Run:

```bash
python3 -m pytest tests/test_subworld_control.py tests/test_architecture_boundaries.py -q
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
python3 -m compileall -q forwin
git diff --check
```

- [ ] **Step 2: Commit Phase 4B only**

Do not stage unrelated dirty LLM/minimax files.

```bash
git add docs/superpowers/specs/2026-05-21-legacy-removal-phase4b-metadata-source-fields-design.md \
  docs/superpowers/plans/2026-05-21-legacy-removal-phase4b-metadata-source-fields.md \
  docs/designs/legacy-inventory.yaml \
  forwin/state/repo.py \
  forwin/subworld_manager.py \
  tests/test_subworld_control.py
git commit -m "refactor: remove legacy metadata source fields"
```
