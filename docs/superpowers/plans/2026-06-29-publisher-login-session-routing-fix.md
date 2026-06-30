# Publisher Login Session Routing Fix Implementation Plan

> Superseded on 2026-06-30 for heartbeat QR behavior: do not implement heartbeat
> pseudo sessions that capture QR images or notify Discord. Ordinary heartbeat
> login-page detection must only report `login-required`; QR notification is
> reserved for active operator-requested login sessions.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make publisher QR notifications fresh and prevent stale login tabs or child login frames from handling upload/session business commands.

**Architecture:** Keep the fix inside the publisher extension. The controller owns QR notification throttling, the background worker owns tab/frame routing and platform-state selection, and the platform agent owns per-page login classification. Backend APIs and database schemas stay unchanged.

**Tech Stack:** JavaScript ES modules, Chrome extension APIs, Node.js built-in test runner, ForWin publisher browser deployment.

---

## File Structure

- Modify `browser_extension/forwin-publisher/lib/controller.js`
  - Replace permanent QR dedupe with a 60-second successful-send throttle.
  - Keep QR notification on active login sessions only; ordinary heartbeats must
    not create pseudo sessions that notify Discord.
- Modify `browser_extension/forwin-publisher/background.js`
  - Route business commands to `{ frameId: 0 }`.
  - Keep login QR extraction able to target child frame IDs.
  - Rank inspected platform tabs before returning platform state.
  - Treat Qidian `authorh5/loginOut` as a login URL.
- Modify `browser_extension/forwin-publisher/platform-agent.js`
  - Classify Qidian `authorh5/loginOut` as unauthenticated.
- Modify `browser_extension/forwin-publisher/tests/controller.test.js`
  - Add red/green tests for QR resend after throttle and retry after empty capture.
- Modify `browser_extension/forwin-publisher/tests/background-wiring.test.js`
  - Add wiring tests for top-frame business commands and ranked platform inspection.
- Modify `browser_extension/forwin-publisher/tests/platform-agent-wiring.test.js`
  - Add a wiring test for Qidian `loginOut` classification.

### Task 1: Controller QR Freshness

**Files:**
- Modify: `browser_extension/forwin-publisher/tests/controller.test.js`
- Modify: `browser_extension/forwin-publisher/lib/controller.js`

- [ ] **Step 1: Write the failing resend-after-throttle test**

Add this test near the existing login QR notification tests:

```js
test('controller sends a fresh login QR again after the throttle window', async () => {
  let captureCalls = 0;
  let nowMs = 1_000_000;
  const { controller, loginQrNotifications } = makeController({
    nowMs: () => nowMs,
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,cXI${captureCalls}=`,
        source: `image-${captureCalls}`,
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  await controller.maybeNotifyLoginQr(session, inspection);
  await controller.maybeNotifyLoginQr(session, inspection);
  nowMs += 60_001;
  await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(captureCalls, 2);
  assert.equal(loginQrNotifications.length, 2);
  assert.equal(loginQrNotifications[0].image_data_url, 'data:image/png;base64,cXI1=');
  assert.equal(loginQrNotifications[1].image_data_url, 'data:image/png;base64,cXI2=');
});
```

- [ ] **Step 2: Run the failing controller test**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/controller.test.js --test-name-pattern "fresh login QR again"
```

Expected: FAIL because the controller still uses permanent `loginQrNotificationAttempted`.

- [ ] **Step 3: Implement the minimal controller throttle**

In `PublisherExtensionController`:

```js
const LOGIN_QR_NOTIFICATION_THROTTLE_MS = 60_000;

function defaultNowMs() {
  return Date.now();
}
```

Use `this.deps.nowMs || defaultNowMs` in `maybeNotifyLoginQr`, skip when
`nowMs - session.loginQrLastNotifiedAtMs < LOGIN_QR_NOTIFICATION_THROTTLE_MS`,
remove the permanent `session.loginQrNotificationAttempted` gate, and set
`session.loginQrLastNotifiedAtMs = nowMs` only after `backend.notifyLoginQr`
returns without throwing.

