# L200 Evidence Collector Runbook

This runbook is only for the fresh 200-chapter run started after the final RC
is frozen. Do not reuse the matrix project or its database.

## Preconditions

- Fresh v5 schema with no projects, Canon commits, generation tasks, outbox
  events, or publisher jobs. Do not insert an equivalent project/Genesis state
  directly in PostgreSQL.
- The collector `bootstrap` command creates the project and performs every
  Genesis generate/lock through supported ForWin MCP/API operations. Its
  transcript is mandatory evidence for the rest of the run.
- No generation task has started.
- RC manifest, all five image identities, tracked release harness, and all role
  containers identify the same immutable RC.
- RC manifest contains passing V1 preflight, release-gate, live-recovery, and
  post-decision fresh-30 evidence; all four share one candidate-manifest hash.
  `init` reloads and revalidates every nested artifact, matrix predecessor
  delta, gate argv/JUnit, recovery event identity, and lifecycle transcript
  rather than trusting the RC summary.
- The L200 manifest does not exist. `bootstrap` creates the output directory
  and exactly one pre-init evidence file, `bootstrap-transcript.json`.
- The exact RC's complete `FORWIN_HTTP_BASIC_USER` and
  `FORWIN_HTTP_BASIC_PASSWORD` pair is exported in the collector environment.

Set local shell variables without recording credentials in evidence files:

```bash
RC_MANIFEST=.artifacts/v5-rc/manifest.json
MCP_URL=http://127.0.0.1:<port>/mcp
API_URL=http://127.0.0.1:<port>
OUTPUT=.artifacts/v5-l200
QUALITY_PROFILE=standard
GATE_DELEGATE=human
PREMISE_FILE=<path-to-l200-premise.txt>
read -r -s FORWIN_L200_DATABASE_URL
export FORWIN_L200_DATABASE_URL
read -r FORWIN_HTTP_BASIC_USER
read -r -s FORWIN_HTTP_BASIC_PASSWORD
export FORWIN_HTTP_BASIC_USER FORWIN_HTTP_BASIC_PASSWORD
```

Enter the complete SQLAlchemy PostgreSQL URL at the hidden prompt. Never pass
it on the command line. Supply every role from one Compose project:

```text
runtime:    forwin, generation-worker, outbox-worker, forwin-mcp,
            publisher-worker
browser:    publisher-browser
dependency: postgres, qdrant, minio
```

The collector requires all containers healthy, exact image IDs, exact image
revision labels for ForWin images, stable secret-redacted configuration
fingerprints, identical effective routing across model-execution roles,
credential-free passive roles, named volumes only, and exact loopback
API/MCP/PostgreSQL/Qdrant bindings. Git and Docker subprocesses reject host
control variables. The local API client sends only the complete Basic
credential pair from the process environment. Local API, MCP, and Qdrant
clients neither inherit proxy settings nor follow redirects, so the recorded
loopback binding is also the network target actually used by the collector.

## Bootstrap Through Supported Operations

Do not create the L200 project separately. Run bootstrap against the frozen
candidate stack:

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py bootstrap \
  --rc-manifest "$RC_MANIFEST" \
  --mcp-url "$MCP_URL" \
  --api-url "$API_URL" \
  --output-dir "$OUTPUT" \
  --title "<fresh L200 title>" \
  --premise-file "$PREMISE_FILE" \
  --genre "<genre>" \
  --quality-profile "$QUALITY_PROFILE" \
  --gate-delegate "$GATE_DELEGATE"
```

The command prints the new `project_id`; export it as `PROJECT_ID` for every
later command. Bootstrap performs `project_create`, the frozen policy update,
and exactly one `genesis_stage_generate` plus one `genesis_stage_lock` for each
of the six ordered stages. The transcript stores argument/result hashes and
operation identities, not premise text or credentials. Its operation hash
chain is bound to the RC source/tree, collector hash, RC-manifest hash, MCP
target, project identity, and matching append-only decision events. A
hand-built equivalent database state without this transcript cannot initialize
an L200 run.

## Initialize Before Writing

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py init \
  --rc-manifest "$RC_MANIFEST" \
  --project-id "$PROJECT_ID" \
  --mcp-url "$MCP_URL" \
  --api-url "$API_URL" \
  --database-url-env FORWIN_L200_DATABASE_URL \
  --quality-profile "$QUALITY_PROFILE" \
  --gate-delegate "$GATE_DELEGATE" \
  --runtime-container <api-container> \
  --runtime-container <generation-worker-container> \
  --runtime-container <outbox-worker-container> \
  --runtime-container <mcp-container> \
  --runtime-container <publisher-worker-container> \
  --browser-container <publisher-browser-container> \
  --dependency-container <postgres-container> \
  --dependency-container <qdrant-container> \
  --dependency-container <minio-container> \
  --output-dir "$OUTPUT"
```

`init` verifies the bootstrap transcript and database audit sequence, then
freezes the project policy, full rule-provenance report, schema, runtime,
container bindings, and collector identity.

