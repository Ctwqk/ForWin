# Release Candidate Freeze Runbook

## 1. Draft Identity

After all tracked R8 changes are committed and final images are built, collect
a draft identity. Every executable file in `RELEASE_HARNESS_PATHS` must already
be tracked at `HEAD`; ignored or locally modified producers make collection
fail. Draft status cannot initialize L200. The collector never overwrites an
existing draft or final manifest; preserve a failed attempt and choose a new
output path.

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

### Fresh Candidate Bootstrap

Draft collection requires the five runtime roles to be running, but every v5
role intentionally fails closed against an empty PostgreSQL volume. Bootstrap
one fresh, isolated Compose project in dependency-first order. Do not start an
application role before the exact candidate runtime image has migrated that
project's database.

Prepare an ignored Compose override that binds the exact runtime and browser
image tags, the full candidate revision label, container names derived from
`RC_COMPOSE_PROJECT`, named volumes, numeric loopback ports, and the secured
runtime/provider env files. The override must not add bind mounts. Only API,
generation worker, and outbox worker receive the provider env file; MCP,
publisher worker, and publisher browser remain passive and credential-free.

```bash
set -Eeuo pipefail

RC_BOOTSTRAP_ID="$(date -u +%Y%m%dt%H%M%Sz)-$$"
export RC_COMPOSE_PROJECT="forwin-v5-rc-${RC_BOOTSTRAP_ID}"
export RC_ENV_FILE=<absolute-secured-runtime-env>
export RC_OVERRIDE=<absolute-ignored-exact-image-override>

rc_compose() {
  docker compose -p "$RC_COMPOSE_PROJECT" \
    -f docker-compose.yml \
    -f "$RC_OVERRIDE" \
    --env-file "$RC_ENV_FILE" \
    --profile publisher "$@"
}

rc_candidate_destroy() {
  trap - ERR INT TERM
  rc_compose down --volumes --remove-orphans
}

rc_candidate_abort() {
  local status=$?
  rc_candidate_destroy || true
  exit "$status"
}

trap rc_candidate_abort ERR INT TERM

test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$RC_COMPOSE_PROJECT")"
for volume_suffix in forwin-data forwin-postgres forwin-qdrant forwin-minio
do
  if docker volume inspect "${RC_COMPOSE_PROJECT}_${volume_suffix}" >/dev/null 2>&1; then
    printf 'candidate volume already exists: %s\n' \
      "${RC_COMPOSE_PROJECT}_${volume_suffix}" >&2
    false
  fi
done

rc_compose up -d --no-build --wait --wait-timeout 120 postgres qdrant minio
rc_compose run --rm --no-deps forwin alembic upgrade head
rc_compose up -d --no-build --wait --wait-timeout 180 forwin generation-worker outbox-worker forwin-mcp publisher-worker publisher-browser
```

The two bounded `--wait` barriers require PostgreSQL, Qdrant, API, MCP, and
publisher browser to become healthy; generation, outbox, and publisher workers
must remain running. The final `up` names exactly six application roles, so
`postgres-test` and any future unrelated service cannot join the candidate.
Verify that all six application containers use the exact candidate image IDs
and full revision label before collecting the draft.

The bootstrap stack is not V1 evidence. It exists only to break the
identity-manifest dependency cycle and may later host the independently
required fresh-30 smoke. V1 and every recovery fault still own separate fresh
volumes, event chains, and terminal destroy through their tracked controllers.
Keep the trap and helper functions active through draft collection and
fresh-30 finalization. Any setup failure destroys the attempted containers,
network, and volumes. The fresh-30 runbook owns terminal destroy after its
finalizer seals the last bootstrap-stack evidence.

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
  --matrix-manifest <absolute-frozen-r6-matrix-manifest> \
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

Use the eleven exact commands in `recovery-evidence-runbook.md`. Each recovery
runner owns fresh-up, fault injection, recovery, and terminal destroy; do not
manually pre-run `recovery_stack.py fresh-up`. Every invocation requires a new
absolute evidence directory and unique fault ID. A reused directory or fault
ID, `setup_blocked`, or any other non-pass report cannot satisfy
`finalize_recovery.py`.

The gate runner uses a minimal child environment, rejects Git/Docker/Compose,
Python, pytest, coverage, and uv control variables, disables ambient pytest
plugin discovery, explicitly loads `pytest_asyncio.plugin`, and runs uv
offline. Ruff is fixed at `0.15.22`; a missing cached tool or locked dependency
is a failed gate, not permission to fetch a different version. `--resume` may
append only to an incomplete gate manifest; a complete release-gate PASS is
sealed against further writes.

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
read -r FORWIN_HTTP_BASIC_USER
read -r -s FORWIN_HTTP_BASIC_PASSWORD
export FORWIN_HTTP_BASIC_USER FORWIN_HTTP_BASIC_PASSWORD
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

Keep the exact candidate's complete Basic credential pair exported while the
four matrix cells create or update project policy and while the finalizer reads
that policy. The runners fail before sending a request when only one value is
present, and credentials must never be written into matrix evidence.

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

## 3. Immutable Commit Record Or Annotated Tag

The final manifest records the complete Git commit SHA and tree hash from a
clean worktree, bound to the tested image revisions and evidence. This immutable
commit record is sufficient; creating a tag is optional. L200 verifies that the
recorded commit exists, that its tree matches, and that the running source and
images remain unchanged.

If using a tag, create an annotated tag at the exact candidate source only after
all evidence passes:

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
  --matrix-audit-manifest <absolute-frozen-r6-audit-manifest> \
  --quality-profile <selected-profile> \
  --gate-delegate <selected-delegate> \
  --v1-manifest .artifacts/v1-release-gate/manifest.json \
  --gate-manifest .artifacts/v5-rc-gates/manifest.json \
  --recovery-manifest .artifacts/v5-recovery-final/manifest.json \
  --smoke-manifest .artifacts/v5-post-decision-smoke/manifest.json \
  --output .artifacts/v5-rc/manifest.json
```

To bind an optional annotated tag, add `--rc-tag <v5-rc-tag>` to this command.
An explicitly supplied tag must be annotated and point to the recorded commit;
an invalid tag does not fall back to the commit-only path.

Final mode revalidates the V1 event chain/report, matrix cells and deterministic
report, exact gate runner/argv/JUnit results, every independent recovery
report/event chain and exact family runner, and the full fresh-30 state plus its
HTTP/MCP operation transcript. It also requires the immutable commit record
(and verifies the annotated tag object when supplied),
identical effective routing across the three model-execution roles,
credential-free MCP/publisher-worker routing, one source tree, all five image
identities, one candidate-manifest hash, one evaluator hash, source-bound
runner hashes, exact endpoint bindings, and one Docker daemon identity across
the recovery set.

The local hash chains are tamper-evident under the tracked-candidate model; they
are not proof against a malicious operator who controls both execution and
artifact storage. A release that requires that stronger threat model must also
retain externally signed CI provenance or a signed attestation outside this
workspace.
