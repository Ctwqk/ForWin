# Production Publisher Session Login Path Design

Date: 2026-07-01

## Purpose

Fix the production publisher-login operating model after Fanqie and Qidian have
both been logged in inside the shared production publisher browser. The
production automation path should preserve, restore, verify, and monitor that
browser session state. It should not keep trying to solve routine login expiry
by sending Discord QR messages.

This is one slice of the larger production-readiness goal. It comes before
publish-false upload-chain smoke, long-form generation, quota-confirmed
publish=true verification, and the two-hour supervisor hardening because those
workflows depend on a stable, non-noisy publisher-browser login path.

## Current Evidence

- Source of truth is GitHub `Ctwqk/ForWin` on `master`.
- `10.0.0.246` is retired/unavailable and must not remain a source or
  documentation dependency.
- Production app deployment output lives on
  `10.0.0.126:/Users/magi1/ForWin-swarm`.
- Production app and MCP endpoints are `http://10.0.0.126:8899` and
  `http://10.0.0.126:8896`.
- Production data services remain on `10.0.0.150`.
- Current production services include the shared
  `forwin-publisher-browser-swarm` service.
- The deployed path already stores publisher browser sessions in the backend,
  restores them into the Linux extension browser on startup, and verifies login
  state through page evidence plus extension heartbeat.
- Runtime config intentionally ignores legacy service-level Discord login QR
  webhook environment variables.
- A separate one-shot Discord QR handoff still exists for manual incidents, but
  the recent operator experience shows it is too noisy and unreliable to be a
  normal production-login path.

## Goals

1. Make backend-synced browser session restore plus heartbeat verification the
   only documented production automation path for publisher login continuity.
2. Keep Fanqie and Qidian in one shared persistent production publisher browser
   profile unless a verified platform limitation later proves that impossible.
3. Treat manual login as an action performed in the production publisher
   browser profile, not in an unrelated local browser and not through repeated
   Discord QR pushes.
4. Ensure baseline, smoke, and supervisor scripts report login expiry as a
   structured human action instead of generating or forwarding QR images.
5. Keep all reports and docs free of cookies, tokens, session secrets, QR image
   data, API keys, passwords, and webhook URLs.
6. Preserve the ability to diagnose login loss through safe evidence: platform
   id, safe URL, page-state label, heartbeat freshness, connected booleans, and
   redacted error text.

## Non-Goals

- Do not implement upload-chain smoke in this slice.
- Do not run long-form generation in this slice.
- Do not create test books, upload chapters, or publish content in this slice.
- Do not bypass QR scan, captcha, MFA, platform risk control, login
  confirmation, or account verification.
- Do not split Fanqie and Qidian into separate browser profiles.
- Do not remove all QR-related code blindly if tests or emergency docs still
  need a migration period. The production automation contract is the important
  boundary.

## Approaches Considered

### Recommended: Session Restore As The Production Path

Keep the current session-sync architecture and make it explicit in docs and
tests. The extension saves authenticated session summaries and encrypted cookie
payloads to the backend. The publisher-browser startup restores those sessions
into the same Linux Chrome profile, opens `/publishers`, and waits for
heartbeat. Operators verify readiness with the production baseline script, which
checks real Fanqie and Qidian pages as well as `/api/publishers/platforms`.

When login expires, scripts emit `publisher_login_required` with the smallest
safe human action: open the production publisher browser profile, complete the
platform login there, then rerun the baseline. No routine script sends a QR code
or a login-success message to Discord.

This is the recommended path because it matches the working deployed behavior,
keeps one browser profile, avoids webhook noise, and leaves platform
authentication with the human when the platform requires it.

### Alternative: Delete The One-Shot QR Flow Now

Remove the one-shot script, backend route, and all QR notification plumbing.
This is cleaner, but it has a larger blast radius and could slow incident
response while the new session path is still being exercised. It is better as a
later cleanup after production checks show the session path is enough.

### Alternative: Replace Discord With Local QR Files

Generate QR images into a local operator-only directory instead of Discord. This
avoids webhook spam, but it keeps QR extraction as part of the operational
model. It also continues to make automation responsible for a platform login
artifact that expires quickly and can trigger repeated human work. It should
not be the production default.

## Design

### Supported Login Lifecycle

1. The production publisher-browser service starts one persistent browser
   profile with the ForWin publisher extension loaded.
2. Startup qualifies the profile with backend URL, extension API key, preferred
   client id when configured, and QR notification settings disabled.
3. Startup restores the latest backend-synced Fanqie and Qidian sessions into
   the same browser context when a CDP endpoint is available.
4. The extension opens or keeps `/publishers` and platform dashboard evidence.
5. Heartbeats sync platform state and safe browser-session summaries back to
   the backend.
6. The baseline verifier opens real platform dashboard URLs in the same browser
   context, waits for heartbeat convergence, and accepts login only when API
   state and page evidence agree.

