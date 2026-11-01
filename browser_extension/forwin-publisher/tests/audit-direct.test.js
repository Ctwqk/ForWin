import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { PublisherExtensionController } from '../lib/controller.js';
import { createBackendClient } from '../lib/backend-client.js';

// Audit sync over the direct request channel (D8): the extension claims audit requests from
// ForWin the way it claims comment sync jobs, runs the existing read-only audit page action
// and posts {work, chapters, cover, milestones} back. Nothing here talks to publish-hub.

const FORWIN_URL = 'http://forwin.test:8899';
const FORWIN_KEY = 'forwin-secret';
const HUB_KEY = 'hub-secret-key';
const CLIENT_ID = 'client-1';

function auditRequest(overrides = {}) {
  return {
    request_id: 'audit-1',
    platform: 'fanqie',
    remote_book_id: '7001',
    upload_url: 'https://fanqienovel.com/main/writer/book-info/7001',
    remote_url: 'https://fanqienovel.com/main/writer/book-info/7001',
    ...overrides,
  };
}

function observation(overrides = {}) {
  return {
    ok: true,
    currentUrl: 'https://fanqienovel.com/main/writer/book-info/7001',
    message: '平台审核状态已同步。',
    resultPayload: {
      task_kind: 'audit_sync',
      work: {
        work_binding_id: '',
        remote_book_id: '7001',
        remote_url: 'https://fanqienovel.com/main/writer/book-info/7001',
        audit_state: 'approved',
        official_status: '已发布',
        platform_message: '审核通过',
      },
      chapters: [],
      cover: { cover_state: 'approved' },
      milestones: [{ milestone_type: 'signing_entry_visible', state: 'open', message: 'x' }],
    },
    ...overrides,
  };
}

// A queue of audit requests the fake ForWin hands out one at a time.
function makeFixture({
  requests = [auditRequest()],
  connected = ['fanqie'],
  backend = {},
  deps = {},
  settings = {},
} = {}) {
  const calls = [];
  const queue = [...requests];
  const openedTabs = [];
  const closedTabs = [];
  const tabs = new Map();
  let nextTabId = 300;
  const defaultBackend = {
    heartbeat: async () => {
      calls.push('heartbeat');
      return { ok: true };
    },
    syncBrowserSession: async () => ({ ok: true }),
    getBrowserSession: async () => null,
    claimNextUploadJob: async () => {
      calls.push('upload-claim');
      return { found: false, claim: null };
    },
    claimNextCommentSyncJob: async () => {
      calls.push('comment-claim');
      return { found: false, job: null };
    },
    claimNextAuditRequest: async (payload) => {
      calls.push(['audit-claim', payload]);
      return queue.shift() || null;
    },
    submitAuditRequestResult: async (requestId, payload) => {
      calls.push(['audit-result', requestId, payload]);
      return { ok: true, status: 'succeeded' };
    },
  };
  const controllerDeps = {
    ensureClientId: async () => CLIENT_ID,
    getClientId: async () => CLIENT_ID,
    ensureHeartbeatAlarm: async () => {},
    getSettings: async () => ({
      backendBaseUrl: FORWIN_URL,
      apiKey: FORWIN_KEY,
      syncSessionToBackend: false,
      ...settings,
    }),
    getExtensionVersion: () => '0.1.64',
    getBrowserInfo: async () => ({ browserName: 'Chrome', browserVersion: '123.0' }),
    getCookies: async () => [],
    getPlatformState: async () => ({}),
    setPlatformState: async () => {},
    notifyPage: async () => {},
    openUploadTab: async (url) => {
      const tabId = nextTabId;
      nextTabId += 1;
      tabs.set(tabId, url);
      openedTabs.push({ tabId, url });
      calls.push(['open-tab', url]);
      return { tabId };
    },
    getTab: async (tabId) => (tabs.has(tabId) ? { id: tabId, url: tabs.get(tabId), status: 'complete' } : null),
    closeTab: async (tabId) => {
      closedTabs.push(tabId);
    },
    runAuditSyncCommand: async (tabId, payload) => {
      calls.push(['audit-page', tabId, payload]);
      return observation();
    },
    runUploadCommand: async () => {
      throw new Error('an audit must never run the upload action');
    },
    runCoverUploadCommand: async () => {
      throw new Error('an audit must never run the cover action');
    },
    runCreateBookCommand: async () => {
      throw new Error('an audit must never create a book');
    },
    reportError: async (context, error) => {
      calls.push(['report-error', context, error instanceof Error ? error.message : String(error)]);
    },
    ...deps,
    backend: { ...defaultBackend, ...backend },
  };
  const controller = new PublisherExtensionController(controllerDeps);
  controller._collectConnectedPlatforms = async () => connected;
  return { controller, calls, openedTabs, closedTabs, queue };
}

