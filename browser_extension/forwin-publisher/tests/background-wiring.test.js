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

test('background treats known publisher login URLs as inspectable login pages', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isPlatformLoginUrl\s*\(\s*platformId,\s*url\s*=\s*''\s*\)/);
  assert.match(source, /\/main\/writer\/login/);
  assert.match(source, /\/portal\/login/);
  assert.match(source, /summary:\s*'known login url'/);
});

test('background login QR extraction asks matching child frames before screenshot fallback', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /findLoginQrFrameTargets/);
  assert.match(source, /webNavigation/);
  assert.match(source, /frameId:\s*target\.frameId/);
  assert.match(source, /extractLoginQrFromFrames/);
  assert.match(source, /frameExtractionAttempt/);
  assert.match(source, /await\s+sleep\(500\)/);
});

test('background login QR extraction probes the top frame before screenshot fallback', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+extractLoginQrFromTopFrame\s*\(/);
  assert.match(source, /captureLoginQrImage[\s\S]*extractLoginQrFromTopFrame/);
  assert.match(source, /captureLoginQrImage[\s\S]*extractLoginQrFromFrames/);
  assert.match(source, /captureLoginQrImage[\s\S]*captureTabScreenshotWithDebugger/);
});
