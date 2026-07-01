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

Production on the shared 150 host uses port `8895` because `8897` is already
reserved by another browser-management service. For that environment use:

```bash
export FORWIN_CODEX_BRIDGE_PORT=8895
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
FORWIN_CODEX_DEFAULT_MODEL=gpt-5.3-codex-spark
FORWIN_CODEX_MAX_CONCURRENT=1
FORWIN_CODEX_SYNC_TIMEOUT_SECONDS=90
FORWIN_CODEX_JOB_TIMEOUT_SECONDS=900
```

`docker-compose.yml` maps `host.docker.internal` to the host gateway for the `forwin` service.

For production Swarm on 126/150, point app and generation services at the 150
bridge explicitly:

```bash
FORWIN_CODEX_BRIDGE_URL=http://10.0.0.150:8895
```

Routing policy:

| Task route | Primary chain | Fallback chain |
| --- | --- | --- |
| Genesis, structured planning, arc planning, reviewer, chapter review form, review, Phase 4 feedback, world model, writer state extraction, thread-time extraction, lore-timeline extraction, scene breakdown | Codex 5.3 bridge | ordinary OpenAI-compatible profiles |
| Writer prose such as `chapter_draft`, `scene_generation`, `scene_stitch`, `chapter_rewrite`, and repair generation | ordinary OpenAI-compatible profiles | Codex 5.3 bridge when the ordinary chain fails |
| `chapter_plan_materialization` | ordinary OpenAI-compatible profiles | no Codex bridge route |
| Explicit `spark`, `codex`, `codex_bridge`, `gpt-5.3`, or `codex` model preference | Codex 5.3 bridge | ordinary profiles only if the explicit Codex call fails |

Each bridge or ordinary fallback records model fallback metadata.

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

On 150 production, use:

```bash
curl http://127.0.0.1:8895/health
```

Check the ForWin view of bridge health:

```bash
curl http://127.0.0.1:8899/api/settings/codex/health
```

ForWin treats a health payload as valid only when it identifies
`backend=codex_bridge`. A generic `{"status":"ok"}` response from another
service must not be treated as a usable Codex Bridge.

Bridge health is optional for MCP operation. A Codex operator can still inspect and control ForWin through MCP when the bridge is disabled.