const names = (calls) => calls.map((call) => (Array.isArray(call) ? call[0] : call));

// --- dispatch -----------------------------------------------------------------------------

test('an alarm round dispatches audit requests after upload and comment sync', async () => {
  const fixture = makeFixture();

  await fixture.controller.heartbeatRound();

  const order = names(fixture.calls);
  assert.ok(order.indexOf('upload-claim') !== -1, order.join(','));
  assert.ok(order.indexOf('comment-claim') !== -1, order.join(','));
  const auditClaim = order.indexOf('audit-claim');
  assert.ok(auditClaim > order.indexOf('comment-claim'), order.join(','));
  assert.ok(auditClaim > order.indexOf('upload-claim'), order.join(','));
  assert.ok(order.includes('audit-page'), order.join(','));
  assert.ok(order.includes('audit-result'), order.join(','));
});

test('a comment sync job that fails does not starve the audit round', async () => {
  const fixture = makeFixture({
    backend: {
      claimNextCommentSyncJob: async () => {
        throw new Error('comment backend down');
      },
    },
  });

  await assert.rejects(fixture.controller.heartbeatRound(), /comment backend down/);

  assert.ok(names(fixture.calls).includes('audit-result'));
});

test('an audit failure is raised by the round after the other work ran', async () => {
  const fixture = makeFixture({
    backend: {
      claimNextAuditRequest: async () => {
        throw new Error('audit claim failed');
      },
    },
  });

  await assert.rejects(fixture.controller.heartbeatRound(), /audit claim failed/);

  const order = names(fixture.calls);
  assert.ok(order.includes('upload-claim') && order.includes('comment-claim'), order.join(','));
});

test('settings-updated style bootstrap dispatch also runs the audit round last', async () => {
  const fixture = makeFixture();

  await fixture.controller._syncBackendStateAndDispatchPendingJobs();

  const order = names(fixture.calls);
  assert.ok(order.indexOf('audit-claim') > order.indexOf('comment-claim'), order.join(','));
});

test('no audit request is started while an upload dispatch is running', async () => {
  let releaseUpload;
  const uploadGate = new Promise((resolve) => {
    releaseUpload = resolve;
  });
  const fixture = makeFixture({
    backend: {
      claimNextUploadJob: async () => {
        await uploadGate;
        return { found: false, claim: null };
      },
    },
  });

  const upload = fixture.controller.dispatchPendingUploadJobs();
  await new Promise((resolve) => setImmediate(resolve));
  const duringUpload = await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(duringUpload, { skipped: true, reason: 'busy' });
  assert.ok(!names(fixture.calls).includes('audit-claim'));

  releaseUpload();
  await upload;
  await fixture.controller.dispatchPendingAuditRequests();
  assert.ok(names(fixture.calls).includes('audit-claim'));
});

test('no audit request is started while a comment sync dispatch is running', async () => {
  let releaseComment;
  const commentGate = new Promise((resolve) => {
    releaseComment = resolve;
  });
  const fixture = makeFixture({
    backend: {
      claimNextCommentSyncJob: async () => {
        await commentGate;
        return { found: false, job: null };
      },
    },
  });

  const comment = fixture.controller.dispatchPendingCommentSyncJobs();
  await new Promise((resolve) => setImmediate(resolve));
  const duringComment = await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(duringComment, { skipped: true, reason: 'busy' });
  assert.ok(!names(fixture.calls).includes('audit-claim'));

  releaseComment();
  await comment;
});

