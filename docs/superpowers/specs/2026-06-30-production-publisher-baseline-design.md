# Production Publisher Baseline Design

Date: 2026-06-30

## Purpose

Bring the first slice of the larger ForWin production readiness goal into a
repeatable, evidence-based baseline: production services must be healthy, the
MCP gateway must be reachable, the durable workers must be deployed, the shared
publisher browser must be running, and Fanqie/Qidian login state must be
verified through both backend state and real browser pages.

This design deliberately covers only the production runtime baseline and
publisher login-state convergence. Publishing jobs, real content upload,
long-form generation experiments, and the two-hour supervisor are separate
follow-up specs because they have different safety boundaries and acceptance
evidence.

## Current Evidence

- Production source of truth is GitHub `Ctwqk/ForWin` on `master`; the old
  `10.0.0.246` source path is retired/unavailable and must not be used as a
  deployment or coding dependency.
- `10.0.0.126:/Users/magi1/ForWin-swarm` is a deploy output, not the long-lived
  coding workspace.
- Production data services live on `10.0.0.150`; ForWin production should not
  start local Postgres, Qdrant, or MinIO on `126`.
- Production currently runs the Swarm services
  `forwin-app-swarm`, `forwin-mcp-swarm`,
  `forwin-generation-worker-swarm`, `forwin-publisher-worker-swarm`,
  `forwin-outbox-worker-swarm`, and `forwin-publisher-browser-swarm`.
- Discord publisher login alerts are intentionally disabled in shared
  production. The baseline check must preserve that state.

## Goals

1. Produce one operator command or script that can verify the production
   baseline without mutating ForWin project/task/chapter state.
2. Verify service health across app, MCP, generation worker, publisher worker,
   outbox worker, and publisher browser.
3. Verify publisher browser heartbeat and preferred-client state.
4. Verify Fanqie and Qidian using one shared persistent publisher browser
   profile.
5. Treat page-level login evidence as authoritative over cookie presence.
6. Emit a redacted structured result that can be archived in logs or attached to
   later runtime reports.
7. Convert platform login, MFA, captcha, or risk-control barriers into minimal
   human actions instead of attempting bypasses.

## Non-Goals

- Do not create projects, run Genesis, generate chapters, upload chapters, or
  publish content.
- Do not create test books or consume publisher platform quotas.
- Do not read, print, copy, or persist cookie values, tokens, passwords,
  session secrets, QR image data, API keys, or Discord webhook URLs.
- Do not trigger Discord login QR delivery.
- Do not bypass captcha, MFA, publisher risk controls, or account verification.
- Do not use raw database reads for project, Genesis, task, chapter, or
  WorldModel truth when an MCP tool exists. Publisher runtime diagnostics may
  use existing API surfaces and redacted browser checks.

## Recommended Approach

Use a production baseline verifier with a login-state convergence pass.

The verifier should be read-only with respect to business state. It may open or
keep platform dashboard tabs in the existing publisher browser because the
extension needs page evidence to report accurate heartbeat state. It must not
open an operator login flow, capture QR images, call login QR notification paths,
or replay cookie values outside the already configured browser/session restore
mechanism.

This approach is stricter than a cookie-only check and smaller than a full
publishing-chain smoke test. It directly addresses two observed failure modes:
Qidian can be logged in on the page while backend state lags until heartbeat
receives page evidence, and Fanqie can have strong-looking cookies while the
site still redirects the browser to its login page.

## Components

### Baseline Orchestrator

A repo script should coordinate the checks and emit one JSON object. It should
accept production defaults matching the current deployment:

- API base: `http://10.0.0.126:8899`
- MCP health URL: `http://10.0.0.126:8896/health`
- MCP URL: `http://10.0.0.126:8896/mcp`
- Swarm manager access through the existing `infra-150-via-colima` path
- Colima profile: `swarmbridged`
- Expected platforms: `fanqie`, `qidian`

The script should also support overriding these values for local or staging
checks.

### Service Checker

The service checker reads Swarm service names, images, and replicas for the six
ForWin services. It verifies that each service has the expected replica count
and that app/MCP health endpoints respond. Worker services are checked through
Swarm task state rather than pretending they are HTTP services.

### Publisher API Checker

The publisher API checker reads:

- `GET /api/publishers/platforms`
- `GET /api/publishers/browser-sessions/{platform}`
- `GET /api/publishers/extension/heartbeat-status`

It records only safe fields such as platform id, connected booleans,
extension-online booleans, preferred-client state, recent heartbeat timestamps,
redacted errors, and cookie counts when already surfaced by the API. It must not
request or print raw cookie payloads.

### Publisher Browser Checker

The browser checker connects to the existing production publisher Chromium CDP
endpoint inside `forwin-publisher-browser-swarm`. For each platform it opens or
reuses the dashboard URL in the same browser context:

