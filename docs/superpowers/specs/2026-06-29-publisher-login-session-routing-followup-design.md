# Publisher Login Session Routing Follow-Up Design

> Superseded on 2026-06-30 for heartbeat QR behavior: ordinary extension
> heartbeats must not capture QR images or notify Discord when a publisher login
> page is visible. QR notification is allowed only for an active
> operator-requested login session.

## Context

The production ForWin publisher browser uses one persistent Chromium profile for
both Fanqie and Qidian. After the user scanned the fresh Discord QR codes, the
publisher platform API briefly reported both platforms as connected. A safe
chapter upload probe still failed with `publish=false` for both platforms:

- Fanqie failed from `https://fanqienovel.com/main/writer/login`, while an
  authenticated Fanqie writer dashboard tab was also open in the same profile.
- Qidian failed from `https://pcwrite.yuewen.com/authorh5/loginOut`, while a
  top-level Qidian writer page and a chapter editor page were also open.

This shows two related gaps beyond the existing QR extraction design:

1. QR notification dedupe can suppress fresh QR delivery for a long-lived login
   session or unchanged login URL.
2. Upload and other business commands can be answered by the wrong tab or child
   frame, including login pages and `loginOut` iframes.

## Goals

- Ensure every operator-requested Discord login QR notification is captured
  freshly at send time, with only short throttling to avoid spam.
- Keep one persistent publisher-browser profile for Fanqie and Qidian.
- Route upload, comment sync, audit sync, cover upload, and editor inspection
  commands to the top frame of the selected tab.
- Keep QR extraction frame-aware, because Qidian and WeChat QR images can live
  in child frames.
- Prefer authenticated dashboard/editor tabs over login tabs when inspecting
  publisher state.
- Prevent `pcwrite.yuewen.com/authorh5/loginOut` from being treated as an
  authenticated Qidian page.
- Preserve the existing safety posture: upload probes use `publish=false`,
  do not replay cookies, do not log QR images, and do not bypass platform
  login, captcha, MFA, or risk controls.

## Non-Goals

- Do not automate real publishing as part of this fix.
- Do not split Fanqie and Qidian into separate browser profiles.
- Do not store QR image hashes, cookies, session values, tokens, passwords,
  webhook URLs, or verification codes in logs or diagnostics.
- Do not introduce broad all-host extension permissions when narrower login
  frame matches are enough.

## Approaches Considered

### Recommended: Targeted Routing And Fresh QR Throttle

Fix the extension routing logic in the smallest reliable places:

- Replace one-shot QR notification dedupe with per-platform/tab throttling.
- Capture QR images immediately before each Discord notification.
- Send business commands with Chrome `tabs.sendMessage(..., { frameId: 0 })`.
- Keep QR extraction's explicit child-frame fallback.
- Inspect all candidate platform tabs and return authenticated pages before
  login pages.

This directly addresses the observed production failures and keeps the blast
radius limited to extension/background/controller behavior and tests.

### Alternative: Aggressive Browser Cleanup

Automatically close any login tab when an authenticated tab exists for the same
platform. This can reduce confusion, but it is brittle: closing a platform tab
can interrupt an in-progress human login or platform redirect, and it does not
fix child-frame message routing.

### Alternative: Separate Browser Profiles Per Platform

Run one browser profile for Fanqie and one for Qidian. This would isolate login
state and iframe side effects, but it violates the desired one-browser workflow
and increases deployment complexity. It should remain a fallback only if a
platform creates an unavoidable session conflict.

## Design

### Fresh QR Notifications

`PublisherExtensionController.maybeNotifyLoginQr` will no longer use a
per-session permanent `loginQrNotificationAttempted` flag. Instead it will
track a `loginQrLastNotifiedAtMs` timestamp and an in-flight flag.

Behavior:

- If a login page is visible and no notification is in flight, capture a QR
  image now.
- Skip only when the last successful send for the same session happened inside
  a 60-second throttle window.
- If capture is empty, rejected, or fails, do not update the successful-send
  timestamp, so the next heartbeat can retry.
- Heartbeat-created pseudo sessions will share the same throttle semantics and
  will not dedupe permanently on `platform:tabId:currentUrl`.
- The backend continues to receive the image data URL only for dispatch; logs
  and diagnostics store only platform, tab id, current URL host/path, phase,
  source, and image data length.

This makes a newly sent Discord message correspond to a newly captured QR image,
not an old capture or a permanently deduped login URL.

### Top-Frame Business Commands

Background messaging should distinguish command classes:

- Business commands use top frame only:
  - `run-upload`
  - `run-comment-sync`
  - `run-audit-sync`
  - `prepare-cover-upload`
  - editor inspection and trusted-input helpers
  - draft verification commands
- QR extraction may target top frame first and then selected child frames.

`sendPlatformAgentMessage` should accept message options and default business
commands to `{ frameId: 0 }`. Existing explicit frame-targeting for QR extraction
stays separate through `sendLoginQrExtractionMessage`.

This prevents a Qidian `authorh5/loginOut` iframe from returning "still on the
login page" while the top-level tab is authenticated or already in an editor.

### Candidate Tab Selection

