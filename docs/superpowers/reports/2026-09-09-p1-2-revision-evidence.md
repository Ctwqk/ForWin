# P1-2 historical revision implementation and verification

This report records the implementation evidence for the approved [three-stage design](../specs/2026-09-09-forwin-three-stage-design.md). It is not a Stage 1 smoke/L100 result, a production rollout approval, or a claim about live-model semantic accuracy.

## Entry and ownership

The existing review retry request accepts optional `replacement_body`, `replacement_title`, and `expected_book_revision`. A supplied body creates a distinct candidate and draft with its own title/hash and a server-bound base book revision. It does not overwrite the active draft, copy old extraction metadata, or demote accepted state. A bare accepted retry only records the request. Responses expose the saved candidate and actual book revision; the existing approve entry performs full-suffix validation. Blocked historical approval retains the accepted status and cannot trigger continuation as if a new revision succeeded.

`RevisionValidationService` captures the complete active prefix and affected suffix under Project → chapter locks, then releases the live transaction. `CandidateReplica` uses a disposable private database. It restores a proven prefix and invokes the existing extraction, entity admission, deterministic review, historical form, BookState review and projection owners for each complete affected body. It never invokes a second Canon acceptance owner. Unchanged successor prose reuses its existing draft but receives a new context-bound acceptance identity.

The sole live acceptance path remains `CanonAdmissionService.commit_plan`, which dispatches a validated historical plan to its atomic revision branch. It checks durable evidence, prepares derived rows outside the live transaction, then reacquires Project → chapter locks and checks the entire base again. All active pointers, new Canon records, snapshots, deltas, quality evidence, obligation journal branches, outbox events and one book-revision increment commit together. Failure rolls back the whole transaction. Old Canon records, graph deltas/patches, state evidence and snapshots remain intact. Current materialized graph rows absent from the new revision are removed; they are not immutable history.

## Frozen identity and supported provenance

The durable result binds every affected body hash/title/draft, all prefix and suffix active acceptance IDs, stable chapter identities, plan revisions, base book revision, policy and actual extraction project inputs (including title/genre), publisher binding snapshot, and a fingerprint of participating source collections. It includes acceptance-bound entity/obligation before-images and exact accepted quality runs. An unrelated new review or audit append does not invalidate a prepared result.

Model identity includes allowed configured provider/model routes and a route fingerprint, without credentials. Every actual transport call records its provider/model, stage, attempt group, requested temperature/token budget, timeout and available usage/duration fields. The real routed Codex provider is `codex_bridge`. Chat/JSON wrappers preserve explicit signatures so timeouts, task family and stage routing survive instrumentation. Unavailable, unallowed or truncated transport evidence produces unknown. Successful semantic validation may only use complete current-body spans and frozen prefix references.

Supported historical prefix provenance requires:

- A contiguous accepted chapter sequence from chapter 1, with valid active Canon ownership and exact surviving accepted bodies. An accepted tail with a missing pointer is an explicit durable capture unknown, not an omitted chapter.
- Surviving accepted delta/snapshot manifests and reversible BookState patches. Old deltas may reconstruct the prefix; they never count as passing validation of the candidate or successors.
- Exact acceptance-bound EntityAdmission before/after images for touched entity, alias and roster rows, including explicit absence. Existing aliases and roster effects from the replaced suffix cannot leak into the prefix.
- Immutable Canon quality evidence for each accepted prefix chapter, bound to the exact admission run or verified historical form. Ordinary old diagnostic rows may still be readable, but cannot establish strict historical provenance.
- Proven NarrativeObligation lifecycle chains. Prefix restoration preserves active debts, and installation appends a new acceptance-bound branch (including a restore-only branch when required), preserving all prior journal events.

A changed chapter that originally activated a NarrativeObligation is currently unsupported without a grounded new-body disposition (retained/removed/fulfilled). The existing form does not provide that disposition; even wording-only edits in this case produce unknown before model validation and are refused again during final materialization. The original obligation row, journal and active chapter remain unchanged. Existing prefix obligations and unchanged-draft successors continue through the proven lifecycle owner. No body similarity or old draft metadata is used to infer disposition.

