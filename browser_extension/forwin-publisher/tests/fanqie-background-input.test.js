import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');
const implementation = (name) => source.match(new RegExp(`^async function ${name}\\([^]*?^}`, 'm'))?.[0];

function context(extra = {}) {
  return {
    URL,
    TOP_FRAME_MESSAGE_OPTIONS: { frameId: 0 },
    READY_CHANNELS: { PLATFORM_AGENT: 'platform' },
    PLATFORM_AGENT_CHANNEL: 'platform',
    extensionApi: { tabs: {} },
    tabReadyRegistry: { waitFor: async () => true, isReady: () => true, markReady() {}, reset() {} },
    sleep: async () => {},
    wrapCall: async () => ({ ok: true, wordCount: 999 }),
    ...extra,
  };
}

test('Fanqie body replacement selects the complete editor and verifies native input', async () => {
  const calls = [];
  await runInNewContext(`${implementation('applyTrustedFanqieBodyInput')}\napplyTrustedFanqieBodyInput(7,'新正文',{x:10,y:10})`, context({
    attachDebugger: async () => calls.push('attach'), detachDebugger: async () => calls.push('detach'),
    trustedClick: async () => {}, inspectFanqieEditorState: async () => ({ wordCount: 999 }),
    trustedSelectAllAndDelete: async () => calls.push('unverified-shortcut'),
    sendDebuggerCommand: async (_tab, _method, args) => {
      const selecting = args.expression.includes('selectNodeContents');
      calls.push(selecting ? 'select' : 'verify');
      return { result: { value: true } };
    },
    trustedInsertText: async (_tab, text) => calls.push(text),
  }));
  assert.deepEqual(calls, ['attach', 'select', '新正文', 'verify', 'detach']);
});

test('Fanqie replacement stops before typing if the whole editor was not selected', async () => {
  let inserted = false;
  await assert.rejects(runInNewContext(`${implementation('applyTrustedFanqieBodyInput')}\napplyTrustedFanqieBodyInput(7,'新正文',{x:10,y:10})`, context({
    attachDebugger: async () => {}, detachDebugger: async () => {}, trustedClick: async () => {},
    trustedSelectAllAndDelete: async () => {},
    sendDebuggerCommand: async () => ({ result: { value: false } }),
    trustedInsertText: async () => { inserted = true; },
  })), /选中/);
  assert.equal(inserted, false);
});

test('Fanqie replacement rejects a body mismatch instead of reporting input complete', async () => {
  let checks = 0;
  await assert.rejects(runInNewContext(`${implementation('applyTrustedFanqieBodyInput')}\napplyTrustedFanqieBodyInput(7,'新正文',{x:10,y:10})`, context({
    attachDebugger: async () => {}, detachDebugger: async () => {}, trustedClick: async () => {},
    trustedSelectAllAndDelete: async () => {}, trustedInsertText: async () => {},
    sendDebuggerCommand: async () => ({ result: { value: ++checks === 1 } }),
  })), /不一致/);
});

test('Fanqie editor inspection is bounded and restricted to the top frame', async () => {
  let sent;
  await runInNewContext(`${implementation('inspectFanqieEditorState')}\ninspectFanqieEditorState(7)`, context({
    sendPlatformAgentMessage: async (...args) => { sent = args; return { ok: true }; },
  }));
  assert.equal(sent?.[1], 'inspect-fanqie-editor-state');
  assert.ok(sent?.[3] > 0);
  assert.equal(sent?.[4]?.frameId, 0);
});

test('A timed-out top-frame probe cannot mark a tab responsive', async () => {
  const result = await runInNewContext(`${implementation('probePlatformAgentResponsive')}\nprobePlatformAgentResponsive(7)`, context({
    wrapCall: async () => ({ ok: false, errorCode: 'platform-agent-timeout' }),
    sendPlatformAgentMessage: async () => ({ ok: false, errorCode: 'platform-agent-timeout' }),
  }));
  assert.equal(result, false);
});