test('an upload that starts during the audit round stops it before the next request', async () => {
  let releaseUpload;
  const uploadGate = new Promise((resolve) => {
    releaseUpload = resolve;
  });
  const fixture = makeFixture({
    requests: [auditRequest({ request_id: 'audit-1' }), auditRequest({ request_id: 'audit-2' })],
    backend: {
      claimNextUploadJob: async () => {
        await uploadGate;
        return { found: false, claim: null };
      },
    },
  });
  let upload = null;
  fixture.controller.deps.runAuditSyncCommand = async () => {
    // another trigger (settings-updated, login) starts an upload dispatch mid-audit
    upload = fixture.controller.dispatchPendingUploadJobs();
    await new Promise((resolve) => setImmediate(resolve));
    return observation();
  };

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(outcome, { found: true, handled: 1 });
  assert.deepEqual(
    fixture.calls.filter((call) => call[0] === 'audit-claim').length,
    1,
  );
  assert.equal(fixture.queue.length, 1);
  releaseUpload();
  await upload;
});

test('two audit dispatches never overlap', async () => {
  let releaseClaim;
  const claimGate = new Promise((resolve) => {
    releaseClaim = resolve;
  });
  let claims = 0;
  const fixture = makeFixture({
    backend: {
      claimNextAuditRequest: async () => {
        claims += 1;
        await claimGate;
        return null;
      },
    },
  });

  const first = fixture.controller.dispatchPendingAuditRequests();
  await new Promise((resolve) => setImmediate(resolve));
  const second = fixture.controller.dispatchPendingAuditRequests();
  releaseClaim();
  await Promise.all([first, second]);

  assert.equal(claims, 1);
});

test('requests run one at a time, each in its own tab that is closed afterwards', async () => {
  const fixture = makeFixture({
    requests: [
      auditRequest({ request_id: 'audit-1' }),
      auditRequest({ request_id: 'audit-2', remote_book_id: '7002' }),
    ],
  });
  let running = 0;
  let maxRunning = 0;
  fixture.controller.deps.runAuditSyncCommand = async (tabId, payload) => {
    running += 1;
    maxRunning = Math.max(maxRunning, running);
    await new Promise((resolve) => setImmediate(resolve));
    fixture.calls.push(['audit-page', tabId, payload]);
    running -= 1;
    return observation();
  };

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.equal(maxRunning, 1);
  assert.equal(outcome.handled, 2);
  assert.deepEqual(
    fixture.calls.filter((call) => call[0] === 'audit-result').map((call) => call[1]),
    ['audit-1', 'audit-2'],
  );
  assert.equal(fixture.openedTabs.length, 2);
  assert.deepEqual(fixture.closedTabs.sort(), fixture.openedTabs.map((tab) => tab.tabId).sort());
});

test('a round handles at most eight audit requests', async () => {
  const requests = Array.from({ length: 12 }, (_, index) => auditRequest({ request_id: `audit-${index}` }));
  const fixture = makeFixture({ requests });

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.equal(outcome.handled, 8);
  assert.equal(outcome.truncated, true);
  assert.equal(fixture.queue.length, 4);
});

// --- platform filter ----------------------------------------------------------------------

test('audit claims are limited to the platforms the extension is logged in to', async () => {
  const fixture = makeFixture({ connected: ['fanqie'], requests: [] });

  await fixture.controller.dispatchPendingAuditRequests();

  const claim = fixture.calls.find((call) => call[0] === 'audit-claim');
  assert.deepEqual(claim[1], { client_id: CLIENT_ID, connected_platforms: ['fanqie'] });
});

test('nothing is claimed when no platform is logged in', async () => {
  const fixture = makeFixture({ connected: [] });

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(outcome, { skipped: true });
  assert.ok(!names(fixture.calls).includes('audit-claim'));
});

test('nothing is claimed without a ForWin url and key', async () => {
  const fixture = makeFixture({ settings: { apiKey: '' } });

  assert.deepEqual(await fixture.controller.dispatchPendingAuditRequests(), { skipped: true });
  assert.ok(!names(fixture.calls).includes('audit-claim'));
});

test('a backend without the audit calls is skipped, like comment sync', async () => {
  const fixture = makeFixture({ backend: { claimNextAuditRequest: undefined } });

  assert.deepEqual(await fixture.controller.dispatchPendingAuditRequests(), { skipped: true });
});

// --- executing a request -----------------------------------------------------------------