- Qidian: `https://write.qq.com/portal/dashboard`
- Fanqie: `https://fanqienovel.com/main/writer/`

It records safe evidence: final URL, title, whether the page appears to be a
dashboard, whether a login page or login controls are visible, and whether
inspection succeeded. It leaves useful dashboard tabs open when needed for the
extension heartbeat to converge. It closes only verifier-created tabs that are
not useful for heartbeat evidence.

### Convergence Loop

When page evidence and API state disagree, the verifier waits for at least one
extension heartbeat interval and re-reads `/api/publishers/platforms`. If the
page remains authenticated but the API remains disconnected, the result is
`state_sync_mismatch`. If the page is a login page or platform verification
screen, the result is `human_login_required`.

## Data Flow

1. Read Swarm service state from the production manager.
2. Check app and MCP health endpoints.
3. Locate the running publisher-browser container.
4. Restore backend-synced browser sessions through the existing restore script
   only if the browser profile has just restarted or the restore step is
   explicitly requested.
5. Read publisher API state.
6. Inspect Qidian and Fanqie dashboard URLs in the shared publisher browser.
7. Wait for heartbeat convergence when page and API evidence conflict.
8. Emit one redacted JSON result with `status`, `checked_at`, `services`,
   `health`, `publisher_browser`, `platforms`, `blocked_items`, and
   `actions_taken`.

## Status Model

The verifier should produce one of these top-level statuses:

- `ok`: all required services are healthy and both expected platforms are
  connected by API and page evidence.
- `degraded`: production services are healthy, but one or more non-mutating
  checks failed or a platform needs human login.
- `failed`: app, MCP, required worker service, publisher browser, or the
  verifier itself failed in a way that prevents a reliable baseline.

Platform-level status should be one of:

- `connected`: API and page evidence agree.
- `human_login_required`: page evidence shows login, captcha, MFA, or risk
  control.
- `state_sync_mismatch`: page evidence shows authenticated dashboard but API
  remains disconnected after a heartbeat wait.
- `browser_unreachable`: CDP or page inspection failed.
- `unknown`: evidence is incomplete and no stronger classification is safe.

## Error Handling

The verifier should fail soft where possible and continue collecting independent
evidence. For example, a Fanqie login failure should not prevent service health,
MCP health, or Qidian checks from completing. A Swarm or CDP failure should be
reported with enough context to identify the failed layer, but without dumping
environment variables or browser/session data.

Human handoff items must include only:

- platform
- safe current URL
- observed page state
- minimal human action
- command to rerun after the action

For example, Fanqie redirecting to `/main/writer/login` should produce a
handoff asking the operator to log in to Fanqie in the production publisher
browser profile, then rerun the baseline verifier.

## Security and Privacy

The verifier must redact or omit:

- cookie values and raw cookie JSON
- session tokens
- password fields
- QR image data
- captcha/MFA data
- API keys and extension keys
- `FORWIN_PUBLISHER_SESSION_SECRET`
- Discord webhook URLs or secret file contents

It should explicitly check that shared production services do not expose unsafe
Discord webhook environment. `forwin-app-swarm` may use
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE` for the mounted secret file. Any
`FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL`, or any Discord webhook env on
browser/worker services, is a `failed` baseline condition.

## Verification Strategy

Automated tests should cover:

- Redaction of sensitive fields in verifier output.
- Classification of API/page agreement as `connected`.
- Classification of a login page as `human_login_required`.
- Classification of authenticated page plus disconnected API after heartbeat as
  `state_sync_mismatch`.
- Unsafe Discord webhook environment causing a failed baseline.
- The verifier continuing independent checks after one platform fails.

Manual production verification should run the new verifier once and confirm:

- all six Swarm services are present and healthy;
- app `/health` returns `ok`;
- MCP `/health` returns healthy with upstream OK;
- Qidian is connected by API and dashboard page evidence;
- Fanqie is either connected or produces a clear `human_login_required` item;
- no sensitive values appear in output;
- no Discord login notification path is triggered.

## Acceptance Criteria

This sub-project is complete when:

1. The design is implemented as a repeatable command or script in the repo.
2. The command emits a redacted JSON result.
3. The command can be run against production without publishing content or
   mutating project/task/chapter state.
4. Qidian and Fanqie are both verified through the same publisher browser
   profile.
5. Platform login failures are reduced to minimal human actions.
6. Relevant tests pass.
7. The README or operations docs describe the command, expected output, and
   rerun path after human login.

## Follow-Up Specs

After this baseline is implemented and verified, continue the larger readiness
goal with separate specs for:

1. Publisher preflight and publish-false upload job validation.
2. Long-form generation stability experiment through MCP/operator workflows.
3. Two-hour Codex/operator intervention supervision.
