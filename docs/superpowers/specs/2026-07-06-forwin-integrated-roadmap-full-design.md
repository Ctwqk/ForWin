# ForWin Integrated Roadmap Full Design

## Goal

Implement the full `forwin_integrated_roadmap_phase0.md` roadmap, not only the
Phase 0 unblocker. The target runtime is a 100-chapter-capable ForWin pipeline
that can use real semantic retrieval, keep pulp-mode BookState facts alive,
plan arcs from canon feedback, distinguish fatal canon blockers from soft
self-healable issues, rotate trope execution constraints, and expose operator
actions without database spelunking.

User approval for spec and plan details is pre-granted for this execution.

## Current Baseline

Phase 0 is already implemented and verified:

- Repair-exhausted chapters receive structured final acceptance decisions.
- Final acceptance no longer falls through to `no_rule_matched`.
- Subworld admission failures route through explicit register, genericize, or
  manual-review actions.
- Repair prompts carry word-budget metadata.
- The Phase 0 code has passed the full test suite, deployed through the 150 sync
  path, and completed a 100-chapter real-machine verification run.

## Requirements By Phase

### Phase 1: Retrieval And Band Scheduler

- Keep `hash` embedding only as an explicit fallback or test backend.
- Provide a production semantic embedding backend that can be selected from
  runtime config and reindex existing chapter memories safely.
- Add `scripts/reembed_memory_index.py` for collection rebuild or side-by-side
  migration.
- Fix duplicate mid-band `micro_progress_power` reward scheduling when
  `boost_reward_density` is active.
- Preserve rollback by using configurable Qdrant collection names.

### Phase P: Pulp BookState Commit Readiness

- Pulp mode cannot be promoted live unless accepted chapters create at least a
  light world-layer BookState delta.
- `writer_mode=single` may defer expensive structured extraction, but acceptance
  must either consume a deferred extraction artifact or run a lightweight
  state/event extraction pass.
- Minimum accepted facts include roles, events, states, possessions, factions or
  organizations, and key event summaries when present in the chapter.
- Live rollout should support a shadow mode before enforcing the gate.

### Phase 2: Arc Activation From Canon Feedback

- Arc activation planning must receive an `ArcActivationReviewPack` containing
  accepted chapter summaries, protagonist and important character state,
  open obligations, BookState, map and faction snapshots, recent director
  imbalance, and audience signals.
- `_plan_arc_chapters` must inject that pack into the planning prompt and use a
  larger token budget.
- LLM planning failure must produce an explicit degraded or needs-review trace
  instead of silently presenting template fallback as normal planning.
- DecisionEvents must record the canon facts used for arc activation.

### Phase 3: Canon Admission Profile And Auto-Continue Healing

- Add middle-profile fatal admission modes for pulp and serial production.
- Blocking signals include dead-character resurrection, level rollback, duplicate
  artifact or resource, faction relation reversal, protagonist resource debt
  mismatch, and impossible location teleport.
- Narrative obligations use tiers: P0 blocks, while P1/P2 warn and create
  obligations where appropriate.
- Auto-continue may self-heal soft `needs_review` chapters, but must not force
  hard canon, admission, or active-rule failures.
- `no_rule_matched` must never be a generation stop reason.

### Phase 4: Trope Library And Fanqie Defaults

- Maintain a trope selector with at least 50 usable trope templates and platform
  fit metadata.
- Default pulp/fanqie tuning should prefer visible payoff density, controlled
  ambiguity, power/status movement, and low setup cost.
- Prompt injection must provide concrete execution constraints, not only trope
  keywords.
- Repetition control must avoid running the same sub-trope three times in a
  20-chapter window.

### Phase 5: Operator View And Documentation

- The home UI must expose needs-review and repair-exhausted queues, stop reason
  distribution, auto-continue chain health, and DecisionEvent context.
- One-click operator actions should cover retry, accept-soft, register subworld
  entity, genericize background reference, and create obligation.
- `Design-docs/CURRENT_ARCHITECTURE.md` and `Design-docs/DESIGN_STATUS.md` must
  describe the live roadmap state instead of leaving operators to grep code.

## Design Boundaries

- Project, task, chapter, and generation truth remains behind ForWin MCP tools.
- BookState DB Canon remains the source of canon truth. Legacy world-model paths
  must stay compatibility/projection-only.
- Pulp performance improvements must not bypass hard canon blockers.
- Deployment remains source-first from `/Users/magi1/ForWin-source-github` and
  then through the 150 sync path into the local deployment target.

## Testing Strategy

- Use focused red-green tests for each implementation slice.
- Keep deterministic unit tests for schedulers, config, admission profiles,
  arc-pack construction, and UI payload builders.
- Run the full pytest suite before deployment.
- After deployment, use ForWin MCP tools to verify the selected 100-chapter
  project has no active generation task, no pending review chapters, and accepted
  chapters through 100.
