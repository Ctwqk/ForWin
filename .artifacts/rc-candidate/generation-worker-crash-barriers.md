# Generation and Projection Recovery Runner

`generation_projection_recovery.py` is the only Task 4 execution path. It
creates one generic one-chapter fixture per invocation and supports exactly:

- `generation_worker_precommit_crash`
- `generation_worker_postcommit_crash`
- `qdrant_unavailable`
- `projection_consumer_unavailable`

## CLI

```bash
python .artifacts/rc-candidate/generation_projection_recovery.py run \
  --fault-kind KIND \
  --fault-id ID \
  --candidate-manifest PATH \
  --mcp-url URL \
  --api-url URL \
  --database-url-env NAME \
  --evidence-dir PATH
```

`NAME` identifies the environment variable containing the PostgreSQL URL. For
`qdrant_unavailable`, also set `FORWIN_RECOVERY_QDRANT_URL`. The collection is
derived from the exact candidate's effective
`FORWIN_LLM_KB_QDRANT_COLLECTION`; a separate recovery override is rejected.
The evidence directory must be new or empty. Every invocation uses a new fault
ID and evidence directory;
`recovery_stack.py fresh-up` creates the new run ID and database volume.

## Supported Lifecycle

The runner uses the parameterized MCP endpoint in this order:

1. `project_create` with `target_total_chapters=1`.
2. `project_get`.
3. For `brief`, `world`, `map`, `story_engine`, `book_blueprint`, and
   `bootstrap`: `genesis_get`, `genesis_stage_generate`, `genesis_get`,
   `genesis_stage_lock`.
4. `genesis_get` and `project_get` to confirm writing readiness.
5. `project_get` and `task_active_generation_check`.
6. `project_start_writing` with `auto_continue=false` and `max_chapters=1`.

There is no refine call after generic project creation. The runner never
writes a business table or changes story/chapter content to force a boundary.
PostgreSQL and Qdrant evidence access after handoff is read-only, except for
the temporary advisory barrier objects described below.

## Generation Faults

Both generation faults install fault-ID-derived SQL identifiers and bind the
project ID, chapter number, and advisory key as SQL parameters. The trigger
looks up the key from the scoped table and calls `pg_advisory_xact_lock`.

After the writing handoff, the runner allows up to 900 seconds for the generic
pipeline to create its candidate row. This is a readiness boundary, not part of
the advisory-lock timeout: model-backed drafting can legitimately spend most
of that time before any transaction can reach the Canon trigger. Only after
the candidate identity is visible does a separate 900-second exact
blocked-waiter timer begin; reviewer and Canon preparation are also
model-backed and cannot share the candidate-readiness clock. If the generation
task enters a terminal status before either
required boundary, the runner fails closed immediately as `setup_blocked`
instead of waiting out a timer or changing fixture content.

| Fault kind | Trigger boundary | Additional scope |
| --- | --- | --- |
| `generation_worker_precommit_crash` | `BEFORE INSERT ON canon_commit_records` | exact project and chapter |
| `generation_worker_postcommit_crash` | `BEFORE INSERT ON post_canon_maintenance_runs` | exact project, chapter, and `NEW.step_name = 'planning'` |

The recovery Compose overlay sets distinct database session identities:

```text
generation-worker: forwin-recovery-generation-worker
outbox-worker:      forwin-recovery-outbox-worker
```

Each is encoded in that service's `FORWIN_DATABASE_URL` `application_name`
query parameter. The advisory-lock holder sets a separate fault-scoped
application name.

Before SIGKILL, `pg_locks` and `pg_stat_activity` must show exactly two rows:
one scoped holder PID and one blocked waiter PID. The waiter must have the
exact generation-worker application name and the holder must be its sole
blocker. An outbox-worker waiter, an outbox-only race, a
generation-plus-outbox race, extra lock rows, or a different blocker produces
`setup_blocked`; the runner does not kill a service. The supplemental barrier
artifact records both `waiter_application_name` and
`target_role=generation-worker`.

Only after this ownership proof does the runner invoke:

```text
recovery_stack.py kill generation-worker --fault-id ID
recovery_stack.py start generation-worker --fault-id ID
```

The same task ID must complete at a higher lease epoch. Task 1's evaluator
requires zero during-fault Canon rows for pre-commit, stable committed and
accepted identities for post-commit, and no duplicate authoritative identity.

In `finally`, the holder lock is released and closed, then the scoped trigger,
function, and scope table are dropped. A residue query must return zero. Any
missing holder/waiter boundary or cleanup residue produces `setup_blocked`.

## Projection Faults

The selected runtime uses automatic Spark gate decisions, so
`ready_for_canon` is not a durable manual-pause boundary. Before writing, the
runner installs the same exact-project/chapter pre-Canon advisory barrier used
by the generation proof. After the candidate is visible it requires the exact
generation-worker transaction to be blocked on Canon insertion, stops only the
mapped projection service through `recovery_stack.py`, then releases and
removes the barrier. The normal automatic Canon path commits the fixture-bound
Canon and `canon.projection.requested` outbox identity while the selected
service is unavailable. The runner never changes project policy or calls a
manual approval endpoint.

| Fault kind | Controller service | Convergence evidence |
| --- | --- | --- |
| `qdrant_unavailable` | `qdrant` | project-filtered Qdrant points plus healthy `llm_kb` checkpoint |
| `projection_consumer_unavailable` | `outbox-worker` | fixture-bound SQL projection checkpoint identities |

The durable outbox evidence preserves the stored
`aggregate_type=project`, real project aggregate ID, event ID/type, strict
Canon projection payload, payload hash, status, sanitized error, and attempt
counter. The payload binds the event to the observed Canon, project, chapter,
and candidate; status/error/attempt are mutable observations, not stable
identity.

The supplemental barrier artifact and cleanup contract are identical to the
generation pre-commit proof: one holder, one generation-worker waiter, the
holder as sole blocker, and zero scoped SQL residue.

For `qdrant_unavailable`, Qdrant remains stopped until the fixture event has
been claimed by the real worker and SQL shows a later attempt in durable
`pending` state with a nonempty sanitized error. If this transition is not
observed, the run emits `setup_blocked` and does not perform convergence or
refresh. Task 1 derives this during-fault requirement from the snapshots.

Recovery uses `recovery_stack.py start`. The runner waits for durable
`processed` state and all projection status components to converge. It records
the configured Qdrant collection or the SQL checkpoint identities, replays the
existing `POST /api/projects/{project_id}/projections/refresh` endpoint, checks
convergence again, and requires the external identities to remain unchanged.
It never calls Docker or Compose directly.

## Evidence

`before.json`, `during.json`, and `after.json` are strict snapshot schema v2
direct children of the evidence directory. The runner:

1. validates and derives assertions with `recovery_evidence.py`;
2. writes every artifact atomically with no clobber;
3. reopens and hashes every written artifact;
4. derives and validates again;
5. validates `fault-report.json` with `finalize_recovery.py` before writing;
6. reopens, hashes, and validates the final report again.

For all four barrier-backed faults, finalization also requires the direct-child
`barrier-observation.json` artifact and independently checks its hash, fixture
identity, exact generation-worker waiter, sole-blocker relation, scoped SQL
object and advisory-lock identities, and zero cleanup residue.

The report binds the fault ID, source SHA, evaluator identity, controller event
log, run identity, evidence directory, and database volume lifecycle. A run
that does not reach its exact boundary emits an atomic `setup_blocked` report
and cannot emit PASS.