- [ ] **Step 4: Run the controller QR tests**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/controller.test.js --test-name-pattern "login QR|fresh login QR"
```

Expected: PASS for QR-related tests.

### Task 2: Heartbeat QR Suppression Semantics

**Files:**
- Modify: `browser_extension/forwin-publisher/tests/controller.test.js`
- Modify: `browser_extension/forwin-publisher/lib/controller.js`

- [ ] **Step 1: Write the heartbeat suppression test**

Add or keep a regression test proving that repeated ordinary heartbeat
inspection of a visible login page does not call `captureLoginQrImage` and does
not call `notifyLoginQr`. Active login-session tests should separately prove
that explicit operator login flows can still send QR notifications.

- [ ] **Step 2: Run the failing heartbeat test**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/controller.test.js --test-name-pattern "fresh login QR after throttle"
```

Expected: FAIL because `heartbeatLoginQrNotificationKeys` permanently dedupes by URL.

- [ ] **Step 3: Implement shared heartbeat throttle**

Replace `heartbeatLoginQrNotificationKeys` with `heartbeatLoginQrNotificationSessions`, a `Map` keyed by `platform:tabId`. Reuse the same pseudo-session object for repeated heartbeats:

```js
const key = `${platformId}:${tabId}`;
let pseudoSession = this.heartbeatLoginQrNotificationSessions.get(key);
if (!pseudoSession) {
  pseudoSession = { platformId, popupTabId: tabId, lastUrl: currentUrl };
  this.heartbeatLoginQrNotificationSessions.set(key, pseudoSession);
}
pseudoSession.lastUrl = currentUrl;
return this.maybeNotifyLoginQr(pseudoSession, { ...inspection, currentUrl, authenticated: false, loginVisible: true });
```

Clear map entries for a platform when the page is no longer login-visible.

- [ ] **Step 4: Run the controller tests**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/controller.test.js
```

Expected: PASS.

### Task 3: Top-Frame Business Messaging

**Files:**
- Modify: `browser_extension/forwin-publisher/tests/background-wiring.test.js`
- Modify: `browser_extension/forwin-publisher/background.js`

- [ ] **Step 1: Write the failing wiring test**

Add this test to `background-wiring.test.js`:

```js
test('background routes platform business commands to the top frame', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /const\s+TOP_FRAME_MESSAGE_OPTIONS\s*=\s*\{\s*frameId:\s*0\s*\}/);
  assert.match(source, /sendPlatformAgentMessage[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
  assert.match(source, /runUploadCommand[\s\S]*sendPlatformAgentMessage\([\s\S]*'run-upload'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
  assert.match(source, /runCommentSyncCommand[\s\S]*sendPlatformAgentMessage\([\s\S]*'run-comment-sync'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
});
```

- [ ] **Step 2: Run the failing wiring test**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/background-wiring.test.js --test-name-pattern "top frame"
```

Expected: FAIL because business commands do not pass top-frame options.

- [ ] **Step 3: Implement top-frame options**

In `background.js`, define:

```js
const TOP_FRAME_MESSAGE_OPTIONS = { frameId: 0 };
```

Change `sendPlatformAgentMessage` signature to:

```js
async function sendPlatformAgentMessage(tabId, action, payload, timeoutMs = 12000, options = TOP_FRAME_MESSAGE_OPTIONS) {
  return Promise.race([
    wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action,
      payload,
    }, options),
    new Promise((resolve) => {
      globalThis.setTimeout(() => {
        resolve({
          ok: false,
          error: '平台页面执行超时。',
          errorCode: 'platform-agent-timeout',
          currentUrl: '',
          resultPayload: {
            phase: 'message-timeout',
            action,
          },
        });
      }, timeoutMs);
    }),
  ]);
}
```

Keep `sendLoginQrExtractionMessage` unchanged so QR extraction can pass child
`frameId` values.

- [ ] **Step 4: Run background wiring tests**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/background-wiring.test.js
```

Expected: PASS.

### Task 4: Platform State Ranking And Qidian LoginOut

**Files:**
- Modify: `browser_extension/forwin-publisher/tests/background-wiring.test.js`
- Modify: `browser_extension/forwin-publisher/tests/platform-agent-wiring.test.js`
- Modify: `browser_extension/forwin-publisher/background.js`
- Modify: `browser_extension/forwin-publisher/platform-agent.js`

- [ ] **Step 1: Write failing wiring tests**

Add to `background-wiring.test.js`:

```js
test('background ranks authenticated platform inspections before login tabs', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+rankPlatformInspection\s*\(/);
  assert.match(source, /authenticated[\s\S]*return\s+100/);
  assert.match(source, /loginVisible[\s\S]*return\s+10/);
  assert.match(source, /inspectedCandidates\.sort/);
});

test('background treats qidian authorh5 loginOut as a known login url', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /authorh5\/loginOut/);
  assert.match(source, /isPlatformLoginUrl[\s\S]*authorh5\/loginOut/);
});
```

Add to `platform-agent-wiring.test.js`:

```js
test('platform agent does not authenticate qidian loginOut authorh5 pages', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isQidianLoginOutUrl\s*\(/);
  assert.match(source, /authorh5\/loginOut/);
  assert.match(source, /authenticated\s*=\s*!qidianLoginOut/);
});
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/background-wiring.test.js tests/platform-agent-wiring.test.js --test-name-pattern "loginOut|authenticated platform"
```

Expected: FAIL because ranking and `loginOut` classification are missing.

- [ ] **Step 3: Implement state ranking and loginOut classification**

In `background.js`, make `isPlatformLoginUrl('qidian', url)` return true for:

```js
value.includes('pcwrite.yuewen.com') && value.includes('/authorh5/loginOut')
```

Refactor `inspectPlatformState` to inspect all ready candidates, push usable
results into `inspectedCandidates`, sort by `rankPlatformInspection`, and return
the strongest result.

In `platform-agent.js`, add:

```js
function isQidianLoginOutUrl(url) {
  return String(url || '').includes('pcwrite.yuewen.com') && String(url || '').includes('/authorh5/loginOut');
}
```

Then require `!qidianLoginOut` before setting Qidian `authenticated`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd browser_extension/forwin-publisher
node --test tests/background-wiring.test.js tests/platform-agent-wiring.test.js
```

