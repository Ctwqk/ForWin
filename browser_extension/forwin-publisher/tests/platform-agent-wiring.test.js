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
  assert.match(source, /二维码已失效/);
  assert.match(source, /nearbyLoginQrText/);
  assert.match(source, /async\s+function\s+refreshExpiredLoginQr\s*\(/);
  assert.match(source, /js_refresh_qrcode/);
  assert.match(source, /imgQrCodeReload/);
  assert.match(source, /点击刷新/);
  assert.match(source, /loginQrRefreshControlScore/);
});

test('platform agent can extract direct WeChat QR images with intrinsic dimensions', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+loginQrCandidateRect\s*\(/);
  assert.match(source, /naturalWidth/);
  assert.match(source, /connect\/qrcode/);
  assert.match(source, /js_qrcode_img/);
});

test('platform agent can pass required login agreement gates before QR activation', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /async\s+function\s+acceptVisibleLoginAgreements\s*\(/);
  assert.match(source, /agree6/);
  assert.match(source, /wxLoginButton/);
});

test('platform agent does not authenticate qidian loginOut authorh5 pages', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+isQidianLoginOutUrl\s*\(/);
  assert.match(source, /authorh5\/loginOut/);
  assert.match(source, /authenticated\s*=\s*!qidianLoginOut/);
});

test('platform agent does not treat generic Fanqie dashboard login text as login visible', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
  const fanqieInspectBlock = source.match(/if \(url\.includes\('fanqienovel\.com'\)\) \{[\s\S]*?return \{[\s\S]*?platform: 'fanqie'[\s\S]*?\};\n    \}/)?.[0] || '';

  assert.ok(fanqieInspectBlock);
  assert.doesNotMatch(fanqieInspectBlock, /['"]登录['"]/);
  assert.match(fanqieInspectBlock, /\/main\/writer\/login/);
  assert.match(fanqieInspectBlock, /登录\/注册/);
});

test('platform agent exposes a dedicated read-only upload reconciliation command', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
  const block = source.match(
    /async\s+function\s+reconcileUploadReadOnly\s*\([^)]*\)\s*\{[\s\S]*?\n  \}\n\n  async function prepareCoverUpload/,
  )?.[0] || '';

  assert.ok(block);
  assert.match(source, /message\.action\s*===\s*'reconcile-upload'/);
  assert.match(source, /reconcileUploadReadOnly\(message\.payload\s*\|\|\s*\{\}\)/);
  assert.match(block, /matchedContentSha256/);
  assert.match(block, /expectedContentSha256/);
  assert.doesNotMatch(block, /\.click\s*\(/);
  assert.doesNotMatch(block, /dispatchEvent\s*\(/);
  assert.doesNotMatch(block, /fill[A-Z]\w*\s*\(/);
  assert.doesNotMatch(block, /runUpload\s*\(/);
  assert.doesNotMatch(block, /runCoverUpload\s*\(/);
});

test('platform agent returns stable chapter identity with successful upload evidence', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

  assert.match(source, /function\s+chapterRemoteIdentity\s*\(/);
  assert.match(source, /async\s+function\s+chapterContentEvidence\s*\(/);
  assert.match(source, /editor-content-hash-mismatch/);
  assert.match(source, /observed_normalized_sha256/);
  assert.match(source, /normalized-editor-text-sha256/);
  assert.match(source, /remote_book_id:\s*remoteBookId/);
  assert.match(source, /remote_chapter_id:\s*remoteChapterId/);
  assert.match(source, /\.\.\.chapterRemoteIdentity\(platform,\s*chapterTitle\)/);
  assert.match(source, /\.\.\.chapterRemoteIdentity\('fanqie',\s*chapterTitle\)/);
  assert.match(source, /\.\.\.chapterRemoteIdentity\('qidian',\s*chapterTitle\)/);
});

test('platform agent requires exact cover identity and fresh platform acceptance', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
  const block = source.match(
    /async\s+function\s+runCoverUpload\s*\([^)]*\)\s*\{[\s\S]*?\n  \}\n\n  async function runAuditSync/,
  )?.[0] || '';

  assert.ok(block);
  assert.match(source, /function\s+observedCoverRemoteBookId\s*\(/);
  assert.match(source, /function\s+coverAcceptanceSignal\s*\(/);
  assert.match(block, /const beforeText = pageText\(\)/);
  assert.match(block, /observedCoverRemoteBookId\(payload, window\.location\.href\)/);
  assert.match(block, /cover-upload-not-confirmed/);
  assert.match(block, /cover-remote-book-mismatch/);
  assert.match(block, /acceptance_signal:\s*acceptanceSignal/);
  assert.doesNotMatch(block, /message:\s*'封面上传动作已提交。'/);
});

test('platform agent never treats visible historical cover state as a matched asset', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
  const block = source.match(
    /if \(taskKind === 'cover_upload'\) \{[\s\S]*?\n    \}\n    if \(taskKind !== 'chapter_upload'\)/,
  )?.[0] || '';

  assert.ok(block);
  assert.match(block, /outcome:\s*'indeterminate'/);
  assert.match(block, /matchedContentSha256:\s*''/);
  assert.doesNotMatch(block, /outcome:\s*accepted\s*\?/);
});

test('platform agent detects typed risk signals without challenge bypass actions', async () => {
  const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
  const detector = source.match(
    /function\s+detectPublisherRiskSignal\s*\([^)]*\)\s*\{[\s\S]*?\n  \}\n\n  function buildRiskPauseResult/,
  )?.[0] || '';

  assert.ok(detector);
  assert.match(detector, /captcha/);
  assert.match(detector, /mfa/);
  assert.match(detector, /account_risk/);
  assert.match(source, /message\.action\s*===\s*'inspect-publisher-risk'/);
  assert.match(source, /buildRiskPauseResult\('pre-mutation'/);
  assert.match(source, /buildRiskPauseResult\('before-save'/);
  assert.match(source, /buildRiskPauseResult\('before-confirm'/);
  assert.match(source, /buildRiskPauseResult\('post-action'/);
  assert.doesNotMatch(detector, /\.click\s*\(/);
  assert.doesNotMatch(detector, /dispatchEvent\s*\(/);
});