### Login Expiry Flow

If the browser lands on a login, captcha, MFA, scan-confirmation, or platform
risk-control page, automation stops at a blocked item:

- `kind`: `publisher_login_required`
- `platform`: `fanqie` or `qidian`
- `page_state`: safe state label such as `login_visible`
- `current_url`: safe URL without sensitive query payloads
- `human_action`: log in inside the production publisher browser profile and
  rerun the baseline command

The blocked item must not include a QR image, login token, cookie value,
authorization header, session secret, platform account detail, or webhook URL.

### Discord QR Policy

The production policy is:

- Legacy service-level Discord login webhook env stays ignored.
- Extension QR notifications stay disabled in qualified production profiles.
- Baseline, upload-chain smoke, supervisor, deploy scripts, and recurring jobs
  must not call `/api/publishers/extension/login-qr`.
- Baseline, upload-chain smoke, supervisor, deploy scripts, and recurring jobs
  must not run `scripts/start_publisher_login_qr_one_shot.py`.
- Existing one-shot QR tooling may remain only as an explicitly named
  emergency/manual path during this transition. It is not part of normal
  production login automation, must not be scheduled, and must not be invoked by
  monitoring.

### Documentation Changes

Update operational docs so the main login path is:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

If it reports `publisher_login_required`, the documented action is to complete
login in the production publisher browser profile and rerun the same command.
Docs should not tell operators to send QR codes to Discord for routine login
expiry. Any retained one-shot QR section should be moved behind an emergency
heading and marked unsupported for automation.

### Verification Surfaces

The fixed path is verified through:

- Swarm service state for `forwin-publisher-browser-swarm`.
- App health at `http://10.0.0.126:8899/health`.
- MCP health at `http://10.0.0.126:8896/health`.
- `GET /api/publishers/platforms`.
- `GET /api/publishers/browser-sessions/{platform}` summaries.
- Key-protected extension heartbeat status when the key is available.
- Browser page evidence for:
  - Fanqie: `https://fanqienovel.com/main/writer/`
  - Qidian: `https://write.qq.com/portal/dashboard`

Cookie presence alone is not enough. Page evidence alone is not enough. The
accepted state is API connected plus real page dashboard evidence after
heartbeat convergence.

### Error Handling

- Missing browser session restore data is a degraded state, not a reason to
  send QR messages.
- Authenticated page evidence with stale API state becomes
  `state_sync_mismatch` after a heartbeat wait.
- Login page evidence becomes `publisher_login_required`.
- CDP/browser inspection failure becomes `browser_unreachable`.
- Discord webhook configuration in shared production remains a policy failure.
- Repeated QR notification attempts from any production automation path are a
  regression.

## Testing

Focused automated coverage should include:

- Qualified Linux extension profiles keep `loginQrNotificationsEnabled=false`
  and `loginQrNotificationsAllowed=false`.
- Legacy Discord login webhook env values are ignored by runtime config.
- Baseline and smoke scripts do not invoke the login QR extension endpoint.
- Supervisor reports login expiry as `publisher_login_required` and does not
  schedule or call one-shot QR delivery.
- Session restore handles encrypted backend browser sessions without printing
  raw cookie values.
- Fanqie login-page cookies do not overwrite a previously verified restorable
  Fanqie session.
- Redaction covers cookies, tokens, API keys, QR image data, body payloads, and
  webhook-looking fields.

Production verification should record:

- deployed commit and image tags
- service replica state
- app and MCP health
- both platform connected states from `/api/publishers/platforms`
- both platform page evidence from the shared publisher browser
- absence of Discord webhook env policy violations
- absence of QR notification actions in `actions_taken`

## Acceptance Criteria

This slice is complete when:

1. README and operations docs describe session restore plus baseline
   verification as the production login continuity path.
2. Routine docs no longer instruct operators to send login QR codes to Discord
   for production login expiry.
3. Existing emergency one-shot QR documentation, if retained, is clearly marked
   manual-only and not supported for automation.
4. Focused tests prove runtime config, extension profile qualification,
   supervisor/baseline behavior, and redaction preserve the no-routine-Discord
   policy.
5. Production baseline reports Fanqie and Qidian connected in the same
   publisher browser profile, or reports only structured human login blockers
   without QR/webhook output.
6. No repository file, command output intended for reports, or final handoff
   contains cookies, tokens, passwords, QR image data, session secrets, API
   keys, or webhook URLs.

## Follow-Up

After this slice is implemented and verified, continue the broader objective in
separate specs or plans:

- publish-false upload-chain smoke on the now-stable browser session path
- platform quota and account-limit confirmation
- long-form generation stability experiment through ForWin MCP/operator tools
- quota-confirmed publish=true minimum real publish verification
- two-hour review-needed and publisher-intervention supervisor hardening
