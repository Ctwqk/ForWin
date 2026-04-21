import test from 'node:test';
import assert from 'node:assert/strict';

import {
  assertDebuggerCapability,
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
  const error = createUnsupportedCapabilityError(
    'firefox',
    'debugger',
    '请在 Chromium 版扩展中执行该上传任务。',
  );

  assert.equal(error.code, 'unsupported-browser-capability');
  assert.equal(error.browserTarget, 'firefox');
  assert.equal(error.capability, 'debugger');
  assert.match(error.message, /Chromium/);
});

test('assertDebuggerCapability throws a typed error for Firefox targets', () => {
  assert.throws(
    () => assertDebuggerCapability({
      browserTarget: 'firefox',
      supportsDebugger: false,
    }, '请在 Chromium 版扩展中执行该上传任务。'),
    (error) => error?.code === 'unsupported-browser-capability' && error?.capability === 'debugger',
  );
});
