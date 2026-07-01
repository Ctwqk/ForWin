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
| `forwin-outbox-worker-swarm` | Eventually consistent side effects from the outbox table | Start with 1 |
| `forwin-publisher-browser-swarm` | Chromium/extension automation for publisher login, heartbeat, upload, and publish flows | Start with 1 when the production publishing path is enabled |

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

## Codex Bridge Runtime

The production Codex Bridge runs on 150 and exposes the ForWin bridge API on
port `8895`:

```bash
curl http://10.0.0.150:8895/health
curl http://10.0.0.150:8895/openapi.json
```

ForWin app, generation-worker, and MCP services that enable Codex routing must
use:

```bash
FORWIN_CODEX_BRIDGE_URL=http://10.0.0.150:8895
FORWIN_CODEX_DEFAULT_MODEL=gpt-5.3-codex-spark
```

Do not point ForWin at `10.0.0.150:8897` for Codex. That port is reserved by a
separate browser-management service in the shared production environment and
does not expose `/v1/codex/chat`.

For a one-shot production publisher baseline check:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

Interpretation:

- `ok`: six ForWin Swarm services are healthy, app/MCP health checks pass, the
  Discord publisher login webhook env is absent, and Fanqie/Qidian are
  connected by both API and browser page evidence.
- `degraded`: runtime is up, but a platform needs human login or page/API state
  has not converged. Follow `blocked_items[*].human_action` and rerun the same
  command.
- `failed`: a required service, health endpoint, publisher browser, or Discord
  env policy check failed.

The verifier is read-only for ForWin business state. It must not create books,
upload chapters, publish content, or record secrets.

For a publisher upload-chain smoke after the baseline verifier, run the
endpoint smoke first. This command may create one `publish=false` API smoke job
and then terminate/delete it; it must not publish content:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job
```

After both platforms are connected in the production publisher browser, run the
browser-claimed `publish=false` smoke:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --endpoint-platform fanqie \
  --create-api-smoke-job \
  --run-upload-smoke \
  --upload-platform fanqie \
  --upload-platform qidian
```

To include the key-protected extension heartbeat-status surface, set
`FORWIN_PUBLISHER_EXTENSION_API_KEY` in the shell through the local secret
manager or an existing deployment secret file, then pass only the environment
variable name:

```bash
python scripts/smoke_production_publisher_upload_chain.py \
  --api-base http://10.0.0.126:8899 \
  --extension-key-env FORWIN_PUBLISHER_EXTENSION_API_KEY \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian
```

The extension key value must never be committed, pasted into logs, printed in
reports, or sent to Discord.

For recurring two-hour Codex/operator intervention checks, install the
read-only supervisor on 150 from a source checkout or deployed copy. It checks
GitHub PRs/issues, publisher upload jobs, MCP generation task state, publisher
browser login/heartbeat state, and Codex Bridge health. It writes one redacted
JSON object per run and exits non-zero when follow-up is required.

Manual one-shot run:

```bash
python scripts/supervise_forwin_interventions.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-url http://10.0.0.126:8896/mcp \
  --github-repo Ctwqk/ForWin \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --output-jsonl /home/taiwei/forwin-supervisor/logs/forwin-supervisor.jsonl \
  --latest-json /home/taiwei/forwin-supervisor/latest.json
```

The supervisor is intentionally read-only. It must not publish content, bypass
publisher login, replay cookies, mutate project/task/chapter state, or resolve
MFA/captcha/risk-control prompts. Login expiry is reported as a blocked item
for a human operator.

Example user-level systemd service on 150:

```ini
[Unit]
Description=ForWin Codex intervention supervisor

[Service]
Type=oneshot
WorkingDirectory=/home/taiwei/deploy-github-sync/repos/ForWin
Environment=PYTHONWARNINGS=ignore
ExecStart=/usr/bin/python3 scripts/supervise_forwin_interventions.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-url http://10.0.0.126:8896/mcp \
  --github-repo Ctwqk/ForWin \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --output-jsonl /home/taiwei/forwin-supervisor/logs/forwin-supervisor.jsonl \
  --latest-json /home/taiwei/forwin-supervisor/latest.json
```