test('Draft verification returns to its exact book page and ignores unscoped frame replies', async () => {
  const expected = 'https://fanqienovel.com/main/writer/chapter-manage/123?type=2';
  const navigated = [];
  let url = 'https://fanqienovel.com/main/writer/';
  let sent;
  const result = await runInNewContext(`${implementation('verifyFanqieDraftOnPage')}\nverifyFanqieDraftOnPage(7,'第2章',{},{},${JSON.stringify(expected)})`, context({
    getTab: async () => ({ url }),
    navigateTab: async (_tab, target) => { navigated.push(target); url = target; },
    waitForRunnableWorkflowTab: async () => true,
    verifyFanqieDraftWithRetries: async ({ verify }) => verify(),
    sendPlatformAgentMessage: async (...args) => { sent = args; return { ok: true, currentUrl: url }; },
  }));
  assert.equal(result.ok, true);
  assert.deepEqual(navigated, [expected]);
  assert.equal(sent?.[1], 'verify-fanqie-draft');
  assert.equal(sent?.[4]?.frameId, 0);
});

test('Draft verification does not inspect the old page when navigation has not completed', async () => {
  let inspected = false;
  const result = await runInNewContext(`${implementation('verifyFanqieDraftOnPage')}\nverifyFanqieDraftOnPage(7,'第2章',{},{},'https://fanqienovel.com/main/writer/chapter-manage/123?type=2')`, context({
    getTab: async () => ({ url: 'https://fanqienovel.com/main/writer/' }),
    navigateTab: async () => {}, waitForRunnableWorkflowTab: async () => false,
    verifyFanqieDraftWithRetries: async ({ verify }) => verify(),
    sendPlatformAgentMessage: async () => { inspected = true; return { ok: true }; },
  }));
  assert.equal(result, null);
  assert.equal(inspected, false);
});

test('Draft verification rejects a success reply from the wrong page', async () => {
  const expected = 'https://fanqienovel.com/main/writer/chapter-manage/123?type=2';
  const result = await runInNewContext(`${implementation('verifyFanqieDraftOnPage')}\nverifyFanqieDraftOnPage(7,'第2章',{},{},${JSON.stringify(expected)})`, context({
    getTab: async () => ({ url: expected }), waitForRunnableWorkflowTab: async () => true,
    verifyFanqieDraftWithRetries: async ({ verify }) => verify(),
    sendPlatformAgentMessage: async () => ({ ok: true, currentUrl: 'https://fanqienovel.com/main/writer/' }),
  }));
  assert.equal(result, null);
});

test('Draft verification accepts the same book after Fanqie appends its title to the route', async () => {
  const expected = 'https://fanqienovel.com/main/writer/chapter-manage/123?type=2';
  const canonical = 'https://fanqienovel.com/main/writer/chapter-manage/123&%E4%B9%A6%E5%90%8D?type=2';
  const navigations = [];
  const result = await runInNewContext(`${implementation('verifyFanqieDraftOnPage')}\nverifyFanqieDraftOnPage(7,'第4章',{},{},${JSON.stringify(expected)})`, context({
    getTab: async () => ({ url: canonical }),
    navigateTab: async (_tab, url) => navigations.push(url),
    waitForRunnableWorkflowTab: async () => true,
    verifyFanqieDraftWithRetries: async ({ verify }) => verify(),
    sendPlatformAgentMessage: async () => ({ ok: true, currentUrl: canonical }),
  }));
  assert.equal(result?.ok, true);
  assert.deepEqual(navigations, []);
});

test('Fanqie title redirects still require the expected book, origin and draft tab', async () => {
  const expected = 'https://fanqienovel.com/main/writer/chapter-manage/123?type=2';
  for (const wrong of [
    'https://fanqienovel.com/main/writer/chapter-manage/456&book?type=2',
    'https://example.com/main/writer/chapter-manage/123&book?type=2',
    'https://fanqienovel.com/main/writer/chapter-manage/123&book?type=1',
  ]) {
    const result = await runInNewContext(`${implementation('verifyFanqieDraftOnPage')}\nverifyFanqieDraftOnPage(7,'第4章',{},{},${JSON.stringify(expected)})`, context({
      getTab: async () => ({ url: expected }),
      waitForRunnableWorkflowTab: async () => true,
      verifyFanqieDraftWithRetries: async ({ verify }) => verify(),
      sendPlatformAgentMessage: async () => ({ ok: true, currentUrl: wrong }),
    }));
    assert.equal(result, null, wrong);
  }
});
