# L200 Evidence Collector Runbook

This runbook is only for the fresh 200-chapter run started after the final RC
is frozen. Do not reuse the matrix project or its database.

## Preconditions

- Fresh v5 schema and fresh project.
- Genesis stages generated and locked through supported ForWin MCP tools.
- Project target is exactly 200 and accepted count is zero.
- No generation task has started.
- RC manifest, all five image identities, tracked release harness, and all role
  containers identify the same immutable RC.
- RC manifest contains passing V1 preflight, release-gate, live-recovery, and
  post-decision fresh-30 evidence; all four share one candidate-manifest hash.
  `init` reloads and revalidates every nested artifact, matrix predecessor
  delta, gate argv/JUnit, recovery event identity, and lifecycle transcript
  rather than trusting the RC summary.
- The L200 output directory does not exist.

Set local shell variables without recording credentials in evidence files:

```bash
RC_MANIFEST=.artifacts/v5-rc/manifest.json
PROJECT_ID=<fresh-project-id>
MCP_URL=http://127.0.0.1:<port>/mcp
API_URL=http://127.0.0.1:<port>
OUTPUT=.artifacts/v5-l200
QUALITY_PROFILE=standard
GATE_DELEGATE=human
read -r -s FORWIN_L200_DATABASE_URL
export FORWIN_L200_DATABASE_URL
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
control variables. Local API, MCP, and Qdrant clients neither inherit proxy
settings nor follow redirects, so the recorded loopback binding is also the
network target actually used by the collector.

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

Only after `init` succeeds, start writing through `project_start_writing`.

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
- retains project, task, gate, cost, rule, Canon, projection, maintenance, and
  publisher state;
- discovers newly resolved `band_checkpoints` and freezes separate S1/S3/S2
  evidence for every completed band.

Do not edit, replace, or backfill a checkpoint. If source, policy, rule state,
RC identity, or evidence files drift, the collector marks the run invalid.

## Finalize

Wait for accepted count 200, no active task, all outbox/maintenance work
processed, and all three projection checkpoints healthy at chapter 200. Then:

```bash
uv run python .artifacts/rc-candidate/l200_evidence.py finalize \
  <same frozen arguments as init>
```

`finalize` emits the exact required output set and fails closed on missing
chapters, reviews, Canon identities, GraphDelta references, snapshot coverage,
entity/alias identity, outbox identity, projection convergence, maintenance
completion, task state, publisher consistency, band evidence, or freeze drift.
Projection convergence includes recomputed Canon-derived digests, exact
Obsidian/LLM-KB file hashes, and exact Qdrant point-ID/payload sets.

After finalization, run the read-only verifier. It revalidates the checkpoint
and band hash chains, the exact eight-file inventory, every artifact hash,
project/report contracts, the accepted `1..200` sequence, Canon/task/projection
invariants, and a deterministic reconstruction of `final-report.md`. It does
not connect to the database and never rewrites failed evidence:

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
