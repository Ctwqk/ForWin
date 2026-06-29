import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('background cache-busts the controller import with the manifest version', async () => {
  const [source, manifestText] = await Promise.all([
    readFile(new URL('../background.js', import.meta.url), 'utf8'),
    readFile(new URL('../manifest.json', import.meta.url), 'utf8'),
  ]);
  const manifest = JSON.parse(manifestText);
  const escapedVersion = String(manifest.version).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  assert.match(source, new RegExp(`\\.\\/lib\\/controller\\.js\\?v=${escapedVersion}`));
});

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

test('background retries top-frame login QR extraction before screenshot fallback', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /topFrameExtractionAttempt/);
  assert.match(source, /topFrameExtractionAttempt\s*<\s*3/);
  assert.match(source, /extractLoginQrFromTopFrame[\s\S]*await\s+sleep\(700\)/);
  assert.match(source, /captureLoginQrImage[\s\S]*extractLoginQrFromTopFrame[\s\S]*extractLoginQrFromFrames[\s\S]*captureTabScreenshotWithDebugger/);
});

test('background keeps retrying direct QR extraction before screenshot fallback', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /LOGIN_QR_DIRECT_EXTRACTION_TIMEOUT_MS\s*=\s*15000/);
  assert.match(source, /directExtractionDeadline/);
  assert.match(source, /Date\.now\(\)\s*<\s*directExtractionDeadline/);
  assert.match(source, /captureLoginQrImage[\s\S]*extractLoginQrFromTopFrame[\s\S]*extractLoginQrFromFrames[\s\S]*await\s+sleep\(1200\)[\s\S]*captureTabScreenshotWithDebugger/);
});

test('background routes platform business commands to the top frame', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /const\s+TOP_FRAME_MESSAGE_OPTIONS\s*=\s*\{\s*frameId:\s*0\s*\}/);
  assert.match(source, /sendPlatformAgentMessage[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
  assert.match(source, /runUploadCommand[\s\S]*sendPlatformAgentMessage\([\s\S]*'run-upload'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
  assert.match(source, /runCommentSyncCommand[\s\S]*sendPlatformAgentMessage\([\s\S]*'run-comment-sync'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
});

test('background inspects publisher login state in the top frame', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /probePlatformAgentResponsive[\s\S]*'inspect-login-state'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
  assert.match(source, /inspectLoginState[\s\S]*'inspect-login-state'[\s\S]*TOP_FRAME_MESSAGE_OPTIONS/);
});

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

test('background prefers qidian top-level dashboard evidence over loginOut child inspections', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isQidianAuthenticatedTopLevelTab\s*\(/);
  assert.match(source, /candidate\?\.title[\s\S]*工作台[\s\S]*作家专区/);
  assert.match(source, /function\s+isQidianAuthenticatedTopLevelTab[\s\S]*const\s+status\s*=\s*String\(candidate\?\.status/);
  assert.match(source, /function\s+isQidianAuthenticatedTopLevelTab[\s\S]*status\s*===\s*'complete'/);
  assert.match(source, /isQidianLoginOutUrl[\s\S]*inspection\.currentUrl/);
  assert.match(source, /authenticated:\s*true[\s\S]*loginVisible:\s*false/);
});

test('background uses qidian top-level dashboard evidence when the agent is not ready', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+buildAuthenticatedInspectionFromTopLevelEvidence\s*\(/);
  assert.match(source, /if\s*\(\s*!inspection\?\.\s*ok\s*&&\s*topLevelEvidence\s*\)/);
  assert.match(source, /summary:\s*'qidian top-level dashboard evidence'/);
});

test('background keeps qidian top-level writing pages authenticated over child outline inspections', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isQidianAuthenticatedWritingTab\s*\(/);
  assert.match(source, /\/portal\/booknovels\/chaptertmp\//);
  assert.match(source, /candidate\?\.title[\s\S]*写作/);
  assert.match(source, /isQidianAuthenticatedWritingTab[\s\S]*inspection\.currentUrl/);
  assert.match(source, /summary:\s*'qidian top-level writing evidence'/);
});
