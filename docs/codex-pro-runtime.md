# ForWin Codex Pro Runtime

ForWin uses two separate Codex paths:

- Codex operates ForWin through the `forwin` MCP server. See [codex-forwin-mcp.md](codex-forwin-mcp.md).
- ForWin background generation may call Codex through the Host Codex Bridge described here.

The bridge is not an OpenAI API-key model profile. It runs on the host machine, reuses the local `codex` CLI login, and exposes a protected localhost HTTP API to the ForWin Docker container.

## Start The Bridge

On the host, make sure the Codex CLI is logged in through the Pro subscription, then run:

```bash
export FORWIN_CODEX_BRIDGE_TOKEN="change-me"
export FORWIN_CODEX_BRIDGE_PORT=8897
forwin-codex-bridge
```

The bridge exposes:

- `GET /health`
- `POST /v1/codex/chat`
- `POST /v1/codex/jobs`
- `GET /v1/codex/jobs/{job_id}`

The bridge runs `codex exec --json --sandbox read-only -c approval_policy="never"`, so background Codex calls are prompt-only and read-only by default. It must not be used as a shortcut to mutate ForWin state.

## Enable ForWin Routing

Configure the ForWin container with:

```bash
FORWIN_CODEX_ENABLED=true
FORWIN_CODEX_BRIDGE_URL=http://host.docker.internal:8897
FORWIN_CODEX_BRIDGE_TOKEN=change-me
FORWIN_CODEX_MAX_CONCURRENT=1
FORWIN_CODEX_SYNC_TIMEOUT_SECONDS=90
FORWIN_CODEX_JOB_TIMEOUT_SECONDS=900
```

`docker-compose.yml` maps `host.docker.internal` to the host gateway for the `forwin` service.

Routing policy:

- `chapter_plan_materialization` always uses the ordinary OpenAI-compatible adapter.
- `genesis`, `writer`, `reviewer`, `repair`, `phase4`, and `world_model` prefer Codex when enabled.
- Bridge failures fall back to the ordinary adapter and record model fallback metadata.

## Governed Writes

Codex never writes canon directly. It may only return governed action requests such as:

- `world_edit_proposal_create`
- `review_finding_create`
- `repair_suggestion_create`
- `conflict_explanation_create`

ForWin validates those requests and stores them in the proposal, review, or conflict-management layer. Canon changes still go through explicit adapters, `StateUpdater`, and `WorldModelCompiler`.

## Health Checks

Check the bridge directly from the host:

```bash
curl http://127.0.0.1:8897/health
```

Check the ForWin view of bridge health:

```bash
curl http://127.0.0.1:8899/api/settings/codex/health
```

Bridge health is optional for MCP operation. A Codex operator can still inspect and control ForWin through MCP when the bridge is disabled.
