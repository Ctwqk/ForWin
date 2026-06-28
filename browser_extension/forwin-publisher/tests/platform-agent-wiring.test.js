import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('platform agent activates scan login tab before extracting QR images', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+activateScanLoginTab\s*\(/);
  assert.match(source, /扫码登录/);
  assert.match(source, /await\s+activateScanLoginTab\s*\(\s*\)/);
});
