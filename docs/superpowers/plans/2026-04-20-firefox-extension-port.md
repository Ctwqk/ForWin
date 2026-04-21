# Firefox Extension Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Firefox target for the existing ForWin publisher extension while preserving the current Chromium target and keeping both targets on one shared codebase and version.

**Architecture:** Keep `browser_extension/forwin-publisher` as the shared source tree, add a small Node build script that generates browser-specific manifests and output directories, and model browser capabilities explicitly so Chromium-only `debugger` flows are gated rather than silently broken in Firefox. Firefox will share login, cookies, options, content scripts, and heartbeat logic, while Chromium retains the current trusted-input automation path.

**Tech Stack:** Node.js ESM, WebExtensions APIs, browser extension source files under `browser_extension/forwin-publisher`, Node test runner (`node --test`)

---

## File Structure

- Create: `browser_extension/forwin-publisher/build-targets.js`
  - Generate browser-specific manifests and dist directories.
- Create: `browser_extension/forwin-publisher/lib/browser-capabilities.js`
  - Centralize browser target detection, capability calculation, and unsupported-capability errors.
- Create: `browser_extension/forwin-publisher/tests/browser-capabilities.test.js`
  - Cover target detection and capability-gated errors.
- Create: `browser_extension/forwin-publisher/tests/build-targets.test.js`
  - Cover Chromium/Firefox manifest generation.
- Modify: `browser_extension/forwin-publisher/lib/extension-runtime.js`
  - Re-export browser target and capabilities next to `extensionApi`.
- Modify: `browser_extension/forwin-publisher/background.js`
  - Use shared capability helpers before any `debugger`-dependent action.
- Modify: `browser_extension/forwin-publisher/package.json`
  - Add `build`, `build:chromium`, and `build:firefox` scripts and include new tests.
- Modify: `browser_extension/forwin-publisher/README.md`
  - Document new build outputs, Firefox loading, and Chromium-only capability limits.

### Task 1: Add Targeted Manifest Build Pipeline

**Files:**
- Create: `browser_extension/forwin-publisher/build-targets.js`
- Create: `browser_extension/forwin-publisher/tests/build-targets.test.js`
- Modify: `browser_extension/forwin-publisher/package.json`
- Modify: `browser_extension/forwin-publisher/README.md`

- [ ] **Step 1: Write the failing manifest build tests**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

import { buildManifestForTarget } from '../build-targets.js';

test('buildManifestForTarget keeps service worker and debugger for chromium', () => {
  const manifest = buildManifestForTarget('chromium');

  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.equal(manifest.background.type, 'module');
  assert.equal(manifest.permissions.includes('debugger'), true);
  assert.equal(manifest.browser_specific_settings, undefined);
});

test('buildManifestForTarget switches to Firefox background scripts and gecko settings', () => {
  const manifest = buildManifestForTarget('firefox');

  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.background.scripts, ['background.js']);
  assert.equal(manifest.background.type, 'module');
  assert.equal('service_worker' in manifest.background, false);
  assert.equal(manifest.permissions.includes('debugger'), false);
  assert.equal(manifest.browser_specific_settings.gecko.id, 'forwin-publisher@example.com');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/build-targets.test.js`
Expected: FAIL with module-not-found or missing export errors because `build-targets.js` does not exist yet.

- [ ] **Step 3: Implement the minimal manifest build module**

```javascript
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SOURCE_DIR = __dirname;
const DIST_ROOT = path.resolve(SOURCE_DIR, '..', 'dist');
const TARGETS = ['chromium', 'firefox'];

async function readSourceManifest() {
  const raw = await fs.readFile(path.join(SOURCE_DIR, 'manifest.json'), 'utf8');
  return JSON.parse(raw);
}

export function buildManifestForTarget(target, sourceManifest) {
  const manifest = structuredClone(sourceManifest);
  if (target === 'chromium') {
    return manifest;
  }
  if (target !== 'firefox') {
    throw new Error(`Unsupported extension target: ${target}`);
  }
  manifest.permissions = (manifest.permissions || []).filter((item) => item !== 'debugger');
  manifest.background = {
    scripts: ['background.js'],
    type: 'module',
  };
  manifest.browser_specific_settings = {
    gecko: {
      id: 'forwin-publisher@example.com',
    },
  };
  return manifest;
}

async function copySourceTree(destinationDir) {
  await fs.rm(destinationDir, { recursive: true, force: true });
  await fs.mkdir(destinationDir, { recursive: true });
  const entries = await fs.readdir(SOURCE_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name === 'tests' || entry.name === 'node_modules') {
      continue;
    }
    await fs.cp(
      path.join(SOURCE_DIR, entry.name),
      path.join(destinationDir, entry.name),
      { recursive: true },
    );
  }
}