Example timer:

```ini
[Unit]
Description=Run ForWin Codex intervention supervisor every two hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=2h
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

Install and trigger once:

```bash
mkdir -p ~/.config/systemd/user /home/taiwei/forwin-supervisor/logs
$EDITOR ~/.config/systemd/user/forwin-codex-supervisor.service
$EDITOR ~/.config/systemd/user/forwin-codex-supervisor.timer
systemctl --user daemon-reload
systemctl --user enable --now forwin-codex-supervisor.timer
systemctl --user start forwin-codex-supervisor.service
systemctl --user status forwin-codex-supervisor.service
```

For focused manual runtime windows, use the read-only runtime monitor:

```bash
python scripts/monitor_forwin_runtime.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-url http://10.0.0.126:8896/mcp \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --duration-minutes 120
```

The monitor samples API health, MCP health, Swarm service replicas, publisher
platform connection state, and MCP generation activity. It writes JSONL and
returns non-zero if any required sample fails. It does not publish, retry jobs,
start generation, or mutate ForWin project/task/chapter state.

## Runtime Images

`forwin-app-swarm`, `forwin-generation-worker-swarm`, `forwin-mcp-swarm`,
`forwin-publisher-worker-swarm`, and `forwin-outbox-worker-swarm` use the slim
default runtime image. `forwin-publisher-browser-swarm` uses the browser runtime
image target because it owns Chromium, Xvfb, extension profile qualification,
and browser heartbeat checks. The GitHub deploy sync must build and update both
runtime images for the same source commit.

`forwin-mcp-swarm` uses the slim default runtime and should not get a dedicated
image yet. Do not split the MCP image until measured deploy size, startup time,
or dependency-risk data shows the split is worth the extra build and rollout
surface.

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
python -m forwin.cli -v publisher-worker --limit 1 --poll-interval 2
```

It is responsible for backend-owned publisher jobs. It is not responsible for
real browser login or browser clicking.

## Network Rules

- Bind MCP only to trusted LAN paths, localhost, or SSH tunnels.
- Keep publisher browser remote debugging bound to localhost or a trusted
  operator network.
- Keep Basic Auth enabled when exposing the API beyond localhost.
- Keep publisher extension API keys and session secrets separate from Basic Auth.
- Keep Discord login QR webhooks disabled in shared production unless an
  operator explicitly opens a temporary scan-login forwarding window. When a
  window is open, set `FORWIN_ENABLE_PUBLISHER_LOGIN_DISCORD_WEBHOOK=true` and
  use environment secrets or mounted secret files; do not commit webhook URLs or
  paste them into deployment logs. Remove both the enable flag and webhook env
  again after the login window.
- Keep the publisher extension's login QR notification setting disabled in the
  shared production browser profile by default. When disabled, the extension
  must not capture QR images or call `/api/publishers/extension/login-qr`.
  A stale `loginQrNotificationsEnabled=true` profile value does not re-enable
  QR forwarding unless the hidden `loginQrNotificationsAllowed=true` guard is
  also set for the operator-requested temporary window.
- Keep QR forwarding disabled until a deployed browser build has verified a
  direct, non-expired QR capture source; screenshots and invalid QR placeholders
  such as "二维码已失效 / 点击刷新" are intentionally rejected.
- Publisher login QR reminders are only allowed for an active operator-requested
  login session. Ordinary heartbeat checks may record `login-required`, but they
  must not capture QR images or notify Discord just because a login page is
  visible. During incident triage, close stale login tabs before starting a fresh
  operator login session.
- Qidian/WeChat QR capture should prefer direct image extraction from the login
  iframe. The extension uses a scripting fallback for cross-frame QR images and
  rejects full-page screenshots as unsafe login QR payloads.
