# ForWin Post-100 Chapter Repair Reset Design

## Goal

Plan a destructive post-100-chapter update that turns the ForWin generation
pipeline from a hotfix-driven long-run system into a contract-driven system:
new names are registered, reader-visible corruption blocks acceptance,
fatal gates have real producers, infrastructure degradation is explicit, pulp
BookState extraction records usable facts, trope templates are real content,
and final validation is a no-hotfix 100-chapter run.

User approval for detail choices, spec approval, and implementation-plan
approval is pre-granted for this update.

## Evidence Baseline

- `docs/operations/100-chapter-test-log-2026-07-06.md` records the 100-chapter
  run and the repeated stop/fix pattern.
- Project `d920ac31663743df850d9c5cc3d2df3f` reached 100/100 accepted chapters,
  but did so with manual code hotfixes during the run.
- Recent commits from July 6 to July 7 are dominated by `Normalize`, `Admit`,
  and `Filter` fixes, which confirms the current admission model is too narrow.
- Current code still contains the specific risk surfaces called out in the
  plan: `_apply_subworld_admission_autofix`,
  `_recent_accepted_summary_character_names`, expanded fatal signal names with
  no producer registry, gateway-to-hash embedding fallback, summary-only pulp
  extraction fallback, and generated trope-library variants.

## Scope And Non-Goals

This update may break old projects, old generation tasks, and old generated
data. New verification must start from a new project. Old project data may be
copied into fixtures, but the production database must not be rewritten to
clean old canon.

The update must not add compatibility toggles, old-data migrations, book-specific
alias rules, or special handling for project `d920ac31663743df850d9c5cc3d2df3f`.
If a no-hotfix validation run fails, the fix must be made generically and the
validation must restart with a new project.

## Approaches Considered

### Recommended: Destructive Contract Reset

Replace the admission, readability, fatal-signal, embedding, pulp extraction,
and trope-count contracts in one coordinated update. This removes the old string
genericization path and makes the system record explicit decisions instead of
implicitly accepting or rewriting ambiguous text. The cost is a larger test
rewrite and a bigger deployment, but it directly addresses the recurring
failures from the 100-chapter run.

### Shadow Registrar Overlay

Add an entity registrar in shadow mode while keeping the old whitelist and
autofix behavior. This is safer for old data, but it leaves the polluting string
replacement path active and still requires regex hotfixes during future runs.
It is not acceptable for the stated no-hotfix target.

### Focused Symptom Patch

Patch only the ch13 readability failures and the embedding fallback. This would
reduce a few visible failures quickly, but it leaves the admission root cause,
silent fatal-signal gaps, pulp memory loss, and generated trope content intact.
It would likely pass narrow tests while failing another long run.

## Selected Architecture

The selected design is the destructive contract reset.

Entity admission becomes a registration workflow. The writer's named references
are matched against `Entity` and `EntityAlias`. Unmatched references go through
`EntityRegistrar`, which classifies each name as a new character, an alias of an
existing entity, a background generic reference, or a plan conflict. Registered
decisions are persisted to `Entity` and `EntityAlias`; plan conflicts become
`needs_review`. Failed registrar calls fail closed as plan conflicts.

Reader readability becomes a deterministic canon-quality collector. It emits
fatal signals for appellation referent conflicts, internal key leakage,
missing protagonist names, chapter title mismatches, and empty summaries, plus
a warning for protagonist-name dilution. These signals are evaluated by the same
gate machinery as other canon quality signals.

Fatal gates become auditable. Every fatal signal type must have a producer
registered in `forwin/canon_quality/producer_registry.py`. Deterministic
continuity checks and LLM review-form issue classes both feed the registry, and
architecture tests prevent adding a fatal gate that no module can emit.

Infrastructure degradation becomes explicit. Embedding gateway or remote
embedder construction must either produce an observable degraded event in
non-required mode or fail startup in required mode. Health checks expose the
active embedder kind and dimensions.

Pulp BookState extraction becomes real extraction, not summary-only placeholder
nodes. Single-call pulp acceptance runs a bounded light extraction pass and
feeds discovered entities into the registrar. Extraction failure sends the
chapter to review instead of committing an empty or fake world layer.

The trope library uses hand-authored templates only. Programmatic expansion may
not count toward the 50-template minimum.