export async function buildTarget(target) {
  const sourceManifest = await readSourceManifest();
  const destinationDir = path.join(DIST_ROOT, `forwin-publisher-${target}`);
  await copySourceTree(destinationDir);
  const manifest = buildManifestForTarget(target, sourceManifest);
  await fs.writeFile(
    path.join(destinationDir, 'manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );
  return { target, destinationDir, manifest };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const target = process.argv[2] || 'all';
  const selected = target === 'all' ? TARGETS : [target];
  await Promise.all(selected.map((item) => buildTarget(item)));
}
```

- [ ] **Step 4: Run tests to verify manifest generation passes**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/build-targets.test.js`
Expected: PASS with `2` passing tests.

- [ ] **Step 5: Wire package scripts and documentation**

```json
{
  "scripts": {
    "build": "node build-targets.js",
    "build:chromium": "node build-targets.js chromium",
    "build:firefox": "node build-targets.js firefox",
    "test": "node --check background.js && node --check options.js && node --check content-bridge.js && node --check platform-agent.js && node --check channels-runtime.js && node --check build-targets.js && node --check lib/channels.js && node --check lib/browser-capabilities.js && node --check lib/extension-runtime.js && node --check lib/tab-ready-registry.js && node --test tests/*.test.js"
  }
}
```

README text to add:

```markdown
构建双目标产物：

- `npm run build`
- `npm run build:chromium`
- `npm run build:firefox`

构建输出目录：

- `browser_extension/dist/forwin-publisher-chromium`
- `browser_extension/dist/forwin-publisher-firefox`

Firefox 加载方式：

1. 运行 `npm run build:firefox`
2. 打开 `about:debugging`
3. 选择“Load Temporary Add-on”
4. 指向 `browser_extension/dist/forwin-publisher-firefox/manifest.json`
```

- [ ] **Step 6: Run the full extension test command**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && npm test`
Expected: PASS with all existing tests plus the new manifest build tests.

### Task 2: Add Shared Browser Capability Modeling

**Files:**
- Create: `browser_extension/forwin-publisher/lib/browser-capabilities.js`
- Create: `browser_extension/forwin-publisher/tests/browser-capabilities.test.js`
- Modify: `browser_extension/forwin-publisher/lib/extension-runtime.js`

- [ ] **Step 1: Write the failing capability tests**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createUnsupportedCapabilityError,
  detectBrowserTarget,
  getBrowserCapabilities,
} from '../lib/browser-capabilities.js';

test('detectBrowserTarget identifies Firefox from user agent', () => {
  assert.equal(
    detectBrowserTarget('Mozilla/5.0 Firefox/137.0'),
    'firefox',
  );
});

test('detectBrowserTarget defaults to chromium for Chrome-style user agent', () => {
  assert.equal(
    detectBrowserTarget('Mozilla/5.0 Chrome/135.0.0.0 Safari/537.36'),
    'chromium',
  );
});

test('getBrowserCapabilities disables debugger on Firefox target', () => {
  const capabilities = getBrowserCapabilities({
    browserTarget: 'firefox',
    extensionApi: {},
  });
  assert.equal(capabilities.supportsDebugger, false);
  assert.equal(capabilities.supportsBackgroundServiceWorker, false);
});

test('createUnsupportedCapabilityError includes browser target and guidance', () => {
  const error = createUnsupportedCapabilityError('firefox', 'debugger', '请在 Chromium 版扩展中执行该上传任务。');
  assert.equal(error.code, 'unsupported-browser-capability');
  assert.equal(error.browserTarget, 'firefox');
  assert.equal(error.capability, 'debugger');
  assert.match(error.message, /Chromium/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/browser-capabilities.test.js`
Expected: FAIL because `lib/browser-capabilities.js` does not exist yet.

- [ ] **Step 3: Implement capability helpers and re-export them through extension runtime**

```javascript
export function detectBrowserTarget(userAgent = '') {
  return /Firefox\//i.test(String(userAgent)) ? 'firefox' : 'chromium';
}

export function getBrowserCapabilities({ browserTarget, extensionApi }) {
  const supportsDebugger = browserTarget === 'chromium' && Boolean(extensionApi?.debugger?.attach);
  return {
    browserTarget,
    supportsDebugger,
    supportsBackgroundServiceWorker: browserTarget === 'chromium',
  };
}

export function createUnsupportedCapabilityError(browserTarget, capability, guidance = '') {
  const suffix = guidance ? ` ${guidance}` : '';
  const error = new Error(`Current browser target "${browserTarget}" does not support capability "${capability}".${suffix}`);
  error.code = 'unsupported-browser-capability';
  error.browserTarget = browserTarget;
  error.capability = capability;
  return error;
}
```

`lib/extension-runtime.js` should expose:

```javascript
import {
  createUnsupportedCapabilityError,
  detectBrowserTarget,
  getBrowserCapabilities,
} from './browser-capabilities.js';

export const extensionApi = globalThis.browser ?? globalThis.chrome;
export const browserTarget = detectBrowserTarget(globalThis.navigator?.userAgent || '');
export const extensionCapabilities = getBrowserCapabilities({ browserTarget, extensionApi });
export { createUnsupportedCapabilityError };
```

- [ ] **Step 4: Run tests to verify capabilities pass**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/browser-capabilities.test.js`
Expected: PASS with `4` passing tests.

- [ ] **Step 5: Run the full extension test command**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && npm test`
Expected: PASS with existing tests unaffected.

### Task 3: Gate Debugger-Only Background Paths for Firefox

**Files:**
- Modify: `browser_extension/forwin-publisher/background.js`
- Modify: `browser_extension/forwin-publisher/README.md`

- [ ] **Step 1: Write the failing regression test for unsupported debugger capability**

Add this test to `browser_extension/forwin-publisher/tests/browser-capabilities.test.js`:

```javascript
import { assertDebuggerCapability } from '../lib/browser-capabilities.js';

test('assertDebuggerCapability throws a typed error for Firefox targets', () => {
  assert.throws(
    () => assertDebuggerCapability({
      browserTarget: 'firefox',
      supportsDebugger: false,
    }, '请在 Chromium 版扩展中执行该上传任务。'),
    (error) => error?.code === 'unsupported-browser-capability' && error?.capability === 'debugger',
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/browser-capabilities.test.js`
Expected: FAIL because `assertDebuggerCapability` is not exported yet.

- [ ] **Step 3: Implement the typed debugger guard and wire it into background actions**

```javascript
export function assertDebuggerCapability(capabilities, guidance = '') {
  if (capabilities?.supportsDebugger) {
    return;
  }
  throw createUnsupportedCapabilityError(
    capabilities?.browserTarget || 'unknown',
    'debugger',
    guidance || '请在 Chromium 版扩展中执行该上传任务。',
  );
}
```

Use it in `background.js` before:

```javascript
async function setCookiesViaDebugger(platformId, cookies = []) {
  assertDebuggerCapability(
    extensionCapabilities,
    '当前浏览器不支持调试协议 cookie 注入，请改用 Chromium 版扩展或仅使用普通 cookies API 恢复。',
  );
  // existing logic
}
```

and:

```javascript
async function attachDebugger(tabId) {
  assertDebuggerCapability(
    extensionCapabilities,
    '当前浏览器不支持可信输入和可信点击，请在 Chromium 版扩展中执行该上传任务。',
  );
  await wrapCall(extensionApi.debugger, 'attach', { tabId }, '1.3');
}
```

- [ ] **Step 4: Run targeted tests and then the full suite**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node --test tests/browser-capabilities.test.js`
Expected: PASS including the new debugger guard regression test.

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && npm test`
Expected: PASS with all tests green.

- [ ] **Step 5: Build both targets and verify dist manifests**

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && npm run build`
Expected: PASS and create:

```text
/home/taiwei/ForWin/browser_extension/dist/forwin-publisher-chromium/manifest.json
/home/taiwei/ForWin/browser_extension/dist/forwin-publisher-firefox/manifest.json
```

Run: `cd /home/taiwei/ForWin/browser_extension/forwin-publisher && node -e "import fs from 'node:fs'; const chromium = JSON.parse(fs.readFileSync('../dist/forwin-publisher-chromium/manifest.json', 'utf8')); const firefox = JSON.parse(fs.readFileSync('../dist/forwin-publisher-firefox/manifest.json', 'utf8')); console.log(JSON.stringify({ chromiumBackground: chromium.background, firefoxBackground: firefox.background, chromiumHasDebugger: chromium.permissions.includes('debugger'), firefoxHasDebugger: firefox.permissions.includes('debugger'), firefoxGeckoId: firefox.browser_specific_settings.gecko.id }, null, 2));"`
Expected: Chromium manifest shows `service_worker`, Firefox manifest shows `scripts`, Chromium keeps `debugger`, Firefox removes it, and Firefox has `forwin-publisher@example.com`.

- [ ] **Step 6: Commit**

```bash
cd /home/taiwei/ForWin
git add docs/superpowers/specs/2026-04-20-firefox-extension-port-design.md \
  docs/superpowers/plans/2026-04-20-firefox-extension-port.md \
  browser_extension/forwin-publisher/build-targets.js \
  browser_extension/forwin-publisher/lib/browser-capabilities.js \
  browser_extension/forwin-publisher/lib/extension-runtime.js \
  browser_extension/forwin-publisher/background.js \
  browser_extension/forwin-publisher/package.json \
  browser_extension/forwin-publisher/README.md \
  browser_extension/forwin-publisher/tests/browser-capabilities.test.js \
  browser_extension/forwin-publisher/tests/build-targets.test.js
git commit -m "feat: add firefox extension target"
```
