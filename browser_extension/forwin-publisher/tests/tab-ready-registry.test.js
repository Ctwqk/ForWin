import test from 'node:test';
import assert from 'node:assert/strict';

import { READY_CHANNELS, TabReadyRegistry } from '../lib/tab-ready-registry.js';

test('TabReadyRegistry resolves waiting callers after markReady', async () => {
  const registry = new TabReadyRegistry();
  const pending = registry.waitFor(12, READY_CHANNELS.PLATFORM_AGENT, 200);
  registry.markReady(12, READY_CHANNELS.PLATFORM_AGENT);
  assert.equal(await pending, true);
  assert.equal(registry.isReady(12, READY_CHANNELS.PLATFORM_AGENT), true);
});

test('TabReadyRegistry reset clears readiness and resolves waiters as false', async () => {
  const registry = new TabReadyRegistry();
  registry.markReady(7, READY_CHANNELS.CONTENT_BRIDGE);
  assert.equal(registry.isReady(7, READY_CHANNELS.CONTENT_BRIDGE), true);
  registry.reset(7, READY_CHANNELS.CONTENT_BRIDGE);
  assert.equal(registry.isReady(7, READY_CHANNELS.CONTENT_BRIDGE), false);

  const pending = registry.waitFor(7, READY_CHANNELS.CONTENT_BRIDGE, 30);
  registry.reset(7, READY_CHANNELS.CONTENT_BRIDGE);
  assert.equal(await pending, false);
});
