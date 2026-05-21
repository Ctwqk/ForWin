# Legacy Compatibility Audit Correction Design

## Context

The first legacy compatibility audit records runtime usage facts, but its summary can still produce unsafe removal conclusions. The two main failure modes are uninstrumented registry entries being treated as unused and runtime-only windows missing rare but reachable paths.

## Design

The audit is split into three layers:

1. Runtime events record facts only. A `legacy_compatibility_used` event can say what compatibility path was used, where, and why. It must not contain `delete_candidate` or `blocking_for_removal`.
2. Registry metadata describes each compatibility path. Each entry declares `removal_mode`, `instrumentation_status`, `compat_layer`, and static search patterns.
3. The summary derives removal verdicts from runtime events plus static reachability counts.

The summary must classify every registry feature into an explicit bucket. No feature may disappear just because it has zero runtime events.

## Removal Modes

- `candidate_if_unused`: removable only when runtime events are zero and static callers are zero.
- `must_migrate_if_used`: blocks removal when runtime events exist; if static callers exist but runtime is zero, the verdict is targeted testing, not deletion.
- `keep_for_import_only`: retained for explicit import/API contracts until that contract is removed separately.
- `out_of_scope`: visible in the report but excluded from removal decisions.

## Static + Runtime Verdict Matrix

| Static callers | Runtime events | Verdict |
|---:|---:|---|
| 0 | 0 | delete candidate only for removable modes |
| >0 | 0 | static-only; expand audit window or write targeted tests |
| >0 | >0 | live path; blocks removal until migrated or intentionally retained |
| 0 | >0 | anomalous runtime use; investigate before deleting |

Uninstrumented features never produce delete candidates. They go to `uninstrumented_no_delete_signal` until they have runtime instrumentation or are explicitly static-only/out-of-scope.

## Instrumentation Scope

The correction adds or preserves instrumentation for:

- BookState `state.location` fallback through a callback/observer, not direct database writes from runtime code.
- Legacy checkpoint status normalization at the API support layer with idempotent audit events.
- Project `creation_status="legacy"` compatibility as a registry/static audit feature.
- Character creation that still creates legacy `Entity` rows.

The review-engine live cutover safety-net audit remains separate. `legacy_safety_net_used`, shadow mismatch severity, and engine live status are not legacy compatibility registry features.

## Deletion Scope

Only confirmed static-dead compatibility code is removed in this correction:

- The old `ChapterRepairCoordinator` module can be deleted if it has no runtime imports.
- The unused `use_legacy_fallback` parameter and current-book fallback profiles can be deleted because no caller passes the flag.

`LegacyBookStateImporter` is not deleted in this correction because the API route still calls it. It remains `keep_for_import_only` until the import API contract is removed deliberately.
