# Publisher Login QR Extraction Design

## Context

The production publisher browser can open both supported login pages and the
backend can forward login QR images to Discord. The current Discord messages are
not usable enough because the extension often falls back to a full-page debugger
screenshot instead of sending the QR image itself.

Observed production behavior:

- Fanqie opens on the SMS-code login tab. The QR code appears only after the
  visible `扫码登录` tab is activated. Manual activation produces a `data:image/png`
  image around 158px square, but the extension message path does not currently
  activate the tab.
- Qidian shows the outer `write.qq.com` page, while the login UI and WeChat QR
  live inside nested frames such as `passport.yuewen.com` and
  `open.weixin.qq.com`. The current content-script match set only runs the
  platform agent in the top platform pages, so the main frame cannot read the QR
  element.
- Sending the login page URL is not a reliable substitute. These QR flows are
  tied to the browser session that is polling for login confirmation; opening a
  copied URL on another machine would usually log in that other browser instead
  of the production publisher browser.

## Goals

- Send a directly scannable QR image to Discord for Fanqie and Qidian whenever
  the production publisher browser needs scan login.
- Preserve the existing fallback screenshot path for diagnostics, but use it
  only after direct QR extraction fails.
- Keep all credentials, cookies, platform session tokens, and Discord webhook
  values out of logs, docs, storage diagnostics, and final reports.
- Keep using one persistent production publisher-browser profile for both
  platforms.

## Non-Goals

- Do not bypass platform login, captcha, MFA, or platform risk controls.
- Do not attempt to log in by replaying cookies or extracting platform session
  tokens.
- Do not treat copied login URLs as the primary login mechanism.
- Do not do real publishing as part of this QR extraction fix.

## Proposed Approach

### Fanqie

Improve the platform-agent scan-tab activation path:

- Search visible, compact controls whose text is exactly `扫码登录` or a short
  label containing it.
- Dispatch a more browser-like pointer/mouse/click sequence, not only
  `element.click()`, so React/Arco tab handlers receive the same events as a
  user click.
- After activation, wait conditionally until a QR candidate appears instead of
  sleeping for a fixed short delay.
- Extract the resulting `img` data URL directly when it is visible and square.

### Qidian

Make QR extraction frame-aware:

- Extend extension permissions and content-script matches to include the login
  frames required for the Qidian/WeChat login flow, especially
  `passport.yuewen.com` and `open.weixin.qq.com`.
- Enable the platform agent in all relevant frames.
- When the background capture path cannot get a QR from the top frame, enumerate
  frames with Chrome debugger APIs and send `extract-login-qr-image` to the
  relevant frame IDs.
- Prefer the first visible image/canvas QR candidate returned from any frame.

### Fallback And Diagnostics

- If direct extraction succeeds, store only sanitized status metadata such as
  platform, phase, source, dimensions, and image data URL length.
- If direct extraction fails, send the existing full-page screenshot but mark the
  source clearly as a fallback.
- Include the page URL host/path in diagnostics, not query secrets.
- Optionally include a QR image source URL in Discord metadata only when it is a
  public QR image endpoint and does not include sensitive platform credentials.

## Data Flow

1. Heartbeat or login-session inspection detects a platform login page.
2. Background asks the platform agent to extract a QR image from the tab.
3. The platform agent activates scan-login UI if needed and returns a data URL.
4. If the top frame fails, background asks matching child frames for a QR image.
5. Background posts the QR data URL to the existing backend login-QR endpoint.
6. Backend sends the image attachment to Discord through the configured secret
   webhook.
7. Extension records sanitized notification status for operator inspection.

## Testing

Automated tests:

- Add or update platform-agent tests for Fanqie scan-tab activation and delayed
  QR appearance.
- Add background/controller tests that verify direct QR extraction is preferred
  over debugger screenshot fallback.
- Add manifest or wiring tests confirming relevant Qidian/WeChat login frames
  are covered without requesting broad all-host permissions.

Manual/production verification:

- Deploy the updated extension/browser image.
- Open Qidian and Fanqie login pages in the production publisher browser.
- Trigger heartbeat.
- Confirm Discord receives QR-focused images with `source` indicating direct
  extraction, not `debugger-screenshot`.
- Confirm `/api/publishers/platforms`,
  `/api/publishers/extension/heartbeat-status`, browser-session summary, and
  actual page state after human scan.

## Risks

- Platform markup can change, so selectors should be behavior-based and
  diagnostic events should reveal why extraction failed.
- Cross-origin frames may restrict DOM access from the parent. Running the
  content script inside permitted frames and addressing messages by frame ID
  avoids parent-frame DOM access.
- QR links can expire quickly. Discord must receive the QR image promptly and the
  operator may need to request a fresh QR after expiration.

## Approval

This design is approved for implementation once the user confirms the written
spec. Implementation should remain limited to QR extraction, QR notification
diagnostics, tests, and deployment verification.
