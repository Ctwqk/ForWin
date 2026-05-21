# Legacy Compatibility Audit Design

## Purpose

The 60-chapter review-engine live pilot already proves whether review decisions
fall back to the review safety net. It does not prove whether non-review legacy
compatibility paths are unused. This design adds a separate audit stream for
legacy compatibility usage during the same pilot.

The audit must answer two questions independently:

1. Did the review engine fall back to legacy live decision-making?
2. Which non-review legacy compatibility paths were used during generation?

Review safety-net audit remains in `review_engine_decision` payloads. Legacy
compatibility audit uses separate events.

## Event Model

Add one canonical event type:

```text
legacy_compatibility_used
```

Each event records only runtime facts. It must not decide whether a path is safe
to delete.

Required payload fields:

```text
compat_layer
compat_feature
usage_kind
source_module
usage_reason
```

Optional payload fields:

```text
compat_key
legacy_identifier
canonical_identifier
related_stage
metadata
```

`delete_candidate` and `blocking_for_removal` are intentionally not event fields.
Those are audit conclusions produced after all events for a pilot run are
summarized.

## Compatibility Registry

The audit layer owns a registry of known compatibility features. Each entry
defines how to assess usage after the run:

```text
compat_feature
compat_layer
default_assessment
description
```

Allowed `default_assessment` values:

```text
candidate_if_unused
must_migrate_if_used
keep_for_import_only
out_of_scope
```

The registry makes deletion decisions centralized and auditable. Call sites only
report facts.

## First Instrumentation Scope

Cover generation-adjacent legacy compatibility points first:

- `book_state.runtime`: legacy `state.location` read fallback.
- `book_state.reviewer`: legacy `state.location` patch warning downgrade.
- `orchestrator_loop_core.finalization` / `world_projection`: legacy world
  projection compatibility path.
- `subworld_manager`: `legacy_entity_id` bridge and legacy entity creation.
- `governance`: `legacy_relaxed` fallback.
- `api_governance_support`: legacy checkpoint status normalization.
- Existing legacy import/projection events remain, but the new event provides a
  unified summary surface.

Out of scope for this pass:

- Deleting BookState import/projection compatibility.
- Deleting API compatibility fields.
- Changing project data schemas.

## Audit Script

Extend the existing review cutover audit script:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60 \
  --include-legacy-compat
```

The existing review cutover pass/fail remains strict:

- all expected chapters have engine live decisions;
- no legacy review safety-net fallback;
- no severe review-engine mismatch.

Legacy compatibility usage is summarized separately:

```json
{
  "legacy_compat": {
    "total_events": 0,
    "by_layer": {},
    "by_feature": {},
    "removal_assessment": {
      "delete_candidates": [],
      "blocking_for_removal": [],
      "keep_for_import_only": [],
      "out_of_scope": []
    }
  }
}
```

Assessment rules:

- `candidate_if_unused`: appears in `delete_candidates` only when no events were
  observed for that feature.
- `must_migrate_if_used`: appears in `blocking_for_removal` when any event was
  observed.
- `keep_for_import_only`: appears separately; usage during generation is a
  signal to inspect, but not automatic deletion approval.
- `out_of_scope`: reported for visibility only.

## Testing

Add unit tests for:

- event payload construction records facts only;
- summary groups compatibility events by layer and feature;
- unused `candidate_if_unused` registry entries become delete candidates;
- used `must_migrate_if_used` entries block removal;
- `audit_review_engine_cutover.py --include-legacy-compat` includes the new
  summary without weakening the existing review safety-net pass/fail logic.

## Success Criteria

After implementation, a 60-chapter pilot can produce both:

1. a strict review safety-net deletion signal;
2. a separate legacy compatibility usage report that classifies features as
   candidates, blockers, import-only, or out of scope.

No single runtime event claims that a feature is deletable. Deletion assessment
is made only by the audit summary.