test('the existing audit page action runs on the request page and the observation is posted back', async () => {
  const fixture = makeFixture();

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(outcome, { found: true, handled: 1 });
  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), [
    'https://fanqienovel.com/main/writer/book-info/7001',
  ]);
  const page = fixture.calls.find((call) => call[0] === 'audit-page');
  assert.equal(page[2].platform, 'fanqie');
  assert.equal(page[2].task_kind, 'audit_sync');
  assert.equal(page[2].remote_book_id, '7001');
  assert.equal(page[2].remote_url, 'https://fanqienovel.com/main/writer/book-info/7001');
  const result = fixture.calls.find((call) => call[0] === 'audit-result');
  assert.equal(result[1], 'audit-1');
  assert.deepEqual(Object.keys(result[2]).sort(), [
    'chapters',
    'client_id',
    'cover',
    'current_url',
    'milestones',
    'work',
  ]);
  assert.equal(result[2].client_id, CLIENT_ID);
  assert.equal(result[2].work.audit_state, 'approved');
  assert.equal(result[2].work.remote_book_id, '7001');
  assert.deepEqual(result[2].chapters, []);
  assert.deepEqual(result[2].cover, { cover_state: 'approved' });
  assert.equal(result[2].milestones[0].milestone_type, 'signing_entry_visible');
  assert.equal(result[2].current_url, 'https://fanqienovel.com/main/writer/book-info/7001');
});

test('the tab target falls back to the platform dashboard when the request has no url', async () => {
  const fixture = makeFixture({
    requests: [auditRequest({ upload_url: null, remote_url: null })],
  });

  await fixture.controller.dispatchPendingAuditRequests();

  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/']);
});

test('an audit only reads: no upload, cover or create action runs', async () => {
  const fixture = makeFixture();

  await fixture.controller.dispatchPendingAuditRequests();

  assert.ok(names(fixture.calls).includes('audit-result'));
  assert.ok(!names(fixture.calls).includes('upload-claim'));
});

test('a failed page read posts nothing and leaves the request to its lease', async () => {
  const fixture = makeFixture({
    deps: {
      runAuditSyncCommand: async () => ({ ok: false, error: '平台审核页面没有准备好', errorCode: 'audit-sync-page-not-ready' }),
    },
  });

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.equal(outcome.handled, 1);
  assert.ok(!names(fixture.calls).includes('audit-result'));
  assert.equal(fixture.closedTabs.length, 1);
  const reported = fixture.calls.find((call) => call[0] === 'report-error');
  assert.match(reported[2], /没有准备好/);
});

test('a page action that throws posts nothing and closes its tab', async () => {
  const fixture = makeFixture({
    deps: {
      runAuditSyncCommand: async () => {
        throw new Error('tab crashed');
      },
    },
  });

  const outcome = await fixture.controller.dispatchPendingAuditRequests();

  assert.equal(outcome.handled, 1);
  assert.ok(!names(fixture.calls).includes('audit-result'));
  assert.equal(fixture.closedTabs.length, 1);
});

test('a risk signal posts nothing and stops auditing that platform for the round', async () => {
  const fixture = makeFixture({
    connected: ['fanqie', 'qidian'],
    requests: [
      auditRequest({ request_id: 'audit-1', platform: 'fanqie' }),
      auditRequest({ request_id: 'audit-2', platform: 'qidian', upload_url: 'https://write.qq.com/portal/book/9', remote_url: 'https://write.qq.com/portal/book/9' }),
    ],
    deps: {
      runAuditSyncCommand: async (tabId, payload) => (
        payload.platform === 'fanqie'
          ? { ok: false, riskPause: true, riskReason: 'captcha' }
          : observation()
      ),
    },
  });

  await fixture.controller.dispatchPendingAuditRequests();

  const results = fixture.calls.filter((call) => call[0] === 'audit-result');
  assert.deepEqual(results.map((call) => call[1]), ['audit-2']);
  const claims = fixture.calls.filter((call) => call[0] === 'audit-claim');
  assert.deepEqual(claims[0][1].connected_platforms, ['fanqie', 'qidian']);
  assert.deepEqual(claims[1][1].connected_platforms, ['qidian']);
});

for (const status of [404, 409]) {
  test(`a result the backend refuses (${status}) is dropped, not retried or raised`, async () => {
    const refused = Object.assign(new Error('not owned'), {
      status,
      code: status === 404 ? 'audit_request_not_found' : 'audit_request_not_owned',
    });
    const fixture = makeFixture({
      backend: {
        submitAuditRequestResult: async () => {
          throw refused;
        },
      },
    });

    const outcome = await fixture.controller.dispatchPendingAuditRequests();

    assert.equal(outcome.handled, 1);
    assert.equal(fixture.closedTabs.length, 1);
  });
}

