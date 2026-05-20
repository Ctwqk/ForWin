# ForWin Auto-Continue Generation Design

## Context

ForWin Genesis already produces a book-level plan before writing starts. A 60 chapter book can have a complete `book_blueprint` with multiple arcs, each arc containing planned chapter ranges. The current writing path materializes and runs only the active arc, then leaves the project in a resumable state. This makes normal arc boundaries behave like manual operator gates.

That behavior is wrong for normal book generation. Arc, band, and checkpoint boundaries are audit boundaries. They should not require manual restart unless a real blocker is present.

## Goals

- Starting a book should default to generating through the intended target chapter.
- Arc and band boundaries should record audit decisions and continue automatically when there is no blocker.
- Manual short runs must remain available for debugging, gray rollout, and operator intervention.
- The system must stop on real blockers instead of bypassing review, repair, budget, or user pause semantics.
- Audit logs must make every auto-continue or stop decision explainable after a long run.

## Chapter Target Semantics

`project.target_total_chapters` is the book's total planned chapter count. It is the upper bound represented by the book plan or Genesis blueprint. For a normal 60 chapter book, this value is 60 and the complete book plan should cover chapters 1 through 60.

`run_until_chapter` is the absolute chapter number this run is allowed to auto-continue through. If omitted, it defaults to `project.target_total_chapters`. It must be greater than or equal to the next chapter to generate and less than or equal to `project.target_total_chapters`.

`max_chapters` is a quota for a short run. It limits how many chapters this specific task request should handle. It must not be used as the book's total target. If both `run_until_chapter` and `max_chapters` are supplied, the effective run stops at the earlier of the absolute target and the quota-derived chapter.

## Desired Behavior

`project_start_writing(project_id)` defaults to auto-continuing through `project.target_total_chapters`.

`project_start_writing(project_id, auto_continue=false)` preserves the old single-active-arc behavior for debugging.

`project_start_writing(project_id, run_until_chapter=N)` starts writing and automatically continues until chapter `N`, unless a blocker appears.

`project_continue_generation(project_id)` defaults to continuing through `project.target_total_chapters`.

`project_continue_generation(project_id, run_until_chapter=N)` continues until chapter `N`, unless a blocker appears.

`project_continue_generation(project_id, max_chapters=N)` runs a short bounded batch for debugging or gray rollout.

## Architecture

Introduce a `GenerationAutoContinueController` as a small orchestration layer above the existing continue-generation machinery.

The controller owns these decisions:

- resolve the effective run target from `target_total_chapters`, `run_until_chapter`, and `max_chapters`
- check that no active generation task already exists
- check whether the current project has reached the run target
- check whether a blocker requires stopping
- materialize the next future arc when needed by using the existing continue workset path
- create the next generation task when continuation is allowed
- write an audit event for every continue or stop decision

The controller must not rewrite writer logic, chapter review logic, or `build_continue_generation_workset`. It should reuse existing workset, arc materialization, generation task creation, and review gate behavior.

## Boundary Rules

Normal chapter, band, and arc boundaries continue automatically when all gates are clear.

Chapter boundary:

- if final acceptance passes, continue to the next planned chapter
- if the chapter is `needs_review` or remains `drafted` under a manual acceptance policy, stop
- if repair reaches policy limits, stop

Band boundary:

- record band audit and checkpoint state
- continue if mismatch, obligation, and budget checks are non-blocking
- stop only when the checkpoint produces a blocking verdict or explicit manual review

Arc boundary:

- record arc completion audit
- check arc obligations and arc/book budget
- materialize and activate the next arc if it exists within the run target
- continue unless a blocking audit or manual review requirement exists

Book boundary:

- stop normally when the run reaches `run_until_chapter`
- mark the project complete only when the run reaches `project.target_total_chapters` and no remaining planned chapters exist

## Blocking Conditions

Auto-continue must stop on:

- user-requested pause
- active generation task already present
- `manual_review` requirement
- `needs_review` or `drafted` chapters that require human acceptance
- severe mismatch or blocking review-engine verdict
- repair attempts exhausted for the relevant scope
- arc or book obligation budget blocker
- blocking scenario rehearsal or provisional gate
- task creation failure, LLM/API failure, or runtime exception
- invalid target inputs, including `run_until_chapter > target_total_chapters`
- reaching `run_until_chapter`

## Audit Events

Each auto-continue decision writes a structured event with:

- `event_type`: `auto_continue_decision`
- `project_id`
- `task_id` when available
- `from_chapter`
- `next_chapter`
- `run_until_chapter`
- `target_total_chapters`
- `boundary_type`: `chapter`, `band`, `arc`, or `book`
- `decision`: `continue` or `stop`
- `reason`
- `blocking_event_id` when a blocking event exists
- `workset_reason`
- `requested_chapters`

Expected reasons include:

- `chapter_completed_no_blocker`
- `band_completed_no_blocker`
- `arc_completed_no_blocker`
- `future_arc_materialized`
- `run_until_reached`
- `target_total_reached`
- `pending_review_blocker`
- `manual_review_blocker`
- `repair_attempts_exhausted`
- `obligation_budget_blocker`
- `scenario_rehearsal_blocker`
- `active_task_blocker`
- `runtime_error`

## API And MCP Contract

The HTTP API and MCP tools should expose the same semantics:

- `auto_continue: bool | None`
- `run_until_chapter: int | None`
- `max_chapters: int | None`

Defaults:

- `auto_continue` defaults to true for normal project start and continue operations.
- `run_until_chapter` defaults to `project.target_total_chapters`.
- `max_chapters` defaults to unset.

Compatibility:

- callers that need the old single-batch behavior must pass `auto_continue=false`
- existing `max_chapters` callers keep their short-run behavior
- MCP descriptions should state that normal book generation runs until target unless blocked

## Testing

Unit tests:

- target resolution defaults to `target_total_chapters`
- `run_until_chapter` is validated against `target_total_chapters`
- `max_chapters` limits the effective run without changing book target
- future arc worksets can continue without manual operator calls
- controller stops on pending review, manual review, active task, blocking gate, and user pause
- controller continues across clear chapter, band, and arc boundaries

API and MCP tests:

- `project_start_writing` defaults to auto-continue semantics
- `auto_continue=false` preserves current active-arc-only behavior
- `project_continue_generation` defaults to run until target
- `run_until_chapter` and `max_chapters` do not conflict or change project target

End-to-end regression:

- a 60 chapter blueprint should require one start request and then proceed until chapter 60 or a real blocker
- the run should produce audit events showing every continue or stop decision
- normal arc boundaries must not require manual restart

## Out Of Scope

- rewriting the writer model route
- changing review verdict semantics
- removing manual review gates
- changing chapter plan generation quality
- changing publication or publisher automation