## Start Continuous Attestation

Immediately after `init`, start the long-running monitor with the same frozen
arguments. The default interval is 60 seconds and may not exceed 90 seconds;
the hard maximum gap is 180 seconds.

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py monitor \
  <same frozen arguments as init> \
  --interval-seconds 60 \
  >.artifacts/v5-l200-monitor.log 2>&1 &
MONITOR_PID=$!
```

Wait until `attestation/state.json` reports `status=running`, at least one
observation exists, and the monitor process is alive. Only then start writing
through `project_start_writing`.

Each observation rechecks Git SHA/tree/dirty state, image and container
identity, redacted configuration fingerprints, routing, endpoints, live
schema, policy thresholds, rule state, append-only policy/rule audit state,
bootstrap transcript, and bootstrap audit events. Immutable record files form
one sequence-checked hash chain under a single collector-session/RC identity.
Readers and the monitor coordinate through a file lock, so checkpoints cannot
accept a half-written record.

This is a local, operator-honesty, tamper-evident protocol. It does not claim
an external signature or protection from a malicious host administrator.
Within that model, any observed drift writes a permanent failing record,
invalidates the manifest, and cannot be cleared by restoring the previous
code, image, routing, schema, rule, or threshold state. A crashed, `SIGKILL`ed,
restarted, stale, discontinuous, or identity-swapped monitor cannot satisfy
finalization.

## Checkpoints

After accepted count crosses each release boundary, run exactly one checkpoint:

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py checkpoint \
  <same frozen arguments as init> \
  --chapter 25
```

Repeat for `50`, `75`, `100`, `125`, `150`, `175`, and `200`. Each checkpoint:

- verifies source, images, containers, endpoints, RC manifest, policy, rules,
  collector identity, secret-safe container configuration fingerprints, and a
  live PostgreSQL schema fingerprint covering columns, constraints, indexes,
  views, functions, triggers, and enums;
- verifies the append-only policy/rule audit ledger so a temporary mutation
  followed by a revert still invalidates the run;
- verifies the continuous attestation chain is live, gap-bounded, complete,
  and bound to the same collector and RC;
- retains project, task, gate, cost, rule, Canon, projection, maintenance, and
  publisher state;
- discovers newly resolved `band_checkpoints` and freezes separate S1/S3
  evidence for every completed band. S2 is the project-level rule-provenance
  snapshot frozen at `init`, linked to the band ID, band chapter end, and the
  accepted count at collection. It is intentionally not described as an
  instantaneous band-completion snapshot. Continuous rule freeze is proven by
  the live attestation chain plus the append-only rule audit ledger; otherwise
  the checkpoint fails.

Do not edit, replace, or backfill a checkpoint. If source, policy, rule state,
RC identity, or evidence files drift, the collector marks the run invalid.

## Finalize

Wait for accepted count 200, no active task, all outbox/maintenance work
processed, and all three projection checkpoints healthy at chapter 200. Stop
the monitor normally and wait for its terminal chain record:

```bash
kill -TERM "$MONITOR_PID"
wait "$MONITOR_PID"
```

Confirm `attestation/state.json` reports `status=terminated`,
`termination_reason=signal`, and `permanent_failure=false`. Then finalize
within the 180-second attestation gap bound:

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py finalize \
  <same frozen arguments as init>
```

`finalize` emits the exact required output set and fails closed on missing
chapters, reviews, Canon identities, GraphDelta references, snapshot coverage,
entity/alias identity, outbox identity, projection convergence, maintenance
completion, task state, publisher consistency, band evidence, bootstrap
operation/audit identity, continuous-attestation continuity/termination, or
freeze drift. Candidate integrity covers every candidate row's ID,
project/chapter/version tuple, draft reference, plan/draft ownership, and Canon
reverse reference. Canon idempotency duplicate checks consider non-empty keys
only, so legitimate empty pre-Canon/retry values are not counted as duplicate
identities.
Projection convergence includes recomputed Canon-derived digests, exact
Obsidian/LLM-KB file hashes, and exact Qdrant point-ID/payload sets.
Finalization is single-shot: a completed manifest or any partial final output
seals that run against overwrite. A failed or interrupted finalization requires
a new L200 run rather than rewriting its evidence.

After finalization, run the read-only verifier. It revalidates the continuous
attestation, bootstrap operation, checkpoint, and band evidence chains; the
exact eight-file final inventory; every artifact hash; project/report
contracts; the accepted `1..200` sequence; Canon/task/projection invariants;
and a deterministic reconstruction of `final-report.md`. Supporting
`bootstrap-transcript.json`, `attestation/`, `checkpoints/`, and `bands/`
evidence does not change the required eight-file final output set. The verifier
does not connect to the database and never rewrites failed evidence:

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py verify-final \
  --output-dir "$OUTPUT"
```

The database URL is never written to the manifest or process arguments. Only a
credential-free target fingerprint is retained. Unset the environment variable
after finalization:

```bash
unset FORWIN_L200_DATABASE_URL
```