test('a result that cannot be delivered is raised so the round reports it', async () => {
  const fixture = makeFixture({
    backend: {
      submitAuditRequestResult: async () => {
        throw Object.assign(new Error('HTTP 500'), { status: 500 });
      },
    },
  });

  await assert.rejects(fixture.controller.dispatchPendingAuditRequests(), /HTTP 500/);
  assert.equal(fixture.closedTabs.length, 1);
});

// --- backend client and wiring ------------------------------------------------------------

function clientWith(responses) {
  const calls = [];
  const client = createBackendClient(
    async (url, options = {}) => {
      calls.push({ url, options });
      return responses.shift();
    },
    { backendBaseUrl: `${FORWIN_URL}/`, apiKey: ` ${FORWIN_KEY} `, hubApiKey: HUB_KEY },
  );
  return { client, calls };
}

test('the backend client claims audit requests with the ForWin key and reads 204 as nothing', async () => {
  const { client, calls } = clientWith([
    { ok: true, status: 204, json: async () => { throw new SyntaxError('no body'); } },
    { ok: true, status: 200, json: async () => auditRequest() },
  ]);

  const none = await client.claimNextAuditRequest({ client_id: CLIENT_ID, connected_platforms: ['fanqie'] });
  const one = await client.claimNextAuditRequest({ client_id: CLIENT_ID, connected_platforms: ['fanqie'] });

  assert.equal(none, null);
  assert.equal(one.request_id, 'audit-1');
  assert.equal(calls[0].url, `${FORWIN_URL}/api/publishers/extension/audit-requests/claim`);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.headers['X-Forwin-Extension-Key'], FORWIN_KEY);
  assert.deepEqual(JSON.parse(calls[0].options.body), { client_id: CLIENT_ID, connected_platforms: ['fanqie'] });
  for (const call of calls) {
    assert.ok(!JSON.stringify(call).includes(HUB_KEY), 'the hub key must never reach ForWin');
    assert.equal(call.options.headers.Authorization, undefined);
  }
});

test('the backend client posts the result to the encoded request path', async () => {
  const { client, calls } = clientWith([{ ok: true, status: 200, json: async () => ({ ok: true }) }]);

  await client.submitAuditRequestResult('audit/1 x', { client_id: CLIENT_ID, work: {} });

  assert.equal(
    calls[0].url,
    `${FORWIN_URL}/api/publishers/extension/audit-requests/audit%2F1%20x/result`,
  );
  assert.equal(calls[0].options.headers['X-Forwin-Extension-Key'], FORWIN_KEY);
  assert.ok(!JSON.stringify(calls[0]).includes(HUB_KEY));
});

test('the backend client surfaces a refused result with status and code', async () => {
  const { client } = clientWith([
    {
      ok: false,
      status: 409,
      json: async () => ({ detail: { code: 'audit_request_finished', message: 'already done' } }),
    },
  ]);

  await assert.rejects(
    client.submitAuditRequestResult('audit-1', { client_id: CLIENT_ID }),
    (error) => error.status === 409 && error.code === 'audit_request_finished',
  );
});

test('background wires the audit calls to the ForWin backend client', () => {
  const source = readFileSync(new URL('../background.js', import.meta.url), 'utf8');

  assert.match(source, /async claimNextAuditRequest\(payload\)\s*\{\s*return withBackendClient\(\(client\) => client\.claimNextAuditRequest\(payload\)\)/);
  assert.match(
    source,
    /async submitAuditRequestResult\(requestId, payload\)\s*\{\s*return withBackendClient\(\(client\) => client\.submitAuditRequestResult\(requestId, payload\)\)/,
  );
  assert.match(source, /controller\.heartbeatRound\(\)/);
});

test('the audit calls are not part of the hub client', () => {
  const source = readFileSync(new URL('../background.js', import.meta.url), 'utf8');
  const hubBackend = source.slice(source.indexOf('hubBackend: {'), source.indexOf('getHubClientId: ensureHubClientId'));

  assert.ok(hubBackend.length > 0);
  assert.doesNotMatch(hubBackend, /Audit/);
});
