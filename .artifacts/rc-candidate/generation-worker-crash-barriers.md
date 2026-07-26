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
`qdrant_unavailable`, also set `FORWIN_RECOVERY_QDRANT_URL` and
`FORWIN_RECOVERY_QDRANT_COLLECTION`. The evidence directory must be new or
empty. Every invocation uses a new fault ID and evidence directory;
`recovery_stack.py fresh-up` creates the new run ID and database volume.

## Supported Lifecycle

The runner uses the parameterized MCP endpoint in this order:

1. `project_create` with `target_total_chapters=1`.
2. `project_get`.
3. For `brief`, `world`, `map`, `story_engine`, `book_blueprint`, and
   `bootstrap`: `genesis_get`, generate, `genesis_get`, refine, `genesis_get`,
   lock.
4. `genesis_get` and `project_get` to confirm writing readiness.
5. `project_get` and `task_active_generation_check`.
6. `project_start_writing` with `auto_continue=false` and `max_chapters=1`.

The runner never writes a business table and never changes chapter content to
force a boundary. PostgreSQL and Qdrant access after handoff is read-only,
except for the temporary advisory barrier objects described below.

## Generation Faults

Both generation faults install fault-ID-derived SQL identifiers and bind the
project ID, chapter number, and advisory key as SQL parameters. The trigger
looks up the key from the scoped table and calls `pg_advisory_xact_lock`.

| Fault kind | Trigger boundary | Additional scope |
| --- | --- | --- |
| `generation_worker_precommit_crash` | `BEFORE INSERT ON canon_commit_records` | exact project and chapter |
| `generation_worker_postcommit_crash` | `BEFORE INSERT ON post_canon_maintenance_runs` | exact project, chapter, and `NEW.step_name = 'planning'` |

Before SIGKILL, `pg_locks` and `pg_stat_activity` must show exactly one scoped
holder PID and one blocked worker waiter PID, with the holder as the waiter's
only blocker. The runner then invokes only:

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

After the one-chapter candidate is `ready_for_canon`, the runner stops only the
mapped service through `recovery_stack.py`, accepts the chapter through
`POST /api/projects/{project_id}/chapters/1/review/approve`, and captures the
fixture-bound Canon and `canon.projection.requested` outbox identity.

| Fault kind | Controller service | Convergence evidence |
| --- | --- | --- |
| `qdrant_unavailable` | `qdrant` | project-filtered Qdrant points plus healthy `llm_kb` checkpoint |
| `projection_consumer_unavailable` | `outbox-worker` | fixture-bound SQL projection checkpoint identities |

Recovery uses `recovery_stack.py start`. The runner waits for the durable
outbox row and all projection status components to converge, then replays the
existing `POST /api/projects/{project_id}/projections/refresh` endpoint and
checks convergence again. It never calls Docker or Compose directly.

## Evidence

`before.json`, `during.json`, and `after.json` are strict snapshot schema v2
direct children of the evidence directory. The runner:

1. validates and derives assertions with `recovery_evidence.py`;
2. writes every artifact atomically with no clobber;
3. reopens and hashes every written artifact;
4. derives and validates again;
5. validates `fault-report.json` with `finalize_recovery.py` before writing;
6. reopens, hashes, and validates the final report again.

The report binds the fault ID, source SHA, evaluator identity, controller event
log, run identity, evidence directory, and database volume lifecycle. A run
that does not reach its exact boundary emits an atomic `setup_blocked` report
and cannot emit PASS.