`inspectPlatformState` currently returns the first ready candidate by active
state or tab id. It will inspect ready candidates and rank outcomes:

1. Authenticated editor/workflow page.
2. Authenticated dashboard/status page.
3. Non-authenticated but non-login platform page.
4. Known login page.
5. No usable inspection.

If an authenticated Fanqie dashboard tab exists, a stale Fanqie login tab should
not mark the platform disconnected. If a Qidian dashboard contains a loginOut
iframe, that iframe should not override an authenticated top-level page.

### Login URL Classification

Qidian login state should treat `pcwrite.yuewen.com/authorh5/loginOut` as login
visible or unauthenticated, not authenticated by virtue of matching
`/authorh5/`. Platform agent `inspectLoginState` should only use `/authorh5/`
as an authenticated hint when the URL is not a known logout/login path and the
page text contains writer/workflow signals.

Fanqie should treat `/main/writer/login` as login visible even when another
dashboard tab is logged in.

## Implementation Touchpoints

Expected code changes are limited to the publisher extension and tests:

- `browser_extension/forwin-publisher/lib/controller.js`
  - Replace permanent QR notification dedupe in `maybeNotifyLoginQr`.
  - Keep QR capture/Discord notification in active operator-requested login
    sessions only; ordinary heartbeat login-page detection should record
    `login-required` without creating pseudo sessions.
- `browser_extension/forwin-publisher/background.js`
  - Add top-frame options to `sendPlatformAgentMessage` or its business-command
    callers.
  - Keep `sendLoginQrExtractionMessage` able to target child frame IDs.
  - Change `inspectPlatformState` to inspect and rank candidate tabs.
  - Update login URL classification for Qidian `authorh5/loginOut`.
- `browser_extension/forwin-publisher/platform-agent.js`
  - Tighten Qidian `inspectLoginState` so `authorh5/loginOut` is
    unauthenticated.
  - Keep `runUpload` login-page rejection, but make it run in the top frame for
    business commands.
- `browser_extension/forwin-publisher/tests/`
  - Add the failing tests listed in this spec before production code changes.

No backend database schema, API contract, or platform credential storage changes
are expected.

## Data Flow

1. Heartbeat inspects platform tabs without reading cookies or session stores.
2. If login is visible during ordinary heartbeat, the controller records
   `login-required` and skips QR capture/Discord notification. If the login tab
   belongs to an active operator-requested login session, the controller captures
   a QR image at that moment and posts it to the backend unless the short
   throttle window is active.
3. When an upload job is claimed, the controller opens or reuses the platform
   workflow page.
4. Background waits for a runnable platform tab and sends `run-upload` only to
   frame `0`.
5. The platform agent in the top frame prepares the editor, fills content, and
   returns success or a precise error.
6. The controller updates the job result and heartbeat state without allowing
   child login frames to poison platform state.

## Testing

Automated tests should fail before the implementation and pass after it:

- Controller test: a login session can send a second QR notification after the
  throttle window, and the second send triggers a fresh capture call.
- Controller test: failed or empty QR capture does not update the send
  timestamp and remains retryable.
- Controller/background wiring test: `run-upload` and other business commands
  call `tabs.sendMessage` with `{ frameId: 0 }`.
- Background test: QR extraction still sends to child frame IDs when top-frame
  extraction fails.
- Background test: `inspectPlatformState` prefers an authenticated dashboard or
  editor over a stale login tab.
- Platform-agent test: Qidian `authorh5/loginOut` is not authenticated.
- Regression test: a Qidian tab with a loginOut iframe cannot cause
  `run-upload` to return the iframe login failure when the top frame is the
  target.
- Connection-state test: when strict preferred-client mode is disabled, a
  recently connected latest client can make the platform connected even if the
  configured preferred client is stale or currently reports `login-required`.

Manual/production verification:

- Deploy the updated publisher browser image.
- Open `/publishers` in the production publisher browser.
- Confirm `/api/publishers/platforms` shows Fanqie and Qidian connected through
  the preferred client, or through a recent fallback client when strict
  preferred-client mode is disabled.
- Trigger a fresh Fanqie QR from an explicit operator login session after
  expiration and confirm Discord receives a new QR message from a new capture
  attempt.
- Run safe upload probes with `publish=false` and `create_if_missing=false`
  against existing test books for both Fanqie and Qidian.
- Confirm failed probes, if any, report the top-frame URL and precise editor
  problem rather than stale login pages or loginOut iframes.

## Rollout

1. Add failing extension/controller/background/platform-agent tests.
2. Implement QR throttling, top-frame business messaging, state ranking, and
   Qidian loginOut classification.
3. Run the publisher extension test suite and targeted Python publisher tests.
4. Build and deploy the publisher browser image through the GitHub-to-swarm
   deployment path.
5. Verify health, heartbeat, platform state, and safe upload probes.
6. Document any remaining human login action with platform and current page
   only, never with QR image data or credentials.

## Safety Notes

All production upload validation remains non-publishing unless the user later
explicitly approves a `publish=true` platform test after quota and platform
constraints are confirmed. The fix must not read, print, copy, or persist
cookies, tokens, passwords, QR image content, Discord webhooks, or API keys.
