# Publisher Login QR Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send directly scannable Fanqie and Qidian login QR images to Discord before falling back to full-page screenshots.

**Architecture:** Keep the existing extension login notification flow. Add a small pure helper for frame targeting, inject the platform agent into relevant Qidian/WeChat frames, make QR extraction ask those frames by `frameId`, and make the platform agent activate QR login UI with real pointer/mouse events before waiting for a QR candidate.

**Tech Stack:** Chrome MV3 extension, Node `node:test`, browser DOM APIs, Chrome `webNavigation`, existing backend QR notification endpoint.

---

## File Structure

- Create `browser_extension/forwin-publisher/lib/login-qr-frames.js`: pure URL/frame helpers used by the background service worker.
- Create `browser_extension/forwin-publisher/tests/login-qr-frames.test.js`: unit tests for frame targeting and URL sanitization.
- Modify `browser_extension/forwin-publisher/manifest.json`: add `webNavigation`, `https://open.weixin.qq.com/*`, and `all_frames` for platform agent injection.
- Modify `browser_extension/forwin-publisher/tests/build-targets.test.js`: ensure build output preserves `webNavigation`.
- Modify `browser_extension/forwin-publisher/tests/platform-agent-wiring.test.js`: assert pointer-event scan tab activation, delayed QR wait, and WeChat QR selectors.
- Modify `browser_extension/forwin-publisher/tests/background-wiring.test.js`: assert background enumerates frames and sends messages with `frameId` before screenshot fallback.
- Modify `browser_extension/forwin-publisher/background.js`: import frame helpers, enumerate relevant frames, ask each frame for QR extraction, then fall back to debugger screenshot.
- Modify `browser_extension/forwin-publisher/platform-agent.js`: add robust scan tab activation, QR candidate selectors, and conditional wait.

## Task 1: Frame Target Helper

**Files:**
- Create: `browser_extension/forwin-publisher/lib/login-qr-frames.js`
- Test: `browser_extension/forwin-publisher/tests/login-qr-frames.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  findLoginQrFrameTargets,
  isLoginQrFrameUrl,
  sanitizeFrameUrlForStatus,
} from '../lib/login-qr-frames.js';

test('identifies Qidian and WeChat login QR frames without broad host matching', () => {
  assert.equal(isLoginQrFrameUrl('https://passport.yuewen.com/yuewen.html?ticket=secret'), true);
  assert.equal(isLoginQrFrameUrl('https://open.weixin.qq.com/connect/qrconnect?appid=abc'), true);
  assert.equal(isLoginQrFrameUrl('https://example.com/connect/qrcode/abc'), false);
});

test('orders nested QR frames before parent login frames and strips query data', () => {
  const frames = [
    { frameId: 0, parentFrameId: -1, url: 'https://write.qq.com/portal/login?secret=1' },
    { frameId: 8, parentFrameId: 4, url: 'https://open.weixin.qq.com/connect/qrconnect?appid=abc&state=secret' },
    { frameId: 4, parentFrameId: 0, url: 'https://passport.yuewen.com/yuewen.html?ticket=secret' },
  ];

  assert.deepEqual(findLoginQrFrameTargets(frames), [
    {
      frameId: 8,
      url: 'https://open.weixin.qq.com/connect/qrconnect',
      priority: 40,
    },
    {
      frameId: 4,
      url: 'https://passport.yuewen.com/yuewen.html',
      priority: 30,
    },
    {
      frameId: 0,
      url: 'https://write.qq.com/portal/login',
      priority: 20,
    },
  ]);
  assert.equal(sanitizeFrameUrlForStatus('https://open.weixin.qq.com/connect/qrcode/abc?secret=1'), 'https://open.weixin.qq.com/connect/qrcode/abc');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd browser_extension/forwin-publisher && node --test tests/login-qr-frames.test.js`

Expected: FAIL with module not found for `../lib/login-qr-frames.js`.

- [ ] **Step 3: Write minimal implementation**

