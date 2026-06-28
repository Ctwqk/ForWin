import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('background backend adapter wires login QR notifications', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+notifyLoginQr\s*\(\s*payload\s*\)/);
  assert.match(source, /client\)\s*=>\s*client\.notifyLoginQr\(payload\)/);
});

test('background login QR fallback captures screenshots through debugger protocol', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+captureTabScreenshotWithDebugger\s*\(\s*tabId\s*\)/);
  assert.match(source, /attachDebugger\s*\(\s*tabId\s*\)/);
  assert.match(source, /Page\.captureScreenshot/);
  assert.match(source, /data:image\/png;base64,\$\{screenshot\.data\}/);
});
