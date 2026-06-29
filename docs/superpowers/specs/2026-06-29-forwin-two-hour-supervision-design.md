# ForWin Two-Hour Supervision Design

## Context

ForWin production is deployed from GitHub `Ctwqk/ForWin` `master` into
`10.0.0.126:/Users/magi1/ForWin-swarm`, while shared data and the Swarm manager
live on `10.0.0.150`. The current read-only runtime monitor already samples API
health, MCP health, Swarm service replicas, publisher platform state, and active
generation state. It is useful for manual operator windows, but it does not yet
cover the full recurring Codex intervention surface requested for production.

Codex's native automation tool is not available in this runtime, so the approved
approach is a 150-hosted systemd timer that runs a repository script every two
hours and writes a structured, redacted JSONL record for each run.

## Goals

- Check every two hours for work that needs Codex or operator intervention.
- Cover GitHub PRs, GitHub issues, ForWin upload jobs, generation tasks,
  publisher browser login/heartbeat state, and Codex Bridge health.
- Emit one structured result per run with the required top-level fields:
  `checked_at`, `github_prs_checked`, `issues_checked`,
  `upload_jobs_checked`, `generation_tasks_checked`,
  `publisher_browser_heartbeat`, `actions_taken`, and `blocked_items`.
- Keep the first implementation mostly read-only. It may record observations,
  classify blocked items, and run safe checks; it must not publish content,
  bypass login, or mutate project/task/chapter state outside an explicit
  follow-up implementation plan.
- Keep all cookies, session values, tokens, API keys, Discord webhooks, QR
  images, passwords, and verification codes out of logs and reports.

## Non-Goals

- Do not replace the ForWin MCP operator path for project, Genesis, task, or
  chapter actions.
- Do not automate platform login, captcha, MFA, phone confirmation, payment, or
  platform risk-control prompts.
- Do not perform real publishing from the scheduled supervisor.
- Do not use the 126 deploy output directory as the long-term coding source.

## Approach

### Recommended Path: 150 systemd timer

Add a repository-owned supervisor script and a documented systemd timer on 150.
The timer runs every two hours from the GitHub-synced or source checkout, writes
JSONL into a persistent log directory, and exits non-zero when there are blocked
items that require human or Codex follow-up.

This is the most reliable option because it can see the production LAN, Docker
Swarm, the deployed ForWin API/MCP endpoints, and the publisher browser state.

### Alternatives Considered

- GitHub Actions schedule: good for repository-only checks, but it cannot
  reliably inspect LAN-only ForWin runtime state or publisher browser sessions.
- Codex native automation: preferred in principle, but the automation tool is
  not exposed in the current Codex runtime.

## Components

### Supervisor Script

Create a script such as `scripts/supervise_forwin_interventions.py`.

Responsibilities:

- Read only from GitHub, ForWin HTTP endpoints, Docker Swarm, and ForWin MCP.
- Reuse the redaction rules from `scripts/monitor_forwin_runtime.py`.
- Gather GitHub pull requests and issues using `gh` or GitHub REST when
  configured.
- Gather upload job summaries from ForWin publisher APIs.
- Gather generation task summaries through ForWin MCP tools where available.
- Gather publisher browser status from the platform API and heartbeat checks.
- Gather Codex Bridge health from `/api/settings/codex/health`.
- Write a single JSON object per run.

### systemd Units

Document two user or system units on 150:

- `forwin-codex-supervisor.service`: runs one supervisor pass.
- `forwin-codex-supervisor.timer`: runs every two hours with a small randomized
  delay so it does not collide with deploy sync or other scheduled work.

The service should run from the ForWin source checkout or deployed copy with
explicit environment for API/MCP URLs and Docker context.

### Log And Status Files

Use a persistent log directory such as:

`/home/taiwei/forwin-supervisor/logs/forwin-supervisor.jsonl`

Each record is append-only JSONL. Optional latest-status output may be written to
a separate small JSON file for dashboards or quick operator reads.

## Data Flow

1. systemd timer starts the supervisor service.
2. The script records `checked_at`.
3. GitHub PR and issue checks classify review requests, changes requested,
   failing checks, unresolved review threads when available, and recent comments.
4. ForWin upload job checks classify failed jobs, retry exhaustion,
   `codex_intervention_required`, login failures, and publish=false safety
   state.
5. ForWin generation checks use MCP task tools for active, failed, paused, or
   long-running generation tasks.
6. Publisher browser checks inspect platform connection state and heartbeat
   freshness without reading cookies.
7. Codex Bridge checks read `/api/settings/codex/health` and report enabled,
   healthy, and status fields without logging bridge tokens.
8. The script writes `actions_taken` for safe observations or local maintenance
   actions and `blocked_items` for anything requiring human login, platform
   confirmation, or a separate code-change run.

## Safety Rules

- Redact keys containing `token`, `secret`, `password`, `authorization`,
  `api_key`, `csrf`, `cookie`, and `set-cookie`.
- Never log QR image data, webhook URLs, browser session payloads, or extension
  API keys.
- Treat login expiry as a blocked item with a minimal operator action, not as a
  condition to bypass.
- Treat publish=false upload jobs as non-publishing work; do not convert them
  into real publish actions.
- Use ForWin MCP tools for generation task truth when matching tools exist.
- Prefer `task_pause` over process kill/restart when a generation stop is
  required by a later manual intervention.

## Testing

Automated tests:

- Unit test redaction on nested dictionaries and lists.
- Unit test supervisor output always includes the required top-level fields.
- Unit test classifier behavior for failed upload jobs, login-required
  publisher state, disabled Codex Bridge, and active generation conflicts.
- Unit test command/API failures produce blocked items rather than tracebacks
  with sensitive data.

Manual verification:

- Run one supervisor pass against local production endpoints.
- Confirm JSONL contains the required fields.
- Confirm login-required publisher state creates a blocked item without cookies
  or QR data.
- Confirm disabled Codex Bridge is represented as a health finding.
- Install or dry-run the systemd timer and confirm it triggers one service run.

## Rollout

1. Implement and test the supervisor script in the GitHub source checkout.
2. Document the systemd unit files and installation commands in operations docs.
3. Deploy through the existing GitHub deploy sync path.
4. Install the timer on 150.
5. Run one manual supervisor pass and inspect the JSONL output.
6. Leave the existing read-only `monitor_forwin_runtime.py` available for
   focused runtime windows.

## Current Login State Note

As of this design, the production publisher browser and extension heartbeat are
healthy, but publisher platform login may still require human scan/confirmation.
The supervisor should report that state as a blocked item and should not attempt
to resolve it by reading or replaying cookies.