```javascript
const FRAME_HOST_PRIORITIES = [
  { host: 'open.weixin.qq.com', priority: 40 },
  { host: 'passport.yuewen.com', priority: 30 },
  { host: 'pcwrite.yuewen.com', priority: 25 },
  { host: 'write.qq.com', priority: 20 },
];

export function sanitizeFrameUrlForStatus(value) {
  try {
    const parsed = new URL(String(value || ''));
    return `${parsed.origin}${parsed.pathname}`;
  } catch (_error) {
    return '';
  }
}

export function framePriorityForUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    const hostname = parsed.hostname.toLowerCase();
    const matched = FRAME_HOST_PRIORITIES.find((item) => hostname === item.host || hostname.endsWith(`.${item.host}`));
    return matched?.priority || 0;
  } catch (_error) {
    return 0;
  }
}

export function isLoginQrFrameUrl(value) {
  return framePriorityForUrl(value) > 0;
}

export function findLoginQrFrameTargets(frames = []) {
  const seen = new Set();
  return (Array.isArray(frames) ? frames : [])
    .map((frame) => ({
      frameId: Number(frame?.frameId ?? 0),
      url: sanitizeFrameUrlForStatus(frame?.url),
      priority: framePriorityForUrl(frame?.url),
    }))
    .filter((frame) => frame.priority > 0 && Number.isInteger(frame.frameId) && !seen.has(frame.frameId) && seen.add(frame.frameId))
    .sort((left, right) => right.priority - left.priority || left.frameId - right.frameId);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd browser_extension/forwin-publisher && node --test tests/login-qr-frames.test.js`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add browser_extension/forwin-publisher/lib/login-qr-frames.js browser_extension/forwin-publisher/tests/login-qr-frames.test.js
git commit -m "Add publisher login QR frame targeting"
```

## Task 2: Manifest And Wiring Tests

**Files:**
- Modify: `browser_extension/forwin-publisher/manifest.json`
- Modify: `browser_extension/forwin-publisher/tests/build-targets.test.js`
- Modify: `browser_extension/forwin-publisher/tests/background-wiring.test.js`
- Modify: `browser_extension/forwin-publisher/tests/platform-agent-wiring.test.js`

- [ ] **Step 1: Write failing wiring tests**

```javascript
test('buildManifestForTarget preserves webNavigation for frame-scoped QR extraction', () => {
  const manifest = buildManifestForTarget('chromium', {
    ...sourceManifest,
    permissions: ['alarms', 'cookies', 'debugger', 'storage', 'tabs', 'webNavigation'],
  });

  assert.equal(manifest.permissions.includes('webNavigation'), true);
});
```

```javascript
test('background login QR extraction asks matching child frames before screenshot fallback', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /findLoginQrFrameTargets/);
  assert.match(source, /webNavigation/);
  assert.match(source, /frameId:\s*target\.frameId/);
  assert.match(source, /debugger-screenshot-failed/);
});
```

```javascript
test('platform agent uses browser-like scan tab activation and delayed QR wait', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+dispatchPointerClick\s*\(/);
  assert.match(source, /pointerdown/);
  assert.match(source, /waitForLoginQrCandidate/);
  assert.match(source, /connect\/qrcode/);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd browser_extension/forwin-publisher && node --test tests/build-targets.test.js tests/background-wiring.test.js tests/platform-agent-wiring.test.js`

Expected: FAIL because `webNavigation`, frame extraction wiring, and pointer activation are not present.

- [ ] **Step 3: Modify manifest**

Add `"webNavigation"` to `permissions`. Add `"https://open.weixin.qq.com/*"` to `host_permissions`. Add `"https://open.weixin.qq.com/*"` to the platform content-script `matches` list and set `"all_frames": true` on that platform content-script block.

- [ ] **Step 4: Run manifest/wiring tests**

Run: `cd browser_extension/forwin-publisher && node --test tests/build-targets.test.js tests/background-wiring.test.js tests/platform-agent-wiring.test.js`

Expected: background and platform tests may still fail until Tasks 3 and 4; build target test passes.

## Task 3: Platform Agent Direct QR Extraction

**Files:**
- Modify: `browser_extension/forwin-publisher/platform-agent.js`
- Test: `browser_extension/forwin-publisher/tests/platform-agent-wiring.test.js`

- [ ] **Step 1: Implement QR selectors and pointer activation**

Add WeChat/Qidian QR selectors to `loginQrCandidates()`:

```javascript
'img[src*="/connect/qrcode/"]',
'img[src*="qrcode" i]',
'img[src*="qrconnect" i]',
```

Add `dispatchPointerClick(element)` using `pointerdown`, `mousedown`, `pointerup`, `mouseup`, and `click`, with `bubbles: true`, `cancelable: true`, and `view: window`.

- [ ] **Step 2: Implement conditional QR wait**

Replace the fixed post-click sleep in `activateScanLoginTab()` with:

```javascript
async function waitForLoginQrCandidate(timeoutMs = 4000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const [candidate] = loginQrCandidates();
    if (candidate) {
      return candidate;
    }
    await sleep(200);
  }
  return null;
}
```

Call `dispatchPointerClick(scanTab)` and `await waitForLoginQrCandidate()` after activating the scan tab.

- [ ] **Step 3: Run platform agent wiring test**

Run: `cd browser_extension/forwin-publisher && node --test tests/platform-agent-wiring.test.js`

Expected: PASS.

## Task 4: Background Frame-Aware QR Capture

**Files:**
- Modify: `browser_extension/forwin-publisher/background.js`
- Test: `browser_extension/forwin-publisher/tests/background-wiring.test.js`

- [ ] **Step 1: Import frame helper**

```javascript
import { findLoginQrFrameTargets } from './lib/login-qr-frames.js';
```

- [ ] **Step 2: Add frame messaging helpers**

Add helpers near `captureLoginQrImage`:

```javascript
async function sendLoginQrExtractionMessage(tabId, options = {}) {
  return wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
    channel: PLATFORM_AGENT_CHANNEL,
    action: 'extract-login-qr-image',
  }, options);
}

