import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('platform agent activates scan login tab before extracting QR images', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+activateScanLoginTab\s*\(/);
  assert.match(source, /扫码登录/);
  assert.match(source, /await\s+activateScanLoginTab\s*\(\s*\)/);
});

test('platform agent uses browser-like scan tab activation and delayed QR wait', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+dispatchPointerClick\s*\(/);
  assert.match(source, /pointerdown/);
  assert.match(source, /element\.click\?\.\(\s*\)/);
  assert.match(source, /waitForLoginQrCandidate/);
  assert.match(source, /timeoutMs\s*=\s*8000/);
  assert.match(source, /li,\[role="button"\],\[tab\]/);
  assert.match(source, /text\s*===\s*label[\s\S]*text\.includes\(label\)/);
  assert.match(source, /connect\/qrcode/);
});

test('platform agent refreshes expired login QR images before extraction', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isExpiredLoginQrImage\s*\(/);
  assert.match(source, /qrcode_expired/);
  assert.match(source, /async\s+function\s+refreshExpiredLoginQr\s*\(/);
  assert.match(source, /js_refresh_qrcode/);
  assert.match(source, /imgQrCodeReload/);
});

test('platform agent can pass required login agreement gates before QR activation', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+acceptVisibleLoginAgreements\s*\(/);
  assert.match(source, /agree6/);
  assert.match(source, /wxLoginButton/);
});
