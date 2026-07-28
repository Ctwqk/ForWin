# Live Recovery Evidence Runbook

Run every fault against its own fresh disposable stack and evidence directory.
Never reuse a project, database volume, fault ID, or event log between faults.

## Candidate Configuration

```bash
export FORWIN_RECOVERY_CANDIDATE_MANIFEST=<absolute-candidate-manifest>
export FORWIN_RECOVERY_ENV_FILE=<absolute-runtime-env>
export FORWIN_RECOVERY_PROVIDER_ENV_FILE=<absolute-provider-env>
```

The candidate manifest supplies the source SHA/tree, runtime, browser,
PostgreSQL, Qdrant, and MinIO image identities, plus the tracked release
harness inventory. The controller rejects a dirty worktree, mutable harness,
mismatched image ID/revision, Docker/Git/Compose control environment, or a
non-local Docker endpoint.

## V1 Fresh-schema Preflight

Use one new evidence directory for the exact RC. This preflight owns the real
upgrade/check/downgrade/upgrade cycle, stale-schema role startup failure, all
nine Compose roles, exact image identity, functional health probes, and the
LAN embedding call.

```bash
export FORWIN_RECOVERY_EVIDENCE_DIR="$PWD/.artifacts/v1-release-gate/live"

uv run python .artifacts/rc-candidate/recovery_stack.py config
uv run python .artifacts/rc-candidate/recovery_stack.py v1-up
uv run python .artifacts/rc-candidate/recovery_stack.py destroy

uv run python .artifacts/rc-candidate/finalize_v1.py \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --stack-events "$FORWIN_RECOVERY_EVIDENCE_DIR/stack-events.jsonl" \
  --output .artifacts/v1-release-gate/manifest.json
```

`finalize_v1.py` writes both `manifest.json` and the required
`.artifacts/v1-release-gate/report.md`. Destroy the isolated stack before
finalizing so no later controller event changes the sealed event-log hash.
The finalizer requires the exact start/completion/destroy event sequence, one
run ID, and an empty post-destroy service set. Never append to this evidence
directory after finalization.

The stale-schema probe retains the raw startup log, requires the structured
`FORWIN_SCHEMA_REVISION_MISMATCH` classification, and compares the generation
task table before and after startup. The embedding probe disables proxy
inheritance and records the actual private-LAN TCP peer as well as metadata and
vector dimensions.

The V1 release requirement is intentionally split across two independently
required RC artifacts. This preflight proves schema, role, image, and embedding
behavior. The exact same RC's post-decision fresh-30 manifest proves the
supported HTTP/MCP project creation, policy update, Genesis generate/lock, writing
handoff, immutable RuntimePolicy task snapshot, candidate, accepted Canon,
BookState/GraphDelta/Snapshot, and outbox path. The final RC collector requires
and revalidates both artifacts.

## Live Recovery Faults

The three runners own fresh-up, fault injection, recovery, and terminal
destroy. Do not run `recovery_stack.py fresh-up` before a runner; that would
conflict with the runner's isolated lifecycle and evidence ownership.

Set the fixed isolated endpoints and choose one new absolute evidence root.
The root must not exist before the first command. `RECOVERY_RUN_ID` makes every
fault ID and child directory unique for this eleven-run set.

```bash
export FORWIN_RECOVERY_CANDIDATE_MANIFEST="$(
  realpath .artifacts/v5-rc/candidate-draft.json
)"
export FORWIN_RECOVERY_DATABASE_URL="postgresql://forwin:forwin@127.0.0.1:55434/forwin"
export FORWIN_RECOVERY_QDRANT_URL="http://127.0.0.1:16337"
export FORWIN_RECOVERY_QDRANT_COLLECTION="chapter_memories"
export FORWIN_RECOVERY_MINIO_ENDPOINT="127.0.0.1:19100"
export FORWIN_RECOVERY_MINIO_ACCESS_KEY="forwin-recovery"
export FORWIN_RECOVERY_MINIO_SECRET_KEY="forwin-recovery-secret"
export FORWIN_RECOVERY_MINIO_BUCKET="forwin-recovery-artifacts"
export FORWIN_RECOVERY_MINIO_PREFIX="artifacts"
export FORWIN_RECOVERY_MINIO_SECURE="false"
export RECOVERY_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
export RECOVERY_EVIDENCE_ROOT="$(
  pwd -P
)/.artifacts/v5-recovery-live/$RECOVERY_RUN_ID"
test ! -e "$RECOVERY_EVIDENCE_ROOT"
```

Export `FORWIN_PUBLISHER_EXTENSION_API_KEY`, `FORWIN_HTTP_BASIC_USER`, and
`FORWIN_HTTP_BASIC_PASSWORD` from the exact candidate's secured operator
configuration before the five publisher commands. Do not write those values
into an evidence file.