async function queryLoginQrFrames(tabId) {
  if (!extensionApi.webNavigation?.getAllFrames) {
    return [];
  }
  try {
    const frames = await wrapCall(extensionApi.webNavigation, 'getAllFrames', { tabId });
    return findLoginQrFrameTargets(frames);
  } catch (_error) {
    return [];
  }
}

async function extractLoginQrFromFrames(tabId) {
  for (const target of await queryLoginQrFrames(tabId)) {
    try {
      const response = await sendLoginQrExtractionMessage(tabId, { frameId: target.frameId });
      if (response?.imageDataUrl) {
        return {
          ...response,
          source: response.source ? `frame:${target.frameId}:${response.source}` : `frame:${target.frameId}:image`,
          frameUrl: target.url,
        };
      }
    } catch (_error) {
      // Continue to the next frame and then to the debugger fallback.
    }
  }
  return null;
}
```

- [ ] **Step 3: Use helpers in capture**

In `captureLoginQrImage(tabId)`, replace the top-frame `wrapCall(extensionApi.tabs, 'sendMessage', ...)` with `sendLoginQrExtractionMessage(tabId)`. After the top frame path fails, call `extractLoginQrFromFrames(tabId)` and return that response before `captureTabScreenshotWithDebugger(tabId)`.

- [ ] **Step 4: Run background wiring test**

Run: `cd browser_extension/forwin-publisher && node --test tests/background-wiring.test.js`

Expected: PASS.

## Task 5: Full Extension Verification And Deployment

**Files:**
- Verify all changed extension files.
- Deploy from `/Users/magi1/ForWin-source-github` into `/Users/magi1/ForWin-swarm`.

- [ ] **Step 1: Run full extension test suite**

Run: `cd browser_extension/forwin-publisher && npm test`

Expected: all syntax checks and `node:test` tests pass.

- [ ] **Step 2: Commit implementation**

```bash
git add browser_extension/forwin-publisher
git commit -m "Extract publisher login QR from platform frames"
```

- [ ] **Step 3: Build and deploy updated browser image**

Use the repository deployment scripts already used for the current swarm, preserving existing Docker secrets and the preferred publisher client id. The deploy marker in `/Users/magi1/ForWin-swarm/.deploy-sync-source-commit` must update to the new source commit.

- [ ] **Step 4: Production verification**

Open or refresh both publisher login pages in the production publisher browser, trigger the extension heartbeat, and confirm Discord receives QR-focused images whose source is not `debugger-screenshot`.

Run health checks:

```bash
curl -fsS http://127.0.0.1:8899/health
curl -fsS http://127.0.0.1:8765/health
```

Expected: both return healthy JSON.

## Self-Review

- Spec coverage: Fanqie pointer activation is covered in Task 3; Qidian nested frames are covered in Tasks 1, 2, and 4; fallback screenshot behavior remains in Task 4; deployment verification is covered in Task 5.
- Placeholder scan: no task uses TBD, TODO, "similar to", or unspecified tests.
- Type consistency: `findLoginQrFrameTargets(frames)` returns `{ frameId, url, priority }`; background uses `target.frameId` and `target.url`; tests assert those names.
