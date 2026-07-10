# ForWin v5 Pipeline Ownership Implementation Plan

**Goal:** Remove `ChapterPipeline` cross-module method assignment, give each pipeline stage a real code owner, type every constructor collaborator, and stop review/repair from depending on the complete pipeline object.

**Architecture:** Pipeline stage modules expose concrete owner classes whose methods are defined in those classes. `ChapterPipeline` statically composes the owner classes and contains only task-scoped state plus typed domain collaborators. Pure calculations remain module functions. Repair receives a narrow typed execution contract rather than `ChapterPipeline`.

## Task 1: Lock the boundary

- [x] Add AST guards rejecting class-body method assignment in `ChapterPipeline`.
- [x] Reject `Any` annotations in the `ChapterPipeline` constructor.
- [x] Reject `ChapterPipeline` imports and `runtime=ChapterPipeline` in repair ownership.
- [x] Confirm the new guards fail against the current implementation.

## Task 2: Establish real stage owners

- [x] Convert run control, governance, runtime helpers, review workflow, repair patches, gate delegation, chapter execution, writer execution, quality diagnostics, post-Canon work, finalization, and manual acceptance into owner classes.
- [x] Preserve pure functions as pure functions and remove inappropriate module-level `staticmethod`/`classmethod` descriptors.
- [x] Make `ChapterPipeline` inherit the static owner classes and delete all 90 imported function assignments.
- [x] Add explicit `__all__` exports for owner classes only where they are package boundaries.

## Task 3: Type composition and narrow repair

- [x] Replace every constructor `Any` collaborator with its concrete type or an owner-local protocol.
- [x] Replace the repair service's full-pipeline runtime argument with a narrow typed execution contract.
- [x] Construct the contract once per pipeline and pass only review/repair capabilities.
- [x] Remove stale pipeline imports and duplicate collaborator fields.

## Task 4: Verify and document

- [x] Run architecture, runtime-container, review/repair, writer fallback, gate, and Canon-flow focused tests.
- [x] Run compileall, import/lint checks, and full test collection.
- [x] Update architecture status and audit D20 evidence.
- [x] Commit the ownership convergence as one coherent slice.
