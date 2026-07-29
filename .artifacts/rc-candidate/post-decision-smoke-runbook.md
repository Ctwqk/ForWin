# Post-Decision Fresh 30 Smoke

Run this only after every R7/R8 code decision is committed and all final
candidate images are built. It must use a fresh v5 database and a project
created after the candidate draft manifest was collected.

## Preconditions

- Candidate source is clean; runtime/browser revisions and all five exact image
  IDs match it.
- Every release-harness producer is tracked at the candidate SHA.
- Fresh database contains no project before the lifecycle runner starts.
- API and MCP URLs are the explicit numeric loopback bindings of the verified
  candidate Compose stack; hostnames, HTTPS, proxies, and alternate local
  services are not accepted.
- Target is exactly 30 and no older matrix project is reused.

Keep the database URL out of process arguments:

```bash
read -r -s FORWIN_SMOKE_DATABASE_URL
export FORWIN_SMOKE_DATABASE_URL
```

Create and hand off the project with the tracked lifecycle runner. The premise
file is read locally and only argument hashes are retained in the transcript:

```bash
uv run python .artifacts/rc-candidate/smoke_lifecycle.py \
  --candidate-manifest <candidate-draft-manifest> \
  --mcp-url <candidate-mcp-url> \
  --api-url <candidate-api-url> \
  --title <fresh-smoke-title> \
  --premise-file <private-premise-file> \
  --genre <genre> \
  --quality-profile <standard-or-pulp> \
  --gate-delegate <human-or-spark> \
  --output .artifacts/v5-post-decision-smoke/lifecycle.json
```

The runner creates the project through MCP, updates RuntimePolicy through HTTP,
generates and locks all six Genesis stages through MCP, verifies no active task,
and invokes the supported writing handoff. The resulting transcript binds both
service URLs to the exact candidate-stack ports. Its HTTP/MCP clients do not
inherit proxies or follow redirects, and its Git checks reject repository and
Docker control variables. After the handoff, the fresh database must contain
exactly that one project. Wait until it has 30 accepted chapters, no
review/failed chapter, no active generation task, and settled
projection/maintenance/publisher state.

## Finalize

```bash
uv run python .artifacts/rc-candidate/finalize_smoke.py \
  --candidate-manifest <candidate-draft-manifest> \
  --operation-transcript \
    .artifacts/v5-post-decision-smoke/lifecycle.json \
  --project-id <fresh-project-id> \
  --mcp-url <candidate-mcp-url> \
  --api-url <candidate-api-url> \
  --database-url-env FORWIN_SMOKE_DATABASE_URL \
  --runtime-container <api-container> \
  --runtime-container <generation-worker-container> \
  --runtime-container <outbox-worker-container> \
  --runtime-container <mcp-container> \
  --runtime-container <publisher-worker-container> \
  --browser-container <publisher-browser-container> \
  --dependency-container <postgres-container> \
  --dependency-container <qdrant-container> \
  --dependency-container <minio-container> \
  --quality-profile <standard-or-pulp> \
  --gate-delegate <human-or-spark> \
  --output-dir .artifacts/v5-post-decision-smoke

unset FORWIN_SMOKE_DATABASE_URL
```

After the finalizer succeeds, destroy the bootstrap stack and all of its named
volumes. It has no remaining release consumer; L200 must start from a different
fresh Compose project and database.

```bash
rc_candidate_destroy
```

The finalizer independently collects project, chapter, task, GateLedger, cost,
rule provenance, Genesis, Canon, GraphDelta, snapshot, entity/alias, outbox,
projection, maintenance, and publisher evidence. Every Genesis stage must
remain locked after handoff. Every generation task must expose a valid,
terminal, redacted RuntimePolicy snapshot whose version and policy hash match
the live project policy; story premise and other task payload content are not
written to the evidence. The handoff task in the operation chain must be
present in the terminal task-policy snapshot set.

The finalizer reuses the same integrity contract as the R6 matrix, requires an
exact accepted `1..30` sequence, and records the candidate/tool hashes. It also
rejects cross-stack endpoints, non-loopback service targets, unexpected mounts,
container/image drift, an incomplete HTTP/MCP operation sequence, a broken
request hash chain, or cross-candidate evidence. The final RC collector reloads
the nested state and transcript, re-runs the contract, and deterministically
regenerates the report instead of trusting either the smoke summary or its
report hash.