Positive-chapter WorldNodeState evidence must reference a surviving delta at the same chapter with a matching node create/state patch. A blank, missing or unrelated Genesis delta ID is not provenance. Existing unproven positive rows remain unknown; forward character creation supplies its explicitly trusted GraphDelta source rather than backfilling history.

Missing or ambiguous history remains unknown. In particular, historical rows without these proofs, unversioned narrative updates, unproven reader-promise changes, unowned suffix deltas, model/input budget exhaustion, partial responses and missing critical coverage cannot be accepted. No repair fabricates historical evidence. There is no legacy-manual or replay-only exemption.

Projection mutation owners share the Project-first lock. The cache is bound to the actual root/savepoint transaction and cleared at its end, including savepoint rollback. Final verification rejects a concurrent source edit completed after capture; an edit waiting behind the final transaction applies afterward. The current-state reader and graph-delta reader use the same active Canon ownership to exclude superseded state evidence. Explicit old acceptance manifests and immutable deltas/snapshots remain inspectable.

## Scenario coverage and limits

`tests/test_revision_full_suffix.py` uses real PostgreSQL, real ChapterWriter extraction parsers/prompts, the existing review and form owners, scratch projection, and final Canon admission. Only external model responses are frozen fixtures. Its positive case validates two complete bodies (three extraction calls and one extended form call per chapter), switches both acceptance IDs atomically, reuses the unchanged successor draft, and preserves original commit evidence. The quiet archive fixture has no character/obligation transitions; it must not be reported as a realistic long-book model evaluation.

| Design scenario | Concrete evidence | What this establishes |
|---|---|---|
| Lost key followed by opening a locked door | `test_revision_semantic_scenarios.py`, possession case: explicitly lost/unrecovered key in prefix, exact later usage body; full-owner generic possession-failure control in `test_revision_full_suffix.py` | Grounded form failure survives orchestration and cannot accept. The frozen model response is human-labelled, not a measured model inference. |
| Secret known before acquisition | Same scenario test, knowledge case with no information source | Exact body/prefix binding and coverage failure contract. |
| Dead character acts | Same scenario test, life-state case with no resurrection/flashback | Exact body/prefix binding and coverage failure contract; separate tracked form tests prevent unknown/absent life-state bypasses. |
| Time conflict | Same scenario test, day 3 18:00 → day 3 08:00 without time mechanism | Exact body/prefix binding and coverage failure contract. |
| Place conflict | Same scenario test, north city → distant south port immediately without transit | Exact body/prefix binding and coverage failure contract. |
| Due obligation | Same scenario test, locked gate remains closed at chapter deadline; historical tracked-form and obligation-history tests | Due debt cannot be waived by a generic absence answer; lifecycle prefix rewind/branch installation preserves evidence. |
| Wording-only revision | Full owner two-chapter positive test and `test_revision_concurrency.py`; explicit origin-promise negative fixture in `test_revision_acceptance.py` | Supported quiet-body edits re-extract/review every chapter and preserve history; edits to an obligation's originating chapter remain unknown until grounded disposition support exists. |
| Multi-platform or unknown publication | P1-1 publication fence tests; P1-2 final `published` race uses another platform | Any affected protected platform freezes the whole proposed suffix; final checks run again after model work. |
| Concurrent later-chapter revision | `test_revision_concurrency.py`: two real proposals frozen at revision 3; chapter 2 accepts first; chapter 1's full-suffix result is rejected | No partial switch or lost accepted later revision; book revision advances once for the winner. |
| Missing coverage and downstream errors | Independent review timeout/known-fail tests; entity timeout/omitted identity tests | Unknown is not converted to a semantic failure, and an already grounded failure is not lost to a downstream exception. |
| Non-Canon writer race | Projection lock, savepoint rollback, source edit after materialization and personality writer tests | Project serialization plus freshness checks prevent overwritten writes. |