## Component Boundaries

- `forwin/naming/entity_registrar.py` owns entity-registration decisions,
  alias registration, persistence, and DecisionEvent records.
- `forwin/orchestrator_loop_core/review_autofix.py` keeps canon-name drift and
  placeholder repair hooks, but no longer owns subworld admission string
  genericization.
- `forwin/state/repo.py` owns allowed entity reads from durable entity tables
  and explicit chapter entry targets only.
- `forwin/checker/reference_classifier.py` keeps generic lexical classifiers,
  not book-specific name rules.
- `forwin/canon_quality/readability.py` owns deterministic reader-readability
  analysis.
- `forwin/canon_quality/producer_registry.py` owns the fatal signal to producer
  contract.
- `forwin/retrieval/memory_index.py` owns embedder construction and required
  fallback behavior.
- `forwin/extractor/book_state_graph_delta.py` and
  `forwin/writer/chapter_writer.py` own light pulp extraction and BookState
  integration.
- `forwin/protocol/trope_library.py` and `Design-docs/trope_library_pulp_v1.md`
  own trope loading and content quality.

## Data Flow

1. A chapter draft is produced.
2. Before review, named references are extracted and passed to
   `EntityRegistrar`.
3. Registered characters and aliases are persisted; background generic names are
   recorded as decisions; plan conflicts move the draft into review.
4. Review runs with the updated entity table as the source of allowed names.
5. Deterministic readability analysis and existing canon-quality collectors emit
   signals.
6. The gate blocks fatal readability and canon signals across all active fatal
   profiles.
7. Accepted pulp chapters run light state extraction before BookState commit.
8. Retrieval indexes accepted chapter memory only with an explicit, observable
   embedder backend.

## Error Handling

- Registrar LLM failure is treated as `plan_conflict`, not as pass-through.
- EntityAlias uniqueness conflicts resolve by reading the existing alias owner
  and recording an alias-conflict DecisionEvent if the target differs.
- Canon-name drift autofix may remain, but every replacement must be recorded as
  a DecisionEvent and then rechecked by readability.
- Readability analyzer failures are implementation bugs and should fail tests;
  runtime collector failures must be surfaced as review errors rather than
  silent acceptance.
- Embedding required mode raises during construction when gateway or remote
  embedding is unavailable.
- Pulp light extraction failure sends the chapter to `needs_review`.
- A failed no-hotfix run is a failed validation run; it cannot be repaired by
  editing code mid-run.

## Testing Strategy

Testing is red-green and fixture-driven.

- Freeze writer outputs from the failed or hotfixed 100-chapter run into
  `tests/fixtures/subworld_replay/` and `tests/fixtures/readability/`.
- Add registrar tests that prove ch6, ch8, ch9, and ch10 alias patterns are
  handled without adding new regex rules.
- Add boundary tests proving subworld string genericization symbols no longer
  exist.
- Add readability tests for polluted ch13, title-mismatched ch28, empty-summary
  ch30, and a clean chapter.
- Add producer-registry architecture tests for every fatal signal type.
- Add embedding tests for `required=true` fail-fast and `required=false`
  explicit degradation.
- Add pulp extraction tests that prove entity and BookState world nodes grow
  across accepted chapters.
- Add trope-loader tests that reject generated template payloads.
- Run focused tests for each task, then `.venv/bin/pytest tests -q`.
- Deploy through the 150 sync path and complete a new 100-chapter no-hotfix
  validation run.

## Acceptance Criteria

- No production path rewrites unknown names into generic role labels in chapter
  body or summary text.
- Unknown named references are resolved by registration, alias binding,
  background-generic decision, or plan conflict.
- The polluted ch13 fixture cannot be accepted under the new gate.
- Every fatal signal type has a registered producer and at least one test that
  proves it can block acceptance.
- Production health reports `gateway` embeddings with 384 dimensions after
  deploy.
- Pulp accepted chapters create real entity and BookState growth.
- The trope library has at least 50 hand-authored templates.
- A new 100-chapter project completes with zero code hotfixes during the run,
  no active task, no pending review, and 100/100 accepted chapters.

## Implementation Plan

The implementation plan is
`docs/superpowers/plans/2026-07-07-forwin-post-100-chapter-repair.md`.
