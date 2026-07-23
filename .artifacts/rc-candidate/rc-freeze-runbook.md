# Release Candidate Freeze Runbook

## 1. Draft Identity

After all tracked R8 changes are committed and final images are built, collect
a draft identity. Every executable file in `RELEASE_HARNESS_PATHS` must already
be tracked at `HEAD`; ignored or locally modified producers make collection
fail. Draft status cannot initialize L200.

After R6 has stopped and before the integration commit, promote only the
reviewed release source inventory:

```bash
git add -f \
  --pathspec-from-file=.artifacts/rc-candidate/release-source-files.txt
git diff --cached --name-status
```

The inventory is itself candidate-bound and intentionally excludes draft
manifests, preflight logs, and all live evidence. Never force-add the whole
`.artifacts/rc-candidate` directory.

```bash
uv run python .artifacts/rc-candidate/collect_rc_manifest.py \
  --draft \
  --runtime-image <runtime-tag> \
  --browser-image <publisher-browser-tag> \
  --postgres-image postgres:16-alpine \
  --qdrant-image qdrant/qdrant:v1.17.1 \
  --minio-image minio/minio:RELEASE.2025-09-07T16-13-09Z \
  --runtime-container <api-container> \
  --runtime-container <generation-worker-container> \
  --runtime-container <outbox-worker-container> \
  --runtime-container <mcp-container> \
  --runtime-container <publisher-worker-container> \
  --matrix-manifest .artifacts/v4-matrix-candidate/manifest.json \
  --quality-profile <selected-profile> \
  --gate-delegate <selected-delegate> \
  --output .artifacts/v5-rc/candidate-draft.json
```

## 2. Candidate Evidence

Run the isolated V1 fresh-schema preflight and finalize its sealed event chain
with `finalize_v1.py`. Run all eight immutable release-gate steps with
`run_rc_gates.py`; partial selection manifests are never release-passing.
Execute the eleven independent fault reports and finalize them with
`finalize_recovery.py`. Execute the tracked HTTP/MCP lifecycle runner for a
fresh post-decision 30-chapter project and finalize it with
`finalize_smoke.py`.

The gate runner uses a minimal child environment, rejects Git/Docker/Compose,
Python, pytest, coverage, and uv control variables, disables ambient pytest
plugin discovery, explicitly loads `pytest_asyncio.plugin`, and runs uv
offline. Ruff is fixed at `0.15.22`; a missing cached tool or locked dependency
is a failed gate, not permission to fetch a different version.

V1 is complete only when both the preflight manifest and the fresh-30 manifest
pass for the exact candidate. The preflight proves the migration cycle,
stale-schema fail-fast, all-role health/image identity, and LAN embedding. The
fresh-30 proves the supported HTTP/MCP Genesis-to-Canon lifecycle.

Finalize R6 only after all four cells have stopped. The finalizer binds its API,
MCP, and PostgreSQL targets to the exact loopback ports of one verified
candidate Compose stack. Git and Docker subprocesses reject host control
variables instead of inheriting alternate repository or daemon targets:
The output directory must be absent or empty; preserve any partial audit and
use a new directory for a retry instead of overwriting earlier evidence.

```bash
read -r -s FORWIN_MATRIX_DATABASE_URL
export FORWIN_MATRIX_DATABASE_URL
uv run python .artifacts/rc-candidate/finalize_matrix.py \
  --matrix-manifest .artifacts/v4-matrix-candidate/manifest.json \
  --mcp-url <candidate-mcp-url> \
  --api-url <candidate-api-url> \
  --database-url-env FORWIN_MATRIX_DATABASE_URL \
  --runtime-container <api-container> \
  --runtime-container <generation-worker-container> \
  --runtime-container <outbox-worker-container> \
  --runtime-container <mcp-container> \
  --runtime-container <publisher-worker-container> \
  --browser-container <publisher-browser-container> \
  --dependency-container <postgres-container> \
  --dependency-container <qdrant-container> \
  --dependency-container <minio-container> \
  --output-dir .artifacts/v4-matrix-candidate/final-audit
unset FORWIN_MATRIX_DATABASE_URL
```

The matrix may be a predecessor of the final RC only when it is an ancestor and
every intervening file is in the collector's explicit R8
publisher/recovery allowlist or the tracked release-harness directory. The
collector does not accept arbitrary files under `tests/` or `forwin/http/`.
Writer, Genesis, Canon, database-schema/migration, and model-routing changes
require a new matrix.

The finalizer may execute from the frozen matrix worktree. Its manifest records
that absolute execution path, but acceptance binds the canonical repository
relative tool path and requires both the executed copy and final RC copy to
have the exact same hash.

Each cell also binds the manifest project ID to the MCP project identity and
the frozen policy version to both the HTTP response and PostgreSQL row. A
policy changed and later restored to identical content still fails by version.

## 3. Annotated Tag

Create an annotated tag at the exact candidate source only after all evidence
passes:

```bash
git tag -a <v5-rc-tag> -m "Freeze ForWin v5 release candidate"
test "$(git cat-file -t refs/tags/<v5-rc-tag>)" = tag
test "$(git rev-parse refs/tags/<v5-rc-tag>^{})" = "$(git rev-parse HEAD)"
```

## 4. Final Manifest

```bash
uv run python .artifacts/rc-candidate/collect_rc_manifest.py \
  --runtime-image <runtime-tag> \
  --browser-image <publisher-browser-tag> \
  --postgres-image postgres:16-alpine \
  --qdrant-image qdrant/qdrant:v1.17.1 \
  --minio-image minio/minio:RELEASE.2025-09-07T16-13-09Z \
  --runtime-container <api-container> \
  --runtime-container <generation-worker-container> \
  --runtime-container <outbox-worker-container> \
  --runtime-container <mcp-container> \
  --runtime-container <publisher-worker-container> \
  --matrix-audit-manifest \
    .artifacts/v4-matrix-candidate/final-audit/manifest.json \
  --quality-profile <selected-profile> \
  --gate-delegate <selected-delegate> \
  --rc-tag <v5-rc-tag> \
  --v1-manifest .artifacts/v1-release-gate/manifest.json \
  --gate-manifest .artifacts/v5-rc-gates/manifest.json \
  --recovery-manifest .artifacts/v5-recovery-final/manifest.json \
  --smoke-manifest .artifacts/v5-post-decision-smoke/manifest.json \
  --output .artifacts/v5-rc/manifest.json
```

Final mode revalidates the V1 event chain/report, matrix cells and deterministic
report, exact gate runner/argv/JUnit results, every independent recovery
report/event chain, and the full fresh-30 state plus its HTTP/MCP operation
transcript. It also requires the annotated tag object, identical effective
routing across the three model-execution roles, credential-free
MCP/publisher-worker routing, all five image identities, and one shared
candidate-manifest hash across every release artifact.

The local hash chains are tamper-evident under the tracked-candidate model; they
are not proof against a malicious operator who controls both execution and
artifact storage. A release that requires that stronger threat model must also
retain externally signed CI provenance or a signed attestation outside this
workspace.