The six semantic scenario fixtures are independent labelled input/output pairs at the form boundary, rather than six live-model capability measurements. Finite model quality and the fresh offline smoke/L100 remain separate roadmap work.

## Historical-test migration map

The obsolete negative-chapter archival/replay entry and dead failed-historical reapproval callback have been removed. Initial acceptance tests retain their original semantics. The following map preserves the properties of every old historical transaction test while refusing the superseded authorization route.

Short names below refer to functions in `tests/test_canon_atomic_transaction.py` unless a file is named:

- **Replay refusal:** `test_replay_only_historical_plan_cannot_authorize_replacement`.
- **Marker refusal:** `test_retry_marker_alone_never_authorizes_accepted_rewrite` (missing/invalid/consumed/ambiguous/backfill variants).
- **Artifact refusal:** `test_cached_artifact_and_old_manual_approval_cannot_bypass_revision_proposal` (failed/reviewed/ready × absent/stale/old-valid artifact).
- **World retention:** `test_rejected_old_replay_preserves_independent_world_edits` (with/without cognition).
- **Manifest refusal:** `test_invalid_old_delta_manifest_cannot_become_revision_evidence` (nine malformed/missing variants).
- **Fresh atomic path:** `test_revision_atomic_failure_restores_whole_acceptance_set` in `test_revision_full_suffix.py`, five injected failure stages and complete captured source-row equality.
- **Fresh history:** full-suffix wording positive, replica alias/roster before-image tests, the origin-obligation final-owner refusal test, and repeated revision/old-manifest reconstruction in `test_revision_concurrency.py`.

| Old historical test suffix (`test_` omitted) | Replacement assertions / retained property |
|---|---|
| historical_rewrite_replaces_old_delta_and_replays_later_accepted_deltas | Replay refusal + fresh history: new deltas/acceptances; original evidence is retained instead of replaced. |
| failed_historical_recovery_verifies_full_artifact_for_absent_fingerprint | Artifact refusal; fresh proposal identity/whole-body preparation tests. |
| failed_historical_artifact_recovery_rejects_without_writing | Artifact refusal + complete mainline equality. |
| failed_historical_artifact_backfill_rolls_back_with_review_transaction | Artifact refusal; fresh atomic path verifies rollback of every authoritative row. |
| failed_historical_artifact_path_binding_rejects_without_writing | Artifact refusal; full-suffix body/identity drift checks. |
| failed_historical_normal_plan_can_enter_manual_review | Artifact refusal; existing approve owner accepts only a distinct real proposal via complete review. |
| failed_historical_recovery_requires_manual_api_entry | Artifact refusal; real retry/approve API tests. |
| failed_historical_recovery_rejects_nonexplicit_plan_modes | Artifact refusal; no cached-plan authorization. |
| failed_historical_candidate_can_be_reapproved_after_atomic_rollback | Fresh atomic path and fresh successful owner path; old callback remains refused. |
| failed_historical_reapproval_reruns_gates_without_mutating_marker | Marker/artifact refusal; full-suffix positive proves all body checks run again. |
| failed_historical_reapproval_rejects_stale_authorization | Artifact refusal + base/policy/body/active identity race tests. |
| failed_historical_reapproval_rejects_equal_marker_timestamp_without_mutation | Marker refusal; timestamps no longer grant acceptance authority. |
| historical_rewrite_rejects_planned_candidate_without_retry_marker | Marker refusal; real proposal required. |
| historical_rewrite_rolls_back_when_replacement_commit_fails | Fresh atomic path (five stages, full captured rows). |
| historical_rewrite_backfills_only_an_unambiguous_accepted_api_retry | Marker refusal; no retroactive evidence backfill; explicit proposal API positive. |
| historical_rewrite_does_not_repair_an_invalid_marker | Marker refusal. |
| historical_rewrite_rejects_invalid_consumption_marker | Marker refusal. |
| historical_rewrite_ignores_only_well_formed_consumed_markers | Marker refusal; markers cannot authorize any replacement. |
| historical_rewrite_replays_successor_manifest_order_not_delta_id_order | Replay refusal; fresh suffix validates and commits strictly in chapter sequence, validation identity binds fresh evidence. |
| historical_rewrite_applies_retained_first_cognition_patch_once | World retention with cognition; ordinary projection cognition-once regression and immutable acceptance-specific overlays. |
| historical_rewrite_restores_verified_node_metadata_and_replays_atomically | Fresh replica exact alias/roster/metadata recovery + atomic rollback; old replay refused. |
| historical_rewrite_preserves_same_chapter_standalone_world_edit | World retention + source-edit-after-materialization refusal, preserving actual edit. |
| historical_rewrite_rejects_invalid_commit_delta_manifest_without_mutation | Manifest refusal + missing-evidence replica tests. |

