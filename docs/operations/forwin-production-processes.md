# ForWin Production Processes

This document describes the production process roles implied by the ForWin
service-process roadmap. It complements `docs/operations/infra-distribution-plan.md`.

## 126 App Processes

The 126 host is the ForWin application node. It should run stateless or
light-state application processes and should not host production Postgres,
Qdrant, or MinIO containers for ForWin.

| Process | Role | Normal replica guidance |
| --- | --- | --- |
| `forwin-app-swarm` | FastAPI, World Studio UI, project APIs, task enqueue/control, read views | Start with 1 |
| `forwin-generation-worker-swarm` | Claims and executes durable generation tasks | Start with 1; raise only for multi-project throughput |
| `forwin-mcp-swarm` | MCP gateway that talks to the API | Start with 1 and expose only on trusted paths |
| `forwin-publisher-worker-swarm` | Backend publisher jobs such as cover generation | Start with 1 |
| `forwin-publisher-browser-swarm` | Optional Chromium/extension automation | Start only when browser publishing is needed |

The generation worker can scale above 1 for multiple projects, but the system
must still prevent same-project adjacent chapter parallelism through task
constraints and lease ownership.

## 150 Data Layer

Production data stores remain centralized on 150:

- Postgres for project, task, canon, publisher, and observability state
- Qdrant for vector retrieval and knowledge indexes
- MinIO for artifacts and object storage

Application processes on 126 should point at the 150-hosted endpoints or Swarm
overlay services. The production path should not start app-local stateful
containers on 126.

## Operator Checks

Prefer ForWin API/MCP tools for project, Genesis, task, and chapter truth. Use
raw database inspection only when no equivalent operator tool exists.

Useful checks:

| Check | Signal |
| --- | --- |
| queued/running generation tasks | A queued task with no worker progress means worker process or config issue |
| `lease_owner` | Shows which generation worker owns a running task |
| `lease_expires_at` | Shows whether another worker may reclaim a stuck running task |
| worker heartbeat | Confirms worker progress without pretending the worker is an HTTP service |
| MCP health/upstream API connectivity | Confirms `forwin-mcp-swarm` can reach `forwin-app-swarm` |
| publisher browser heartbeat | Confirms browser automation is logged in and connected |

## Generation Worker Operation

The worker should run the existing CLI command:

```bash
python -m forwin.cli -v generation-worker \
  --worker-id forwin-generation-worker-swarm-1 \
  --lease-seconds 300 \
  --poll-interval 2
```

For smoke checks, a one-shot worker run may be used:

```bash
python -m forwin.cli generation-worker --once --worker-id smoke-check
```

If there is no claimable task, a no-work one-shot run should exit cleanly.

## Publisher Worker Operation

The publisher backend worker should run:

```bash
python -m forwin.cli -v publisher-worker --limit 1
```

It is responsible for backend-owned publisher jobs. It is not responsible for
real browser login or browser clicking.

## Network Rules

- Bind MCP only to trusted LAN paths, localhost, or SSH tunnels.
- Keep publisher browser remote debugging bound to localhost or a trusted
  operator network.
- Keep Basic Auth enabled when exposing the API beyond localhost.
- Keep publisher extension API keys and session secrets separate from Basic Auth.
