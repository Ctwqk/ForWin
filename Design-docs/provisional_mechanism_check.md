# Provisional Boundary After V5

Status: Provisional Band Preview runtime removed.

## Removed Runtime

The v5 hard cut physically removed the unreachable Provisional Band Preview
feature family:

- `PlanningPolicy.provisional_preview` and every enablement path;
- the second runtime writer and preview service;
- preview execution, fallback, and repair callbacks;
- band execution and chapter ledger models/tables;
- gate events, artifacts, payloads, HTTP routes, task history, and UI stages;
- the standalone preview probe.

There is no compatibility switch and no migration path for the removed
runtime. Fresh v5 schema contains neither preview table.

## Preserved Names

Several similarly named structures have different ownership and remain:

- `provisional_window` and `provisional_band_size` are Arc sizing names for the
  near-term `ChapterPlan` window passed into envelope analysis. They do not
  generate prose.
- `ProvisionalPromotionRecord` records which accepted chapter plans informed an
  ArcEnvelope analysis. It is not a preview execution ledger.
- `WorldProjectionDeltaRow.projection_layer="provisional_projection"` describes
  a projection lifecycle, not the deleted writer feature.
- `ChapterWriter.write_preview_chapter()` is the normal writer failure fallback
  and routes through `writer_preview`. It does not run a second band pipeline.

## Current Gates

The current writing path is:

```text
Arc/ChapterPlan
-> Writer
-> immutable CandidateDraftRecord
-> Candidate Draft Review / Repair
-> CanonPreparationService
-> CanonAdmissionService.commit_plan
-> BookState
```

Arc/ChapterPlan planning services own pre-writing plan construction. The Writer
produces an immutable candidate; Candidate Draft Review / Repair and
`CanonPreparationService` establish post-writing eligibility before
`CanonAdmissionService.commit_plan` performs the only accepted-state commit to
BookState. No preview artifact can block or authorize canon writing.