## Verification record

Initial P1-2 tests were written against missing functionality and failed before implementation. Independent review subsequently reproduced and fixed: signal truncation by raw-row count, unknown/fail reclassification, lost chat timeout/stage arguments, late duplicate validation demoting an accepted candidate, savepoint lock-cache bypass, unfrozen project title/genre, and accepted-tail omission. The repeated-revision test additionally exposed stale current graph/state selection; its corrected path is required for final verification.

Forward migration `0004_revision_validation` adds durable validation and quality evidence storage. Existing acceptance rows remain unchanged; old quality runs start with explicitly empty/unknown provenance. The migration test upgrades an isolated PostgreSQL fixture and refuses evidence-destroying downgrade. No deployed baseline or production database was rewritten for this work.

Fresh owner verification after the source freeze:

```text
.venv/bin/python -m pytest -q tests/test_revision*.py tests/test_canon_atomic_transaction.py tests/test_stable_chapter_revisions.py tests/test_canon_quality_acceptance_evidence.py tests/test_obligation_acceptance_history.py tests/test_canon_repair_stage.py tests/test_large_module_boundaries.py
211 passed in 53.37s

.venv/bin/python -m pytest -q tests/test_revision_concurrency.py tests/test_book_state_repository_projection_compiler.py tests/test_book_state_runtime.py tests/test_map_cognition_path.py
60 passed in 10.99s

.venv/bin/python -m pytest -q tests/test_revision_model_transport.py tests/test_llm_router.py tests/test_revision_concurrency.py tests/test_revision_entity_input.py
26 passed in 3.41s
```

The second batch includes existing explicit-as-of and cognition-once behavior. The repeated-revision fixture also reads original deltas in each old acceptance's manifest order and verifies their state values against the preserved old snapshot state index. It does not introduce an API for arbitrary old acceptance-version graph reconstruction. Current graph materialization tables have no incoming model foreign keys (`rg` of world/map/narrative node/edge `.id` targets under `forwin/models`); immutable snapshot/state evidence uses stable identifiers independently. Entity compatibility rows retain their existing inactive treatment.

The later origin-obligation audit added a controlled fail-closed boundary, and independent state tests added exact chapter/node provenance checks and forward character-emitter attribution. The complete 211-test batch above includes these final boundaries and supersedes the earlier 176-test owner run.

Full-rule Ruff for new revision modules, the finite application revision helper, and `test_revision*` is clean. Scoped `I,F` for the touched existing acceptance/entity/atomic files, compilation of the new Canon modules, and `git diff --check` pass. A late additional actual-router assertion caught and fixed the existing trace default that incorrectly reported requested temperature 0.0 as 0.85; the transport itself was unchanged. An intermediate Ruff autofix removed two imported fixture bindings, causing four collection errors; explicit module fixture bindings were restored before the complete 176-test green run.

These are owner and compatibility batches, not the complete project suite. The parent delivery record owns final full-suite, backup/restore and runtime evidence.