Expected: PASS.

### Task 5: Full Verification, Commit, Deploy, And Production Probe

**Files:**
- Verify all modified files.
- Commit implementation changes.
- Deploy through the existing GitHub-to-swarm path.

- [ ] **Step 1: Run full publisher extension tests**

Run:

```bash
cd browser_extension/forwin-publisher
npm test
```

Expected: PASS.

- [ ] **Step 2: Run targeted Python publisher tests**

Run:

```bash
cd /Users/magi1/ForWin-source-github
.venv/bin/python -m pytest tests/test_publisher_runtime_upload_jobs.py tests/test_publisher_runtime_bindings.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add browser_extension/forwin-publisher
git commit -m "Fix publisher login session routing"
```

- [ ] **Step 4: Push and deploy**

Run the repository's existing deploy/sync path from GitHub master to
`/Users/magi1/ForWin-swarm`. After deploy, confirm:

```bash
python3 scripts/check_codex_operator_ready.py --strict
curl -fsS http://10.0.0.126:8899/health
curl -fsS http://10.0.0.126:8896/health
```

Expected: all commands exit 0.

- [ ] **Step 5: Verify publisher browser state**

Run:

```bash
python3 scripts/monitor_forwin_runtime.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-url http://10.0.0.126:8896 \
  --duration-seconds 8 \
  --interval-seconds 4 \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian
```

Expected: monitor reports both platforms connected through the same preferred
publisher browser client.

- [ ] **Step 6: Re-run safe upload probes**

Create `publish=false`, `create_if_missing=false`, cover-disabled upload jobs
for the existing Fanqie and Qidian test books and poll them to terminal state.

Expected: upload commands are not answered by stale login pages or
`authorh5/loginOut` iframes. If a platform still needs human scan, record only
the platform and current page; do not log cookies, QR image content, tokens, or
webhooks.