Run all eleven commands. Each command uses the runner's real CLI, a new
absolute evidence directory, and a unique fault ID.

```bash
uv run python .artifacts/rc-candidate/generation_projection_recovery.py run \
  --fault-kind generation_worker_precommit_crash \
  --fault-id "${RECOVERY_RUN_ID}-01-generation-precommit" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/01-generation-precommit"

uv run python .artifacts/rc-candidate/generation_projection_recovery.py run \
  --fault-kind generation_worker_postcommit_crash \
  --fault-id "${RECOVERY_RUN_ID}-02-generation-postcommit" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/02-generation-postcommit"

uv run python .artifacts/rc-candidate/generation_projection_recovery.py run \
  --fault-kind qdrant_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-03-qdrant" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/03-qdrant"

uv run python .artifacts/rc-candidate/generation_projection_recovery.py run \
  --fault-kind projection_consumer_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-04-projection-consumer" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/04-projection-consumer"

uv run python .artifacts/rc-candidate/minio_recovery.py run \
  --fault-kind minio_pre_canon_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-05-minio-pre-canon" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/05-minio-pre-canon"

uv run python .artifacts/rc-candidate/minio_recovery.py run \
  --fault-kind minio_post_canon_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-06-minio-post-canon" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/06-minio-post-canon"

uv run python .artifacts/rc-candidate/publisher_recovery.py run \
  --fault-kind publisher_backend_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-07-publisher-backend" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/07-publisher-backend"

uv run python .artifacts/rc-candidate/publisher_recovery.py run \
  --fault-kind publisher_browser_unavailable \
  --fault-id "${RECOVERY_RUN_ID}-08-publisher-browser" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/08-publisher-browser"

uv run python .artifacts/rc-candidate/publisher_recovery.py run \
  --fault-kind publisher_captcha \
  --fault-id "${RECOVERY_RUN_ID}-09-publisher-captcha" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/09-publisher-captcha"

uv run python .artifacts/rc-candidate/publisher_recovery.py run \
  --fault-kind publisher_mfa \
  --fault-id "${RECOVERY_RUN_ID}-10-publisher-mfa" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/10-publisher-mfa"

uv run python .artifacts/rc-candidate/publisher_recovery.py run \
  --fault-kind publisher_account_risk \
  --fault-id "${RECOVERY_RUN_ID}-11-publisher-account-risk" \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --mcp-url http://127.0.0.1:19096/mcp \
  --api-url http://127.0.0.1:19099 \
  --database-url-env FORWIN_RECOVERY_DATABASE_URL \
  --evidence-dir "$RECOVERY_EVIDENCE_ROOT/11-publisher-account-risk"
```

## Fault Report

Each command must exit zero and write `fault-report.json` with schema version
2, `result = pass`, three hashed snapshots, one hashed terminal event chain,
the source-bound semantic evaluator, and the exact family runner identity.
`setup_blocked` is a non-pass result. It cannot satisfy finalization.

Never retry with an existing directory or fault ID. A `setup_blocked`, failed,
interrupted, or otherwise non-pass run remains evidence of that attempt; use a
new `RECOVERY_RUN_ID` and eleven new child directories for the retry. A reused
directory or fault ID cannot finalize.

## Finalize

After all eleven independent commands return PASS, run this exact finalizer:

```bash
export RECOVERY_FINAL_DIR="$(
  pwd -P
)/.artifacts/v5-recovery-final/$RECOVERY_RUN_ID"
test ! -e "$RECOVERY_FINAL_DIR"

uv run python .artifacts/rc-candidate/finalize_recovery.py \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/01-generation-precommit/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/02-generation-postcommit/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/03-qdrant/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/04-projection-consumer/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/05-minio-pre-canon/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/06-minio-post-canon/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/07-publisher-backend/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/08-publisher-browser/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/09-publisher-captcha/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/10-publisher-mfa/fault-report.json" \
  --fault-report "$RECOVERY_EVIDENCE_ROOT/11-publisher-account-risk/fault-report.json" \
  --output "$RECOVERY_FINAL_DIR/manifest.json"
```

The finalizer reloads every report, snapshot, independent event chain, source
identity, candidate-manifest hash, exact image set, Docker identity, tracked
harness, evaluator, and auditor hash. The final RC collector then recomputes
and source-binds the exact family runner and requires one Docker daemon
identity across the eleven event chains. Any missing fault, duplicate
identity, altered artifact, cross-candidate or cross-fault swap, mismatched
timestamp, `setup_blocked`, or failed invariant makes the evidence unusable.
