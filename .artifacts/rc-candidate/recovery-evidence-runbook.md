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

For each fault, set a new empty directory:

```bash
export FORWIN_RECOVERY_EVIDENCE_DIR=<absolute-new-fault-directory>

uv run python .artifacts/rc-candidate/recovery_stack.py config
uv run python .artifacts/rc-candidate/recovery_stack.py fresh-up
```

`fresh-up` destroys only the isolated `forwin-v5-recovery` volumes. It refuses
to append to an existing evidence directory. Controller events form a
SHA-256 chain under `stack-events.jsonl`.

## Independent Fault Set

Execute exactly these contracts:

```text
generation_worker_precommit_crash
generation_worker_postcommit_crash
qdrant_unavailable
projection_consumer_unavailable
minio_pre_canon_unavailable
minio_post_canon_unavailable
publisher_backend_unavailable
publisher_browser_unavailable
publisher_captcha
publisher_mfa
publisher_account_risk
```

Use `recovery_stack.py stop/start` for service outages and `kill/start` for
generation-worker or publisher-worker process crashes. Risk faults use the
supported publisher operator APIs; never bypass CAPTCHA, MFA, or risk control.
The post-Canon MinIO fault must follow
`post-canon-minio-barrier.md` and prove zero temporary trigger/function residue.

## Fault Report

Each fault produces one JSON report with:

```text
schema_version = 1
fault_kind / fault_id / source_sha
result = pass
fault_time / recovery_time
nonempty expected[] / actual[]
replay_result = pass
assertions = exact contract values from finalize_recovery.py
artifacts = before, during, after snapshots
```

Every snapshot is a JSON object containing the same `source_sha`, `fault_kind`,
`fault_id`, and `stage`, plus a `state` object. Service faults also embed the
identity and hash of their own `stack-events.jsonl`. A report from one fault
cannot satisfy another fault.

## Finalize

After all eleven independent reports pass:

```bash
uv run python .artifacts/rc-candidate/finalize_recovery.py \
  --candidate-manifest "$FORWIN_RECOVERY_CANDIDATE_MANIFEST" \
  --fault-report <generation-precommit-report> \
  --fault-report <generation-postcommit-report> \
  --fault-report <qdrant-report> \
  --fault-report <projection-consumer-report> \
  --fault-report <minio-pre-canon-report> \
  --fault-report <minio-post-canon-report> \
  --fault-report <publisher-backend-report> \
  --fault-report <publisher-browser-report> \
  --fault-report <captcha-report> \
  --fault-report <mfa-report> \
  --fault-report <account-risk-report> \
  --output .artifacts/v5-recovery-final/manifest.json
```

The finalizer reloads every report, snapshot, independent event chain, source
identity, candidate-manifest hash, exact image set, Docker identity, tracked
harness, and auditor hash. Any missing fault, duplicate identity, altered
artifact, cross-candidate swap, mismatched timestamp, or failed invariant
makes the final result `fail`.
