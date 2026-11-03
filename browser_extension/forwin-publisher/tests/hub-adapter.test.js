import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import { createBackendClient, createHubClient } from '../lib/backend-client.js';
import { PublisherExtensionController } from '../lib/controller.js';
import {
  HUB_PLATFORMS,
  decodeFileUri,
  decodeHubBookMeta,
  hubPauseReason,
  storableJson,
  storableText,
  translateHubClaim,
} from '../lib/hub-adapter.js';
import { HUB_CLIENT_ID_STORAGE_KEY, createHubClientIdStore } from '../lib/settings.js';
import { createUploadJournal } from '../lib/upload-journal.js';

const NOW_MS = Date.parse('2026-11-01T00:00:00Z');
const HUB_URL = 'http://publish-hub-tunnel:18930';
const HUB_KEY = 'hub-secret-key-123';
const HUB_CLIENT_ID = 'magi3-ext-abcd1234';
const FORWIN_URL = 'http://forwin.local:8899';
const FORWIN_KEY = 'forwin-secret-456';
const FORWIN_CLIENT_ID = 'forwin-client-1';
const BODY = '第一段正文。\n第二段正文。';
const FENCE_CODES = ['stale_epoch', 'lease_expired', 'job_cancelled'];

function sha256(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function hubError(status, code, message = '') {
  return { status, body: { error: { code, message } } };
}

function claimReply(claim) {
  return { status: 200, body: claim };
}

function hubChapterClaim(overrides = {}) {
  const base = {
    job: {
      id: 'hub-job-1',
      status: 'pending',
      action: 'upload',
      content_kind: 'novel_chapter',
      source_system: 'forwin',
      source_ref: 'project/p-1/chapter/3',
      destination_id: 'dest-1',
      publication: { id: 'pub-1', remote_id: null, remote_url: null, official_state: 'created' },
      latest_receipt: null,
      last_error: null,
      attempt_count: 1,
      available_at: '2026-10-31T23:59:00Z',
      created_at: '2026-10-31T23:00:00Z',
      updated_at: '2026-10-31T23:59:30Z',
      finished_at: null,
      payload: {
        book_name: '测试之书',
        chapter_title: '第3章 风起',
        body_text: BODY,
        mode: 'draft',
      },
      content_refs: [],
      destination: {
        id: 'dest-1',
        kind: 'novel_book',
        remote_id: '7001',
        remote_url: 'https://fanqienovel.com/main/writer/chapter-manage/7001',
        settings: { book_name: '测试之书' },
      },
      account: { id: 'acct-1', platform: 'fanqie', credential_ref: 'fanqie-main' },
      receipts: [],
    },
    attempt_id: 'hub-attempt-1',
    epoch: 3,
    mode: 'execute',
    lease_expires_at: '2026-11-01T00:02:00Z',
  };
  return mergeClaim(base, overrides);
}

function hubCreateClaim(overrides = {}) {
  return hubChapterClaim(mergeOverrides({
    job: {
      id: 'hub-create-1',
      action: 'create_destination',
      content_kind: null,
      source_ref: null,
      publication: null,
      payload: {
        book_meta: {
          audience: 'male',
          category: '悬疑',
          tags: ['theme:悬疑推理', 'role:警察', 'plot:破案:旧案', '都市'],
          protagonist_names: ['林舟'],
          intro: '一桩旧案在港区重启，刑警林舟一步步逼近真相。',
        },
      },
      destination: {
        id: 'dest-new',
        kind: 'novel_book',
        remote_id: null,
        remote_url: null,
        settings: { book_name: '港区旧案' },
      },
    },
    attempt_id: 'hub-attempt-create',
  }, overrides));
}

function hubCoverClaim(overrides = {}) {
  return hubChapterClaim(mergeOverrides({
    job: {
      id: 'hub-cover-1',
      action: 'upload_cover',
      content_kind: 'novel_cover',
      source_ref: 'project/p-1/cover/asset-9',
      payload: {},
      content_refs: [
        {
          uri: 'file:///data/publisher/covers/%E5%B0%81%E9%9D%A2%201.png',
          sha256: 'c'.repeat(64),
          size: 1234,
          mime: 'image/png',
          role: 'cover',
        },
      ],
      destination: {
        id: 'dest-1',
        kind: 'novel_book',
        remote_id: '7001',
        remote_url: 'https://fanqienovel.com/main/writer/book-info/7001',
        settings: { book_name: '测试之书' },
      },
    },
    attempt_id: 'hub-attempt-cover',
  }, overrides));
}

function isPlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value);
}

function mergeOverrides(base, overrides) {
  const result = structuredClone(base);
  for (const [key, value] of Object.entries(overrides || {})) {
    if (isPlainObject(value) && isPlainObject(result[key]) && key !== 'payload' && key !== 'settings' && key !== 'book_meta') {
      result[key] = mergeOverrides(result[key], value);
    } else {
      result[key] = structuredClone(value);
    }
  }
  return result;
}

function mergeClaim(base, overrides) {
  return mergeOverrides(base, overrides);
}

function defaultHubReply(action, claims) {
  switch (action) {
    case 'executor-heartbeat':
      return { status: 204 };
    case 'claim':
      return claims.length ? claims.shift() : { status: 204 };
    case 'heartbeat':
      return { status: 200, body: { lease_expires_at: '2026-11-01T00:04:00Z' } };
    case 'phase':
      return { status: 204 };
    case 'receipt':
      return { status: 201, body: { id: 'receipt-1', official_state: 'drafted' } };
    default:
      return { status: 200, body: { id: 'hub-job-1', status: 'whatever' } };
  }
}

function makeFakeHub({ claims = [], on = {} } = {}) {
  const requests = [];
  const queue = [...claims];
  const fetchImpl = async (url, options = {}) => {
    const { pathname } = new URL(url);
    const body = options.body ? JSON.parse(options.body) : null;
    let action = 'unknown';
    if (pathname === '/v1/executor/heartbeat') {
      action = 'executor-heartbeat';
    } else if (pathname === '/v1/executor/upload-jobs/claim') {
      action = 'claim';
    } else {
      action = pathname.match(/^\/v1\/executor\/upload-jobs\/[^/]+\/attempts\/[^/]+\/([a-z_]+)$/)?.[1] || 'unknown';
    }
    const request = {
      action,
      pathname,
      url,
      body,
      headers: options.headers || {},
      method: options.method,
    };
    requests.push(request);
    let reply = on[action] ? await on[action](body, request, requests) : undefined;
    if (reply === undefined) {
      reply = defaultHubReply(action, queue);
    }
    const status = reply.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (reply.body === undefined) {
          throw new SyntaxError('Unexpected end of JSON input');
        }
        return structuredClone(reply.body);
      },
    };
  };
  return {
    fetchImpl,
    requests,
    actions: () => requests.map((item) => item.action),
    byAction: (action) => requests.filter((item) => item.action === action),
  };
}

function memoryJournalStore(initialSnapshot) {
  let snapshot = initialSnapshot == null ? initialSnapshot : structuredClone(initialSnapshot);
  return {
    createJournal: () => createUploadJournal({
      read: async () => (snapshot == null ? snapshot : structuredClone(snapshot)),
      write: async (next) => {
        snapshot = structuredClone(next);
      },
    }),
    get snapshot() {
      return snapshot == null ? snapshot : structuredClone(snapshot);
    },
  };
}

function okChapterResult(body = BODY, bookId = '7001') {
  return {
    ok: true,
    currentUrl: `https://fanqienovel.com/main/writer/chapter-manage/${bookId}?chapter_id=9001`,
    message: '章节草稿已保存。',
    resultPayload: {
      mode: 'draft',
      observed_content_sha256: sha256(body),
      expected_normalized_sha256: 'b'.repeat(64),
      observed_normalized_sha256: 'b'.repeat(64),
      content_match_basis: 'normalized-editor-text-sha256',
    },
  };
}

function okCoverResult() {
  return {
    ok: true,
    currentUrl: 'https://fanqienovel.com/main/writer/book-info/7001',
    message: '封面上传成功',
    resultPayload: {
      cover_state: 'uploaded',
      acceptance_signal: '上传成功',
      remote_book_id: '7001',
    },
  };
}

function okCreateResult() {
  return {
    ok: true,
    currentUrl: 'https://fanqienovel.com/main/writer/book-info/8001',
    message: '书本已创建。',
    remote_book_id: '8001',
    remote_url: 'https://fanqienovel.com/main/writer/book-info/8001',
    resultPayload: {
      task_kind: 'create_book',
      verified_via: 'book-info-url',
      remote_book_id: '8001',
      remote_url: 'https://fanqienovel.com/main/writer/book-info/8001',
      cover_deferred: false,
    },
  };
}

function makeHubController({
  hub = makeFakeHub(),
  settings = {},
  deps = {},
  backend = {},
  journalStore = memoryJournalStore(),
} = {}) {
  const calls = [];
  const forwinCalls = [];
  const hubRounds = [];
  const reportedErrors = [];
  const openedTabs = [];
  const closedTabs = [];
  const tabs = new Map();
  let nextTabId = 100;
  const fullSettings = {
    backendBaseUrl: FORWIN_URL,
    apiKey: FORWIN_KEY,
    syncSessionToBackend: true,
    hubBaseUrl: HUB_URL,
    hubApiKey: HUB_KEY,
    ...settings,
  };
  const defaultBackend = {
    heartbeat: async (payload) => {
      forwinCalls.push(['heartbeat', payload]);
      return { ok: true };
    },
    syncBrowserSession: async (payload) => {
      forwinCalls.push(['syncBrowserSession', payload]);
      return { ok: true };
    },
    getBrowserSession: async () => null,
    claimNextUploadJob: async (payload) => {
      forwinCalls.push(['claim', payload]);
      return { found: false, claim: null };
    },
    heartbeatUploadAttempt: async () => ({ attempt_status: 'running', job_status: 'running' }),
    updateUploadAttemptPhase: async (...args) => {
      forwinCalls.push(['phase', ...args]);
      return { attempt_status: 'running', job_status: 'running', next_action: 'heartbeat' };
    },
    submitUploadAttemptResult: async (...args) => {
      forwinCalls.push(['result', ...args]);
      return { attempt_status: 'succeeded', job_status: 'succeeded' };
    },
    submitUploadReceipt: async (...args) => {
      forwinCalls.push(['receipt', ...args]);
      return { attempt_status: 'running', job_status: 'running' };
    },
    reconcileUploadAttempt: async (...args) => {
      forwinCalls.push(['reconcile', ...args]);
      return { attempt_status: 'succeeded', job_status: 'succeeded' };
    },
    pauseUploadAttempt: async (...args) => {
      forwinCalls.push(['pause', ...args]);
      return { attempt_status: 'paused', job_status: 'paused' };
    },
    claimNextCommentSyncJob: async (payload) => {
      forwinCalls.push(['comment-claim', payload]);
      return { found: false, job: null };
    },
  };
  const controllerDeps = {
    ensureClientId: async () => FORWIN_CLIENT_ID,
    getClientId: async () => FORWIN_CLIENT_ID,
    getHubClientId: async () => HUB_CLIENT_ID,
    ensureHeartbeatAlarm: async () => {},
    getSettings: async () => ({ ...fullSettings }),
    getExtensionVersion: () => '0.1.66',
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
      calls.push(['open', url]);
      return { tabId };
    },
    getTab: async (tabId) => (tabs.has(tabId) ? { id: tabId, url: tabs.get(tabId), status: 'complete' } : null),
    closeTab: async (tabId) => {
      closedTabs.push(tabId);
    },
    waitForRunnableTab: async (platform, tabId) => {
      calls.push(['wait-runnable', platform, tabId]);
      return { tabId, url: tabs.get(tabId), ready: true };
    },
    inspectPlatformRiskCommand: async (tabId, context) => {
      calls.push(['risk', context?.boundary]);
      return { detected: false };
    },
    runUploadCommand: async (tabId, payload) => {
      calls.push(['run-upload', payload, hub.actions().slice()]);
      return okChapterResult();
    },
    runCoverUploadCommand: async (tabId, payload) => {
      calls.push(['run-cover-upload', payload, hub.actions().slice()]);
      return okCoverResult();
    },
    runCreateBookCommand: async (tabId, payload) => {
      calls.push(['run-create-book', payload, hub.actions().slice()]);
      return okCreateResult();
    },
    runReconciliationCommand: async (tabId, payload) => {
      calls.push(['reconcile-upload', payload, hub.actions().slice()]);
      return { outcome: 'indeterminate', reason: 'nothing conclusive' };
    },
    runReconcileCreateCommand: async (tabId, payload) => {
      calls.push(['reconcile-create', payload, hub.actions().slice()]);
      return { finding: 'unknown', candidates: [], works_list_complete: false, reason: 'seam' };
    },
    uploadJournal: journalStore.createJournal(),
    hubBackend: createHubClient(hub.fetchImpl, fullSettings),
    hubHeartbeatIntervalMs: 3_600_000,
    nowMs: () => NOW_MS,
    recordHubRound: async (round) => {
      hubRounds.push({ ok: round.ok, error: round.error ? round.error.message : '' });
    },
    reportError: async (context, error) => {
      reportedErrors.push({ context, message: error instanceof Error ? error.message : String(error) });
    },
    ...deps,
    backend: { ...defaultBackend, ...backend },
  };
  const controller = new PublisherExtensionController(controllerDeps);
  controller._collectConnectedPlatforms = async () => ['qidian', 'fanqie'];
  return {
    controller,
    hub,
    calls,
    forwinCalls,
    hubRounds,
    reportedErrors,
    openedTabs,
    closedTabs,
    journalStore,
  };
}

function hubRecords(journalStore) {
  return (journalStore.snapshot?.records || []).filter((record) => record.backend === 'hub');
}

function finalBody(hub, action) {
  const requests = hub.byAction(action);
  assert.equal(requests.length, 1, `expected one ${action} request, saw ${hub.actions().join(',')}`);
  return requests[0].body;
}

// --- translation --------------------------------------------------------------------------

test('translateHubClaim maps a hub chapter claim into the internal claim shape', async () => {
  const internal = await translateHubClaim(hubChapterClaim());

  assert.equal(internal.execution_mode, 'execute');
  assert.equal(internal.job.job_id, 'hub-job-1');
  assert.equal(internal.job.task_kind, 'chapter_upload');
  assert.equal(internal.job.platform, 'fanqie');
  assert.equal(internal.job.content_sha256, sha256(BODY));
  assert.deepEqual(internal.job.input, {
    book_name: '测试之书',
    chapter_title: '第3章 风起',
    body: BODY,
    publish: false,
    create_if_missing: false,
  });
  assert.equal(internal.job.hub.destination_id, 'dest-1');
  assert.equal(internal.job.hub.destination_remote_id, '7001');
  assert.equal(internal.attempt.attempt_id, 'hub-attempt-1');
  assert.equal(internal.attempt.lease_epoch, 3);
  assert.equal(internal.attempt.phase, 'claimed');
  assert.equal(internal.attempt.heartbeat_interval_seconds, 30);

  const publish = await translateHubClaim(hubChapterClaim({
    job: { payload: { book_name: '测试之书', chapter_title: 'c', body_text: 'b', mode: 'publish' } },
  }));
  assert.equal(publish.job.input.publish, true);

  const reconcile = await translateHubClaim(hubChapterClaim({ mode: 'reconcile', epoch: 9 }));
  assert.equal(reconcile.execution_mode, 'reconcile');
  assert.equal(reconcile.attempt.lease_epoch, 9);

  await assert.rejects(translateHubClaim(hubChapterClaim({ mode: 'mystery' })), /mode/);
});

test('translateHubClaim maps actions to task kinds and builds create and cover inputs', async () => {
  const create = await translateHubClaim(hubCreateClaim());
  assert.equal(create.job.task_kind, 'create_book');
  assert.deepEqual(create.job.input, {
    platform: 'fanqie',
    book_name: '港区旧案',
    book_meta: {
      audience: 'male',
      primary_category: '悬疑',
      theme_tags: ['悬疑推理'],
      role_tags: ['警察'],
      plot_tags: ['破案:旧案'],
      tags: ['都市'],
      protagonist_names: ['林舟'],
      intro: '一桩旧案在港区重启，刑警林舟一步步逼近真相。',
    },
  });
  assert.match(create.job.content_sha256, /^[0-9a-f]{64}$/);

  const cover = await translateHubClaim(hubCoverClaim());
  assert.equal(cover.job.task_kind, 'cover_upload');
  assert.equal(cover.job.content_sha256, 'c'.repeat(64));
  assert.deepEqual(cover.job.input, {
    book_name: '测试之书',
    remote_book_id: '7001',
    remote_url: 'https://fanqienovel.com/main/writer/book-info/7001',
    upload_url: 'https://fanqienovel.com/main/writer/book-info/7001',
    file_path: '/data/publisher/covers/封面 1.png',
  });

  const unsupported = await translateHubClaim(hubChapterClaim({ job: { action: 'update_visibility' } }));
  assert.equal(unsupported.job.task_kind, '');
  const video = await translateHubClaim(hubChapterClaim({ job: { content_kind: 'video' } }));
  assert.equal(video.job.task_kind, '');
});

test('decodeHubBookMeta decodes tag groups on the first colon and keeps plain tags flat', () => {
  assert.deepEqual(decodeHubBookMeta({
    audience: ' female ',
    category: '都市',
    tags: ['theme:甜宠', 'role:霸总', 'plot:a:b', 'plain', 'other:x', ':lead', 'theme:', '  '],
    protagonist_names: ['甲', ' ', '乙'],
    intro: ' 简介 ',
  }), {
    audience: 'female',
    primary_category: '都市',
    theme_tags: ['甜宠'],
    role_tags: ['霸总'],
    plot_tags: ['a:b'],
    tags: ['plain', 'other:x', ':lead'],
    protagonist_names: ['甲', '乙'],
    intro: '简介',
  });
  assert.deepEqual(decodeHubBookMeta({}), {});
  assert.deepEqual(decodeHubBookMeta(null), {});
  assert.deepEqual(decodeHubBookMeta({ audience: '', category: '', tags: [], protagonist_names: [], intro: '' }), {});
});

test('decodeFileUri turns the percent-encoded file uri into the container path', () => {
  assert.equal(
    decodeFileUri('file:///data/publisher/covers/%E5%B0%81%E9%9D%A2%201.png'),
    '/data/publisher/covers/封面 1.png',
  );
  assert.equal(decodeFileUri('file:///data/a%25b.png'), '/data/a%b.png');
  for (const invalid of ['', 'http://x/a.png', 'file://host/a.png', 'file:///a%ZZ.png', '/data/a.png', 'file:///a%00b.png', null]) {
    assert.equal(decodeFileUri(invalid), '', String(invalid));
  }
});

test('hubPauseReason maps the extension risk reasons onto the hub pause reasons', () => {
  assert.equal(hubPauseReason('captcha'), 'captcha');
  assert.equal(hubPauseReason('mfa'), 'mfa');
  assert.equal(hubPauseReason('account_risk'), 'risk');
  assert.equal(hubPauseReason(''), 'risk');
  assert.equal(hubPauseReason('something-else'), 'risk');
  assert.deepEqual(HUB_PLATFORMS, ['qidian', 'fanqie']);
});

// --- HTTP client ----------------------------------------------------------------------------

test('hub client posts executor calls with the bearer key and never the forwin key', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const client = createHubClient(hub.fetchImpl, {
    backendBaseUrl: FORWIN_URL,
    apiKey: FORWIN_KEY,
    hubBaseUrl: `${HUB_URL}/`,
    hubApiKey: ` ${HUB_KEY} `,
  });

  assert.equal(await client.heartbeat({ client_id: 'c', platforms: ['qidian'], version: '1' }), null);
  const claim = await client.claim({ client_id: 'c', platforms: ['qidian'] });
  assert.equal(claim.attempt_id, 'hub-attempt-1');
  assert.equal(await client.claim({ client_id: 'c', platforms: ['qidian'] }), null);
  await client.attempt('job/1', 'attempt 2', 'result', { client_id: 'c', epoch: 1, outcome: 'succeeded' });

  assert.deepEqual(hub.requests.map((item) => item.url), [
    `${HUB_URL}/v1/executor/heartbeat`,
    `${HUB_URL}/v1/executor/upload-jobs/claim`,
    `${HUB_URL}/v1/executor/upload-jobs/claim`,
    `${HUB_URL}/v1/executor/upload-jobs/job%2F1/attempts/attempt%202/result`,
  ]);
  for (const request of hub.requests) {
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.Authorization, `Bearer ${HUB_KEY}`);
    assert.equal(request.headers['X-Forwin-Extension-Key'], undefined);
    assert.doesNotMatch(JSON.stringify(request.headers), new RegExp(FORWIN_KEY));
  }
});

test('hub client exposes the error envelope code and status', async () => {
  const hub = makeFakeHub({ claims: [hubError(409, 'attempt_in_progress', 'busy')] });
  const client = createHubClient(hub.fetchImpl, { hubBaseUrl: HUB_URL, hubApiKey: HUB_KEY });
  await assert.rejects(
    client.claim({ client_id: 'c', platforms: ['fanqie'] }),
    (error) => error.status === 409 && error.code === 'attempt_in_progress' && error.message === 'busy',
  );
});

test('forwin client never sends the hub key', async () => {
  const calls = [];
  const client = createBackendClient(async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({ ok: true }) };
  }, { backendBaseUrl: FORWIN_URL, apiKey: FORWIN_KEY, hubBaseUrl: HUB_URL, hubApiKey: HUB_KEY });
  await client.heartbeat({ client_id: 'x' });
  await client.claimNextUploadJob({ client_id: 'x' });
  for (const call of calls) {
    assert.doesNotMatch(JSON.stringify(call), new RegExp(HUB_KEY));
    assert.equal(call.options.headers.Authorization, undefined);
  }
});

// --- chapter execution ----------------------------------------------------------------------

test('hub chapter job: mutation_started is reported before run-upload, then receipt and success', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const journalStore = memoryJournalStore();
  let journalPhaseAtMutation = '';
  const fixture = makeHubController({
    hub,
    journalStore,
    deps: {
      runUploadCommand: async (tabId, payload) => {
        journalPhaseAtMutation = journalStore.snapshot.records[0].local_phase;
        fixture.calls.push(['run-upload', payload, hub.actions().slice()]);
        return okChapterResult();
      },
    },
  });

  const result = await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(result, { found: true, handled: 1 });
  assert.deepEqual(hub.actions(), ['executor-heartbeat', 'claim', 'phase', 'receipt', 'result', 'claim']);
  assert.deepEqual(hub.byAction('executor-heartbeat')[0].body, {
    client_id: HUB_CLIENT_ID,
    platforms: ['qidian', 'fanqie'],
    version: '0.1.66',
  });
  assert.deepEqual(hub.byAction('claim')[0].body, { client_id: HUB_CLIENT_ID, platforms: ['qidian', 'fanqie'] });
  assert.match(hub.byAction('phase')[0].pathname, /\/upload-jobs\/hub-job-1\/attempts\/hub-attempt-1\/phase$/);
  assert.deepEqual(hub.byAction('phase')[0].body, {
    client_id: HUB_CLIENT_ID,
    epoch: 3,
    phase: 'mutation_started',
  });

  const run = fixture.calls.find((call) => call[0] === 'run-upload');
  assert.ok(run, 'run-upload was dispatched');
  assert.deepEqual(run[2], ['executor-heartbeat', 'claim', 'phase']);
  assert.equal(journalPhaseAtMutation, 'mutation_started');
  assert.deepEqual(run[1], {
    platform: 'fanqie',
    task_kind: 'chapter_upload',
    content_sha256: sha256(BODY),
    book_name: '测试之书',
    chapter_title: '第3章 风起',
    body: BODY,
    publish: false,
    create_if_missing: false,
  });
  const risk = fixture.calls.findIndex((call) => call[0] === 'risk');
  assert.ok(risk >= 0 && risk < fixture.calls.indexOf(run));
  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/']);

  const receipt = finalBody(hub, 'receipt');
  assert.equal(receipt.client_id, HUB_CLIENT_ID);
  assert.equal(receipt.epoch, 3);
  assert.equal(receipt.official_state, 'drafted');
  assert.equal(receipt.remote_id, '9001');
  assert.equal(receipt.remote_url, 'https://fanqienovel.com/main/writer/chapter-manage/7001?chapter_id=9001');
  assert.equal(receipt.raw.remote_book_id, '7001');
  assert.equal(receipt.raw.remote_chapter_id, '9001');
  assert.equal(receipt.raw.content_sha256, sha256(BODY));
  assert.equal(receipt.raw.evidence.heading, '第3章 风起');
  assert.equal(receipt.lease_epoch, undefined);

  assert.deepEqual(finalBody(hub, 'result'), { client_id: HUB_CLIENT_ID, epoch: 3, outcome: 'succeeded' });
  const records = hubRecords(journalStore);
  assert.equal(records.length, 1);
  assert.equal(records[0].local_phase, 'acked');
  assert.equal(records[0].client_id, HUB_CLIENT_ID);
  assert.ok(fixture.closedTabs.includes(fixture.openedTabs[0].tabId));
});

test('hub chapter job uses the destination upload_url setting as the start page', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubChapterClaim({
      job: {
        destination: {
          settings: { book_name: '测试之书', upload_url: 'https://fanqienovel.com/main/writer/book-manage' },
        },
      },
    }))],
  });
  const fixture = makeHubController({ hub });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/book-manage']);
});

test('hub chapter and cover jobs without a destination remote_id are deferred before any mutation', async () => {
  for (const claim of [
    hubChapterClaim({ job: { destination: { remote_id: null } } }),
    hubCoverClaim({ job: { destination: { remote_id: '' } } }),
  ]) {
    const hub = makeFakeHub({ claims: [claimReply(claim)] });
    const fixture = makeHubController({ hub });

    await fixture.controller.dispatchPendingUploadJobs();

    assert.deepEqual(hub.actions(), ['executor-heartbeat', 'claim', 'result', 'claim']);
    const body = finalBody(hub, 'result');
    assert.equal(body.outcome, 'defer');
    assert.equal(body.defer_until, '2026-11-01T00:05:00.000Z');
    assert.equal(body.epoch, 3);
    assert.equal(fixture.openedTabs.length, 0);
    assert.equal(fixture.calls.filter((call) => call[0].startsWith('run-')).length, 0);
  }
});

for (const code of FENCE_CODES) {
  test(`hub job is abandoned without touching the platform when mutation_started gets ${code}`, async () => {
    const hub = makeFakeHub({
      claims: [claimReply(hubChapterClaim())],
      on: { phase: () => hubError(409, code) },
    });
    const journalStore = memoryJournalStore();
    const fixture = makeHubController({ hub, journalStore });

    await fixture.controller.dispatchPendingUploadJobs();

    assert.equal(fixture.calls.filter((call) => call[0] === 'run-upload').length, 0);
    assert.equal(hub.byAction('result').length, 0);
    assert.equal(hub.byAction('receipt').length, 0);
    assert.equal(hub.byAction('pause').length, 0);
    assert.equal(hubRecords(journalStore).length, 0);
  });
}

test('hub job stops reporting and closes its tab when the attempt heartbeat is fenced mid-mutation', async () => {
  let mutating = false;
  let fenced = 0;
  const hub = makeFakeHub({
    claims: [claimReply(hubChapterClaim())],
    on: {
      heartbeat: () => {
        if (!mutating) {
          return undefined;
        }
        fenced += 1;
        return hubError(409, 'stale_epoch');
      },
    },
  });
  const journalStore = memoryJournalStore();
  const fixture = makeHubController({
    hub,
    journalStore,
    deps: {
      hubHeartbeatIntervalMs: 5,
      runUploadCommand: async () => {
        mutating = true;
        const deadline = Date.now() + 2000;
        while (!fenced && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 5));
        }
        await new Promise((resolve) => setTimeout(resolve, 20));
        return okChapterResult();
      },
    },
  });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.equal(fenced, 1);
  assert.equal(hub.byAction('phase').length, 1);
  assert.equal(hub.byAction('heartbeat').at(-1).body.epoch, 3);
  assert.equal(hub.byAction('receipt').length, 0);
  assert.equal(hub.byAction('result').length, 0);
  assert.equal(hubRecords(journalStore).length, 0);
  assert.ok(fixture.closedTabs.includes(fixture.openedTabs[0].tabId));
});

test('hub receipt answered with remote_id_conflict pauses the job for a human', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubChapterClaim())],
    on: { receipt: () => hubError(409, 'remote_id_conflict', 'another remote id on record') },
  });
  const journalStore = memoryJournalStore();
  const fixture = makeHubController({ hub, journalStore });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(hub.actions().slice(2), ['phase', 'receipt', 'pause', 'claim']);
  const pause = finalBody(hub, 'pause');
  assert.equal(pause.reason, 'needs_human');
  assert.equal(pause.epoch, 3);
  assert.match(pause.detail, /remote_id_conflict/);
  assert.equal(hubRecords(journalStore)[0].local_phase, 'acked');
});

test('hub chapter saved into another book is paused for a human, never receipted', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const fixture = makeHubController({
    hub,
    deps: { runUploadCommand: async () => okChapterResult(BODY, '7999') },
  });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.equal(hub.byAction('receipt').length, 0);
  assert.equal(hub.byAction('result').length, 0);
  const pause = finalBody(hub, 'pause');
  assert.equal(pause.reason, 'needs_human');
  assert.match(pause.detail, /7999/);
  assert.match(pause.detail, /7001/);
});

test('hub chapter whose book name differs from the destination fails before any mutation', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubChapterClaim({
      job: { destination: { settings: { book_name: '另一本书' } } },
    }))],
  });
  const fixture = makeHubController({ hub });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.equal(hub.byAction('phase').length, 0);
  assert.equal(fixture.openedTabs.length, 0);
  const body = finalBody(hub, 'result');
  assert.equal(body.outcome, 'permanent_error');
  assert.equal(body.error.code, 'book_name_mismatch');
});

test('hub chapter failure after mutation_started is retryable, a risk signal pauses', async () => {
  const cases = [
    {
      result: { ok: false, errorCode: 'platform-mutation-timeout', error: 'timeout', currentUrl: '' },
      expect: (hub) => {
        const body = finalBody(hub, 'result');
        assert.equal(body.outcome, 'retryable_error');
        assert.equal(body.error.code, 'platform-mutation-timeout');
      },
    },
    {
      result: { ...okChapterResult(), resultPayload: {} },
      expect: (hub) => {
        assert.equal(hub.byAction('receipt').length, 0);
        const body = finalBody(hub, 'result');
        assert.equal(body.outcome, 'retryable_error');
        assert.equal(body.error.code, 'receipt-evidence-missing');
      },
    },
    {
      result: { ...okChapterResult(), resultPayload: { ...okChapterResult().resultPayload, risk_reason: 'captcha' } },
      expect: (hub) => {
        assert.equal(finalBody(hub, 'receipt').remote_id, '9001');
        assert.equal(finalBody(hub, 'result').outcome, 'succeeded');
        assert.equal(hub.byAction('pause').length, 0);
      },
    },
    {
      result: {
        ok: false,
        riskPause: true,
        riskReason: 'captcha',
        currentUrl: 'https://fanqienovel.com/main/writer/',
        riskEvidence: { boundary: 'before-save', message: '请完成安全验证' },
      },
      expect: (hub) => {
        const pause = finalBody(hub, 'pause');
        assert.equal(pause.reason, 'captcha');
        assert.match(pause.detail, /安全验证/);
        assert.equal(hub.byAction('result').length, 0);
      },
    },
  ];
  for (const item of cases) {
    const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
    const fixture = makeHubController({ hub, deps: { runUploadCommand: async () => item.result } });
    await fixture.controller.dispatchPendingUploadJobs();
    assert.equal(hub.byAction('phase').length, 1);
    item.expect(hub);
  }
});

test('hub pre-mutation risk pause and failed risk inspection never report mutation_started', async () => {
  const pauseHub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const paused = makeHubController({
    hub: pauseHub,
    deps: {
      inspectPlatformRiskCommand: async () => ({
        detected: true,
        riskPause: true,
        riskReason: 'account_risk',
        riskEvidence: { message: '账号异常' },
      }),
    },
  });
  await paused.controller.dispatchPendingUploadJobs();
  assert.equal(pauseHub.byAction('phase').length, 0);
  assert.equal(finalBody(pauseHub, 'pause').reason, 'risk');
  assert.equal(paused.calls.filter((call) => call[0] === 'run-upload').length, 0);

  const failHub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const failed = makeHubController({
    hub: failHub,
    deps: { inspectPlatformRiskCommand: async () => ({ ok: false, errorCode: 'platform-agent-timeout' }) },
  });
  await failed.controller.dispatchPendingUploadJobs();
  assert.equal(failHub.byAction('phase').length, 0);
  assert.equal(finalBody(failHub, 'result').outcome, 'retryable_error');
});

test('hub mutation_started that is not acknowledged never mutates', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubChapterClaim())],
    on: { phase: () => ({ status: 502, body: { error: { code: 'bad_gateway', message: 'tunnel down' } } }) },
  });
  const fixture = makeHubController({ hub });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.equal(fixture.calls.filter((call) => call[0] === 'run-upload').length, 0);
  assert.equal(finalBody(hub, 'result').outcome, 'retryable_error');
});

// --- cover execution ------------------------------------------------------------------------

test('hub cover job decodes the file uri and reports a cover receipt', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubCoverClaim())] });
  const fixture = makeHubController({ hub });

  await fixture.controller.dispatchPendingUploadJobs();

  const run = fixture.calls.find((call) => call[0] === 'run-cover-upload');
  assert.equal(run[1].file_path, '/data/publisher/covers/封面 1.png');
  assert.equal(run[1].remote_book_id, '7001');
  assert.deepEqual(run[2].slice(-1), ['phase']);
  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/book-info/7001']);
  const receipt = finalBody(hub, 'receipt');
  assert.equal(receipt.official_state, 'cover_uploaded');
  assert.equal(receipt.remote_id, undefined);
  assert.equal(receipt.raw.remote_book_id, '7001');
  assert.equal(finalBody(hub, 'result').outcome, 'succeeded');
});

test('hub cover job with an unreadable file uri fails before any mutation', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubCoverClaim({
      job: { content_refs: [{ uri: 'http://elsewhere/cover.png', role: 'cover', sha256: 'c'.repeat(64) }] },
    }))],
  });
  const fixture = makeHubController({ hub });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.equal(hub.byAction('phase').length, 0);
  assert.equal(finalBody(hub, 'result').outcome, 'permanent_error');
});

// --- create_book orchestration --------------------------------------------------------------

test('hub create job opens a fresh create tab, reports mutation_started, then receipts the new book id', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim())] });
  const fixture = makeHubController({ hub });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/create']);
  const order = fixture.calls.map((call) => call[0]);
  assert.ok(order.indexOf('wait-runnable') < order.indexOf('run-create-book'));
  const run = fixture.calls.find((call) => call[0] === 'run-create-book');
  assert.deepEqual(run[2].slice(-1), ['phase']);
  assert.deepEqual(run[1], {
    platform: 'fanqie',
    book_name: '港区旧案',
    book_meta: {
      audience: 'male',
      primary_category: '悬疑',
      theme_tags: ['悬疑推理'],
      role_tags: ['警察'],
      plot_tags: ['破案:旧案'],
      tags: ['都市'],
      protagonist_names: ['林舟'],
      intro: '一桩旧案在港区重启，刑警林舟一步步逼近真相。',
    },
  });
  assert.equal(finalBody(hub, 'phase').phase, 'mutation_started');
  const receipt = finalBody(hub, 'receipt');
  assert.equal(receipt.official_state, 'created');
  assert.equal(receipt.remote_id, '8001');
  assert.equal(receipt.remote_url, 'https://fanqienovel.com/main/writer/book-info/8001');
  assert.equal(receipt.raw.verified_via, 'book-info-url');
  assert.equal(finalBody(hub, 'result').outcome, 'succeeded');
});

test('hub create job on qidian opens the qidian create page', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubCreateClaim({ job: { account: { platform: 'qidian' } } }))],
  });
  const fixture = makeHubController({
    hub,
    deps: {
      runCreateBookCommand: async () => ({
        ...okCreateResult(),
        remote_book_id: '1234567',
        remote_url: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/1234567',
      }),
    },
  });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(fixture.openedTabs.map((tab) => tab.url), ['https://write.qq.com/portal/dashboard/create-novel?from=S5']);
  assert.equal(finalBody(hub, 'receipt').remote_id, '1234567');
});

test('hub create outcomes map onto executor reports after mutation_started', async () => {
  const cases = [
    {
      name: 'outcome unknown',
      reply: { ok: false, errorCode: 'create-book-outcome-unknown', resultPayload: { submitted: true } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'outcome unknown with a risk signal',
      reply: { ok: false, errorCode: 'create-book-outcome-unknown', resultPayload: { submitted: true, risk_reason: 'captcha' } },
      report: 'pause',
      reason: 'captcha',
    },
    {
      name: 'explicit rate limit refusal',
      reply: { ok: false, errorCode: 'create-book-rate-limited', resultPayload: { submitted: true, platform_reason: 'daily-create-limit' } },
      report: 'result',
      outcome: 'permanent_error',
    },
    {
      name: 'explicit validation refusal',
      reply: { ok: false, errorCode: 'create-book-validation-failed', resultPayload: { submitted: true } },
      report: 'result',
      outcome: 'permanent_error',
    },
    {
      name: 'not submitted',
      reply: { ok: false, errorCode: 'create-book-page-not-ready', resultPayload: { submitted: false } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'validation failed before the click',
      reply: { ok: false, errorCode: 'create-book-validation-failed', resultPayload: { submitted: false } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'already running',
      reply: { ok: false, errorCode: 'create-book-already-running', resultPayload: { in_flight: true, submitted: true } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'message timeout',
      reply: { ok: false, errorCode: 'platform-mutation-timeout', resultPayload: { phase: 'mutation-message-timeout' } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'tab disconnected',
      reply: { ok: false, errorCode: 'platform-mutation-disconnected' },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'risk pause inside the action',
      reply: { ok: false, riskPause: true, riskReason: 'mfa', riskEvidence: { message: '短信验证' } },
      report: 'pause',
      reason: 'mfa',
    },
    {
      name: 'success without a book id',
      reply: { ok: true, message: '书本已创建。', resultPayload: { task_kind: 'create_book' } },
      report: 'result',
      outcome: 'retryable_error',
    },
    {
      name: 'command threw',
      reply: new Error('sendMessage failed'),
      report: 'result',
      outcome: 'retryable_error',
    },
  ];
  for (const item of cases) {
    const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim())] });
    let runs = 0;
    const fixture = makeHubController({
      hub,
      deps: {
        runCreateBookCommand: async () => {
          runs += 1;
          if (item.reply instanceof Error) {
            throw item.reply;
          }
          return item.reply;
        },
      },
    });
    await fixture.controller.dispatchPendingUploadJobs();
    assert.equal(runs, 1, item.name);
    assert.equal(hub.byAction('phase').length, 1, item.name);
    assert.equal(hub.byAction('receipt').length, 0, item.name);
    const body = finalBody(hub, item.report);
    if (item.outcome) {
      assert.equal(body.outcome, item.outcome, item.name);
    }
    if (item.reason) {
      assert.equal(body.reason, item.reason, item.name);
    }
  }
});

test('hub create job without usable meta, name or with a book already on record fails before any mutation', async () => {
  const meta = hubCreateClaim().job.payload.book_meta;
  const cases = [
    { job: { payload: { book_meta: { audience: '', category: '', tags: [], protagonist_names: [], intro: '' } } }, code: 'book_meta_unusable' },
    { job: { payload: { book_meta: { ...meta, intro: '' } } }, code: 'book_meta_unusable' },
    { job: { payload: { book_meta: { ...meta, category: '  ' } } }, code: 'book_meta_unusable' },
    { job: { payload: {} }, code: 'book_meta_unusable' },
    { job: { destination: { settings: {} } }, code: 'book_name_missing' },
    { job: { destination: { remote_id: '8001' } }, code: 'destination_already_created' },
  ];
  for (const item of cases) {
    const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim(item))] });
    const fixture = makeHubController({ hub });
    await fixture.controller.dispatchPendingUploadJobs();
    assert.equal(hub.byAction('phase').length, 0, item.code);
    assert.equal(fixture.openedTabs.length, 0, item.code);
    assert.equal(fixture.calls.filter((call) => call[0] === 'run-create-book').length, 0, item.code);
    const body = finalBody(hub, 'result');
    assert.equal(body.outcome, 'permanent_error', item.code);
    assert.equal(body.error.code, item.code);
  }
});

test('hub create job whose create page never becomes runnable is retried before any mutation', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim())] });
  const fixture = makeHubController({ hub, deps: { waitForRunnableTab: async () => null } });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.equal(hub.byAction('phase').length, 0);
  assert.equal(fixture.calls.filter((call) => call[0] === 'run-create-book').length, 0);
  assert.equal(finalBody(hub, 'result').outcome, 'retryable_error');
});

test('hub create job redirected away from the create page is retried before any mutation', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim())] });
  const fixture = makeHubController({
    hub,
    deps: {
      getTab: async (tabId) => ({ id: tabId, url: 'https://fanqienovel.com/main/writer/login', status: 'complete' }),
      waitForRunnableTab: async (platform, tabId) => ({ tabId, url: 'https://fanqienovel.com/main/writer/login', ready: true }),
    },
  });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.equal(hub.byAction('phase').length, 0);
  assert.equal(finalBody(hub, 'result').outcome, 'retryable_error');
});

// --- reconcile mode -------------------------------------------------------------------------

test('hub chapter reconcile: observation_started first, matched becomes receipt plus found', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim({ mode: 'reconcile', epoch: 4 }))] });
  const fixture = makeHubController({
    hub,
    deps: {
      runReconciliationCommand: async (tabId, payload) => {
        fixture.calls.push(['reconcile-upload', payload, hub.actions().slice()]);
        return {
          outcome: 'matched',
          currentUrl: 'https://fanqienovel.com/main/writer/chapter-manage/7001?chapter_id=9001',
          matchedContentSha256: sha256(BODY),
          officialState: 'drafted',
          match: 'chapter-title-and-content-sha256',
          matchBasis: ['chapter_title', 'content_sha256'],
        };
      },
    },
  });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(hub.actions(), ['executor-heartbeat', 'claim', 'phase', 'receipt', 'reconcile', 'claim']);
  assert.deepEqual(finalBody(hub, 'phase'), { client_id: HUB_CLIENT_ID, epoch: 4, phase: 'observation_started' });
  const run = fixture.calls.find((call) => call[0] === 'reconcile-upload');
  assert.deepEqual(run[2].slice(-1), ['phase']);
  assert.equal(run[1].content_sha256, sha256(BODY));
  assert.equal(finalBody(hub, 'receipt').remote_id, '9001');
  assert.deepEqual(finalBody(hub, 'reconcile'), { client_id: HUB_CLIENT_ID, epoch: 4, finding: 'found' });
  assert.equal(fixture.calls.filter((call) => call[0] === 'run-upload').length, 0);
});

test('hub chapter reconcile maps indeterminate, absent and foreign books to unknown and risk to a pause', async () => {
  const cases = [
    { observation: { outcome: 'indeterminate', reason: 'not found' }, finding: 'unknown' },
    { observation: { outcome: 'absent', reason: 'not in list' }, finding: 'unknown' },
    {
      observation: {
        outcome: 'matched',
        currentUrl: 'https://fanqienovel.com/main/writer/chapter-manage/7999?chapter_id=9001',
        matchedContentSha256: sha256(BODY),
        matchBasis: ['chapter_title', 'content_sha256'],
      },
      finding: 'unknown',
    },
    { observation: { outcome: 'risk_pause', riskReason: 'captcha', reason: '验证码' }, pause: 'captcha' },
  ];
  for (const item of cases) {
    const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim({ mode: 'reconcile' }))] });
    const fixture = makeHubController({ hub, deps: { runReconciliationCommand: async () => item.observation } });
    await fixture.controller.dispatchPendingUploadJobs();
    assert.equal(finalBody(hub, 'phase').phase, 'observation_started');
    assert.equal(hub.byAction('receipt').length, 0);
    if (item.pause) {
      assert.equal(finalBody(hub, 'pause').reason, item.pause);
      assert.equal(hub.byAction('reconcile').length, 0);
    } else {
      assert.equal(finalBody(hub, 'reconcile').finding, item.finding);
    }
  }
});

test('hub create reconcile reads the works list and reports unknown or a risk pause', async () => {
  const unknownHub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const unknown = makeHubController({ hub: unknownHub });
  await unknown.controller.dispatchPendingUploadJobs();
  assert.deepEqual(unknown.openedTabs.map((tab) => tab.url), ['https://fanqienovel.com/main/writer/']);
  const run = unknown.calls.find((call) => call[0] === 'reconcile-create');
  assert.deepEqual(run[1], { platform: 'fanqie', book_name: '港区旧案' });
  assert.deepEqual(run[2].slice(-1), ['phase']);
  assert.equal(finalBody(unknownHub, 'phase').phase, 'observation_started');
  assert.equal(finalBody(unknownHub, 'reconcile').finding, 'unknown');
  assert.equal(unknown.calls.filter((call) => call[0] === 'run-create-book').length, 0);

  const riskHub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const risk = makeHubController({
    hub: riskHub,
    deps: {
      runReconcileCreateCommand: async () => ({ finding: 'unknown', outcome: 'risk_pause', riskReason: 'mfa' }),
    },
  });
  await risk.controller.dispatchPendingUploadJobs();
  assert.equal(finalBody(riskHub, 'pause').reason, 'mfa');

  const foundHub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const found = makeHubController({
    hub: foundHub,
    deps: {
      runReconcileCreateCommand: async () => ({
        finding: 'found',
        remote_book_id: '8001',
        remote_url: 'https://fanqienovel.com/main/writer/chapter-manage/8001',
      }),
    },
  });
  await found.controller.dispatchPendingUploadJobs();
  assert.equal(finalBody(foundHub, 'receipt').remote_id, '8001');
  assert.equal(finalBody(foundHub, 'reconcile').finding, 'found');

  const absentHub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const absent = makeHubController({
    hub: absentHub,
    deps: { runReconcileCreateCommand: async () => ({ finding: 'absent', works_list_complete: false }) },
  });
  await absent.controller.dispatchPendingUploadJobs();
  assert.equal(finalBody(absentHub, 'reconcile').finding, 'unknown');
});

test('hub reconcile answered with remote_id_on_record is reported as unknown instead', async () => {
  const hub = makeFakeHub({
    claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))],
    on: {
      reconcile: (body) => (body.finding === 'unknown' ? undefined : hubError(409, 'remote_id_on_record')),
    },
  });
  const fixture = makeHubController({
    hub,
    deps: {
      runReconcileCreateCommand: async () => ({ finding: 'absent', works_list_complete: true }),
    },
  });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(hub.byAction('reconcile').map((request) => request.body.finding), ['absent', 'unknown']);
});

test('hub cover reconcile reports unknown without driving the platform', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubCoverClaim({ mode: 'reconcile' }))] });
  const fixture = makeHubController({ hub });
  await fixture.controller.dispatchPendingUploadJobs();
  assert.equal(finalBody(hub, 'phase').phase, 'observation_started');
  assert.equal(finalBody(hub, 'reconcile').finding, 'unknown');
  assert.equal(fixture.calls.filter((call) => call[0].startsWith('run-') || call[0].startsWith('reconcile-')).length, 0);
});

// --- dispatch ---------------------------------------------------------------------------------

function forwinClaim(index) {
  return {
    execution_mode: 'execute',
    job: {
      job_id: `forwin-job-${index}`,
      idempotency_key: `publisher-job:v1:forwin-job-${index}`,
      task_kind: 'chapter_upload',
      platform: 'qidian',
      content_sha256: 'a'.repeat(64),
      input: { book_name: 'Q', chapter_title: 'C', body: 'B', publish: false },
    },
    attempt: {
      attempt_id: `forwin-attempt-${index}`,
      attempt_number: 1,
      lease_epoch: 1,
      phase: 'claimed',
      lease_expires_at: '2026-11-01T00:02:00Z',
      heartbeat_interval_seconds: 3600,
    },
  };
}

test('each round claims from publish-hub first, then forwin, alternating until both are empty', async () => {
  const sequence = [];
  const hub = makeFakeHub({
    claims: [
      claimReply(hubChapterClaim({ job: { id: 'hub-a' }, attempt_id: 'hub-attempt-a' })),
      claimReply(hubChapterClaim({ job: { id: 'hub-b' }, attempt_id: 'hub-attempt-b' })),
    ],
    on: {
      claim: () => {
        sequence.push('hub-claim');
        return undefined;
      },
    },
  });
  const forwinQueue = [forwinClaim(1)];
  const fixture = makeHubController({
    hub,
    backend: {
      claimNextUploadJob: async (payload) => {
        sequence.push('forwin-claim');
        assert.deepEqual(payload, { client_id: FORWIN_CLIENT_ID, connected_platforms: ['qidian', 'fanqie'] });
        const claim = forwinQueue.shift();
        return claim ? { found: true, claim } : { found: false, claim: null };
      },
    },
  });
  const executed = [];
  fixture.controller.executeUploadClaim = async (claim) => {
    executed.push(claim.job.job_id);
    sequence.push(`forwin-run:${claim.job.job_id}`);
    return { status: 'succeeded' };
  };

  const result = await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(result, { found: true, handled: 3 });
  assert.deepEqual(sequence, [
    'hub-claim',
    'forwin-claim',
    'forwin-run:forwin-job-1',
    'hub-claim',
    'forwin-claim',
    'hub-claim',
  ]);
  assert.deepEqual(executed, ['forwin-job-1']);
  assert.deepEqual(hub.byAction('result').map((request) => request.pathname.split('/')[4]), ['hub-a', 'hub-b']);
});

test('a hub claim answered attempt_in_progress waits for the next round without failing forwin', async () => {
  const hub = makeFakeHub({ claims: [hubError(409, 'attempt_in_progress')] });
  const forwinQueue = [forwinClaim(1)];
  const fixture = makeHubController({
    hub,
    backend: {
      claimNextUploadJob: async () => {
        const claim = forwinQueue.shift();
        return claim ? { found: true, claim } : { found: false, claim: null };
      },
    },
  });
  const executed = [];
  fixture.controller.executeUploadClaim = async (claim) => {
    executed.push(claim.job.job_id);
    return { status: 'succeeded' };
  };

  const result = await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(result, { found: true, handled: 1 });
  assert.deepEqual(executed, ['forwin-job-1']);
  assert.deepEqual(hub.actions(), ['executor-heartbeat', 'claim']);
});

test('the hub only claims platforms the browser is logged into', async () => {
  const hub = makeFakeHub();
  const fixture = makeHubController({ hub });
  fixture.controller._collectConnectedPlatforms = async () => ['fanqie'];
  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(hub.byAction('claim')[0].body.platforms, ['fanqie']);
  assert.deepEqual(hub.byAction('executor-heartbeat')[0].body.platforms, ['qidian', 'fanqie']);
});

test('an unreachable hub does not stop forwin claims and its error is still reported', async () => {
  const hub = makeFakeHub({
    on: {
      'executor-heartbeat': () => {
        throw new TypeError('Failed to fetch');
      },
    },
  });
  const forwinQueue = [forwinClaim(1)];
  const fixture = makeHubController({
    hub,
    backend: {
      claimNextUploadJob: async () => {
        const claim = forwinQueue.shift();
        return claim ? { found: true, claim } : { found: false, claim: null };
      },
    },
  });
  const executed = [];
  fixture.controller.executeUploadClaim = async (claim) => {
    executed.push(claim.job.job_id);
    return { status: 'succeeded' };
  };
  assert.deepEqual(await fixture.controller.dispatchPendingUploadJobs(), { found: true, handled: 1 });
  assert.deepEqual(executed, ['forwin-job-1']);
  assert.deepEqual(hub.actions(), ['executor-heartbeat']);
  assert.deepEqual(fixture.hubRounds, [{ ok: false, error: 'Failed to fetch' }]);
});

test('a forwin failure in a hub round is still raised as before', async () => {
  const hub = makeFakeHub();
  const fixture = makeHubController({
    hub,
    backend: {
      claimNextUploadJob: async () => {
        throw new TypeError('forwin claim failed');
      },
    },
  });
  await assert.rejects(fixture.controller.dispatchPendingUploadJobs(), /forwin claim failed/);
  assert.deepEqual(fixture.hubRounds, [{ ok: true, error: '' }]);
});

test('with publish-hub unset the dispatch makes no hub request and behaves as before', async () => {
  for (const settings of [{ hubBaseUrl: '', hubApiKey: '' }, { hubBaseUrl: HUB_URL, hubApiKey: '' }, { hubBaseUrl: '', hubApiKey: HUB_KEY }]) {
    const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
    const fixture = makeHubController({ hub, settings });
    const result = await fixture.controller.dispatchPendingUploadJobs();
    assert.deepEqual(result, { found: false });
    assert.equal(hub.requests.length, 0);
    assert.deepEqual(fixture.forwinCalls.map((call) => call[0]), ['claim']);
  }
  const hub = makeFakeHub();
  const fixture = makeHubController({
    hub,
    settings: { hubBaseUrl: '', hubApiKey: '', backendBaseUrl: '', apiKey: '' },
  });
  assert.deepEqual(await fixture.controller.dispatchPendingUploadJobs(), { skipped: true });
  assert.equal(hub.requests.length, 0);
});

test('hub settings never reach forwin requests', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const fixture = makeHubController({ hub });
  await fixture.controller.sendHeartbeat();
  await fixture.controller.dispatchPendingUploadJobs();
  const serialized = JSON.stringify(fixture.forwinCalls);
  assert.doesNotMatch(serialized, new RegExp(HUB_KEY));
  assert.doesNotMatch(serialized, /host\.docker\.internal/);
  for (const request of hub.requests) {
    assert.doesNotMatch(JSON.stringify(request), new RegExp(FORWIN_KEY));
  }
});

test('heartbeatAndDispatchUploadJobs still runs publish-hub jobs when the forwin heartbeat fails', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const fixture = makeHubController({
    hub,
    backend: {
      heartbeat: async () => {
        throw new TypeError('forwin down');
      },
    },
  });
  await assert.rejects(fixture.controller.heartbeatAndDispatchUploadJobs(), /forwin down/);
  assert.equal(finalBody(hub, 'result').outcome, 'succeeded');

  const legacyHub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  let claims = 0;
  const legacy = makeHubController({
    hub: legacyHub,
    settings: { hubBaseUrl: '', hubApiKey: '' },
    backend: {
      heartbeat: async () => {
        throw new TypeError('forwin down');
      },
      claimNextUploadJob: async () => {
        claims += 1;
        return { found: false, claim: null };
      },
    },
  });
  await assert.rejects(legacy.controller.heartbeatAndDispatchUploadJobs(), /forwin down/);
  assert.equal(claims, 0);
  assert.equal(legacyHub.requests.length, 0);
});

// --- journal replay ---------------------------------------------------------------------------

function legacySnapshotWithResult() {
  const claim = forwinClaim(7);
  return {
    version: 1,
    records: [
      {
        client_id: FORWIN_CLIENT_ID,
        execution_mode: 'execute',
        job: claim.job,
        attempt: claim.attempt,
        local_phase: 'ack_pending',
        receipt: null,
        result: {
          outcome: 'failed',
          message: '',
          current_url: '',
          error_code: 'extension-worker-restarted',
          error_message: 'restart',
          details: {},
        },
        pause: null,
        created_at: '2026-10-31T00:00:00Z',
        updated_at: '2026-10-31T00:00:00Z',
        acked_at: null,
      },
    ],
  };
}

async function seedHubEntry(journalStore, claimOverrides = {}, { result, receipt, pause, mutationStarted } = {}) {
  const journal = journalStore.createJournal();
  const claim = await translateHubClaim(hubChapterClaim(claimOverrides));
  await journal.recordClaim({ clientId: HUB_CLIENT_ID, claim, backend: 'hub' });
  const attemptId = claim.attempt.attempt_id;
  if (mutationStarted || receipt) {
    await journal.markMutationStarted(attemptId, 'hub');
  }
  if (receipt) {
    await journal.saveReceipt(attemptId, receipt, 'hub');
  }
  if (result) {
    await journal.saveResult(attemptId, result, 'hub');
  }
  if (pause) {
    await journal.savePause(attemptId, pause, 'hub');
  }
  return claim;
}

test('journal entries without a backend are replayed to forwin only', async () => {
  const journalStore = memoryJournalStore(legacySnapshotWithResult());
  const hub = makeFakeHub();
  const fixture = makeHubController({ hub, journalStore });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(fixture.forwinCalls.map((call) => call[0]), ['result', 'claim']);
  assert.equal(fixture.forwinCalls[0][1], 'forwin-job-7');
  assert.deepEqual(hub.actions(), ['executor-heartbeat', 'claim']);
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('hub journal entries are replayed to publish-hub only, unchanged', async () => {
  const journalStore = memoryJournalStore();
  const resultBody = { client_id: HUB_CLIENT_ID, epoch: 3, outcome: 'retryable_error', error: { code: 'x' } };
  await seedHubEntry(journalStore, {}, {
    mutationStarted: true,
    result: { report: 'result', outcome: 'retryable_error', body: resultBody },
  });
  const hub = makeFakeHub();
  const fixture = makeHubController({ hub, journalStore, settings: { backendBaseUrl: FORWIN_URL } });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.deepEqual(hub.actions(), ['executor-heartbeat', 'result', 'claim']);
  assert.deepEqual(hub.byAction('result')[0].body, resultBody);
  assert.equal(fixture.forwinCalls.filter((call) => call[0] === 'result').length, 0);
  assert.equal(hubRecords(journalStore)[0].local_phase, 'acked');
});

for (const fence of [...FENCE_CODES.map((code) => hubError(409, code)), hubError(404, 'not_found')]) {
  test(`a hub journal entry fenced with ${fence.body.error.code} on replay is deleted`, async () => {
    const journalStore = memoryJournalStore(legacySnapshotWithResult());
    await seedHubEntry(journalStore, {}, {
      mutationStarted: true,
      result: { report: 'result', outcome: 'retryable_error', body: { client_id: HUB_CLIENT_ID, epoch: 3, outcome: 'retryable_error' } },
    });
    await seedHubEntry(journalStore, { attempt_id: 'hub-attempt-2', job: { id: 'hub-job-2' } }, {
      pause: { body: { client_id: HUB_CLIENT_ID, epoch: 3, reason: 'needs_human', detail: 'x' } },
    });
    const hub = makeFakeHub({ on: { result: () => fence, pause: () => fence } });
    const fixture = makeHubController({ hub, journalStore });

    await fixture.controller.dispatchPendingUploadJobs();

    assert.deepEqual(hub.actions(), ['executor-heartbeat', 'result', 'pause', 'claim']);
    assert.equal(hubRecords(journalStore).length, 0);
    assert.equal(journalStore.snapshot.records.length, 1);
    assert.equal(journalStore.snapshot.records[0].backend, 'forwin');
  });
}

test('the forwin replay never sends a hub journal entry to forwin', async () => {
  const resultBody = { client_id: HUB_CLIENT_ID, epoch: 3, outcome: 'retryable_error' };
  for (const [settings, hubConfigured] of [
    [{ hubBaseUrl: '', hubApiKey: '' }, false],
    [{}, true],
  ]) {
    const journalStore = memoryJournalStore(legacySnapshotWithResult());
    await seedHubEntry(journalStore, {}, {
      mutationStarted: true,
      result: { report: 'result', outcome: 'retryable_error', body: resultBody },
    });
    const hub = makeFakeHub({
      on: { result: () => ({ status: 503, body: { error: { code: 'unavailable', message: 'down' } } }) },
    });
    const fixture = makeHubController({ hub, journalStore, settings });

    assert.deepEqual(await fixture.controller.dispatchPendingUploadJobs(), { found: false });
    if (hubConfigured) {
      assert.deepEqual(fixture.hubRounds, [{ ok: false, error: 'down' }]);
    } else {
      assert.equal(hub.requests.length, 0);
      assert.deepEqual(fixture.hubRounds, []);
    }

    assert.deepEqual(fixture.forwinCalls.map((call) => call[0]), ['result', 'claim']);
    assert.equal(fixture.forwinCalls[0][1], 'forwin-job-7');
    const [hubEntry] = hubRecords(journalStore);
    assert.equal(hubEntry.local_phase, 'ack_pending');
  }
});

test('a hub journal entry kept by a transient error is resent in the next round', async () => {
  const journalStore = memoryJournalStore();
  const body = { client_id: HUB_CLIENT_ID, epoch: 3, outcome: 'defer', defer_until: '2026-11-01T00:05:00.000Z' };
  await seedHubEntry(journalStore, {}, { result: { report: 'result', outcome: 'defer', body } });
  let fail = true;
  const hub = makeFakeHub({
    on: {
      result: () => (fail ? { status: 503, body: { error: { code: 'unavailable', message: 'down' } } } : undefined),
    },
  });
  const fixture = makeHubController({ hub, journalStore, settings: { backendBaseUrl: '', apiKey: '' } });

  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(fixture.hubRounds, [{ ok: false, error: 'down' }]);
  assert.equal(hubRecords(journalStore)[0].local_phase, 'ack_pending');
  fail = false;
  await fixture.controller.dispatchPendingUploadJobs();
  assert.deepEqual(hub.byAction('result').map((request) => request.body), [body, body]);
  assert.equal(hubRecords(journalStore)[0].local_phase, 'acked');
});

test('hub replay finishes an interrupted attempt from what the journal holds', async () => {
  const journalStore = memoryJournalStore();
  await seedHubEntry(journalStore, { attempt_id: 'started' }, { mutationStarted: true });
  await seedHubEntry(journalStore, { attempt_id: 'claimed-only' });
  await seedHubEntry(journalStore, { attempt_id: 'receipt-only' }, {
    receipt: { body: { client_id: HUB_CLIENT_ID, epoch: 3, official_state: 'drafted', remote_id: '9001', raw: {} } },
  });
  const hub = makeFakeHub();
  const fixture = makeHubController({ hub, journalStore, settings: { backendBaseUrl: '', apiKey: '' } });

  await fixture.controller.dispatchPendingUploadJobs();

  const reported = hub.requests
    .filter((request) => ['result', 'receipt', 'pause', 'reconcile'].includes(request.action))
    .map((request) => [request.pathname.split('/')[6], request.action, request.body.outcome || '']);
  assert.deepEqual(reported, [
    ['started', 'result', 'retryable_error'],
    ['receipt-only', 'receipt', ''],
    ['receipt-only', 'result', 'succeeded'],
  ]);
  const records = hubRecords(journalStore);
  assert.deepEqual(records.map((record) => record.attempt.attempt_id).sort(), ['receipt-only', 'started']);
  assert.ok(records.every((record) => record.local_phase === 'acked'));
});

// --- wiring -----------------------------------------------------------------------------------

test('manifest allows the publish-hub tunnel and the version is bumped everywhere', async () => {
  const [manifestText, backgroundSource, controllerSource, hubSource, packageText] = await Promise.all([
    readFile(new URL('../manifest.json', import.meta.url), 'utf8'),
    readFile(new URL('../background.js', import.meta.url), 'utf8'),
    readFile(new URL('../lib/controller.js', import.meta.url), 'utf8'),
    readFile(new URL('../lib/hub-adapter.js', import.meta.url), 'utf8'),
    readFile(new URL('../package.json', import.meta.url), 'utf8'),
  ]);
  const manifest = JSON.parse(manifestText);
  assert.ok(manifest.host_permissions.includes('http://publish-hub-tunnel:18930/*'));
  assert.equal(manifest.version, '0.1.66');
  for (const path of ['controller', 'platforms', 'risk-inspection', 'upload-journal']) {
    assert.match(backgroundSource, new RegExp(`\\./lib/${path}\\.js\\?v=0\\.1\\.66'`));
  }
  for (const path of ['platforms', 'reconciliation', 'risk-inspection', 'hub-adapter']) {
    assert.match(controllerSource, new RegExp(`\\./${path}\\.js\\?v=0\\.1\\.66'`));
  }
  assert.doesNotMatch(backgroundSource + controllerSource + hubSource, /\?v=0\.1\.65/);
  assert.match(JSON.parse(packageText).scripts.test, /node --check lib\/hub-adapter\.js/);
});

test('background wires the hub client, hub client id, create commands and the heartbeat round', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');
  assert.match(source, /createHubClient/);
  assert.match(source, /hubBackend:/);
  assert.match(source, /getHubClientId:/);
  assert.match(source, /getStorageValue\(HUB_CLIENT_ID_STORAGE_KEY/);
  assert.match(source, /async\s+function\s+runCreateBookCommand\s*\(/);
  assert.match(source, /'run-create-book'/);
  assert.match(source, /async\s+function\s+runReconcileCreateCommand\s*\(/);
  assert.match(source, /'reconcile-create'/);
  assert.match(source, /runCreateBookCommand,/);
  assert.match(source, /runReconcileCreateCommand,/);
  assert.match(source, /waitForRunnableTab:/);
  assert.match(source, /controller\.heartbeatRound\(\)/);
});

// --- fix round 1 ------------------------------------------------------------------------------

// What publish-hub accepts as text (app/payloads.py is_storable_text/is_storable_json).
function isStorable(value) {
  if (typeof value === 'string') {
    return !value.includes('\u0000') && value.isWellFormed();
  }
  if (typeof value === 'number') {
    return Number.isFinite(value);
  }
  if (Array.isArray(value)) {
    return value.every(isStorable);
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).every(([key, item]) => isStorable(key) && isStorable(item));
  }
  return true;
}

function codePoints(value) {
  return Array.from(String(value)).length;
}

for (const [name, on] of [
  ['an unreachable hub', { 'executor-heartbeat': () => { throw new TypeError('Failed to fetch'); } }],
  ['a hub that refuses the key (401)', { claim: () => hubError(401, 'unauthorized', 'missing or invalid API key') }],
]) {
  test(`${name} does not stop forwin comment sync or the heartbeat round`, async () => {
    const hub = makeFakeHub({ on });
    const fixture = makeHubController({ hub });

    await fixture.controller.heartbeatRound();

    const order = fixture.forwinCalls.map((call) => call[0]);
    assert.ok(order.includes('heartbeat'), order.join(','));
    assert.ok(order.includes('comment-claim'), order.join(','));
    assert.equal(fixture.hubRounds.length, 1);
    assert.equal(fixture.hubRounds[0].ok, false);
    assert.doesNotMatch(JSON.stringify(fixture.hubRounds), new RegExp(HUB_KEY));
  });
}

test('settings-updated still runs forwin comment sync when publish-hub is down', async () => {
  const hub = makeFakeHub({ on: { 'executor-heartbeat': () => { throw new TypeError('Failed to fetch'); } } });
  const fixture = makeHubController({ hub, deps: { refreshContentBridge: async () => {} } });

  await fixture.controller.handleMessage({ action: 'settings-updated', payload: {} });

  assert.ok(fixture.forwinCalls.some((call) => call[0] === 'comment-claim'));
  assert.deepEqual(fixture.hubRounds, [{ ok: false, error: 'Failed to fetch' }]);
});

test('a successful hub round is recorded as such', async () => {
  const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
  const fixture = makeHubController({ hub });
  await fixture.controller.heartbeatRound();
  assert.deepEqual(fixture.hubRounds, [{ ok: true, error: '' }]);
});

test('when the forwin heartbeat fails, hub and dispatch errors are surfaced, not swallowed', async () => {
  const hub = makeFakeHub({ on: { claim: () => hubError(401, 'unauthorized', 'missing or invalid API key') } });
  const fixture = makeHubController({
    hub,
    backend: {
      heartbeat: async () => {
        throw new TypeError('forwin down');
      },
      claimNextUploadJob: async () => {
        throw new TypeError('forwin claim down');
      },
    },
  });

  await assert.rejects(fixture.controller.heartbeatAndDispatchUploadJobs(), /forwin down/);

  assert.deepEqual(fixture.hubRounds, [{ ok: false, error: 'missing or invalid API key' }]);
  assert.equal(fixture.reportedErrors.length, 1);
  assert.match(fixture.reportedErrors[0].message, /forwin claim down/);
  assert.doesNotMatch(JSON.stringify(fixture.reportedErrors), new RegExp(HUB_KEY));
});

test('storableText and storableJson remove what publish-hub refuses and cut on code points', () => {
  assert.equal(storableText('a\u0000b'), 'a�b');
  assert.equal(storableText('x\uD800y\uDC00z'), 'x�y�z');
  assert.equal(storableText('ok 😀'), 'ok 😀');
  const emoji = '😀'.repeat(10);
  const cut = storableText(emoji, 5);
  assert.ok(cut.isWellFormed());
  assert.equal(codePoints(cut), 5);
  const json = storableJson({
    'k\uDC00': ['v\u0000', 1, Number.NaN, Number.POSITIVE_INFINITY, true, null, { deep: '\uD800' }],
    fine: 'text',
    skipped: undefined,
  });
  assert.ok(isStorable(json));
  assert.deepEqual(json, {
    'k�': ['v�', 1, null, null, true, null, { deep: '�' }],
    fine: 'text',
  });
});

test('hub reports built from page text with NUL and lone surrogates stay storable', async () => {
  const dirty = 'bad\u0000text\uD800tail\uDFFF';
  const longMessage = `${'😀'.repeat(2500)}\uD83D`;
  const cases = [
    {
      name: 'failure result',
      result: {
        ok: false,
        errorCode: 'platform-\u0000rejected',
        error: longMessage,
        currentUrl: 'https://fanqienovel.com/\uD800',
        resultPayload: { platform_message: dirty, ['key\uDC00']: [dirty, { nested: dirty }], score: Number.NaN },
      },
      check: (hub) => {
        const body = finalBody(hub, 'result');
        assert.ok(isStorable(body), JSON.stringify(body).slice(0, 200));
        assert.ok(codePoints(body.error.message) <= 2000);
        assert.equal(body.error.details.score, null);
      },
    },
    {
      name: 'risk pause',
      result: { ok: false, riskPause: true, riskReason: 'captcha', riskEvidence: { message: `${longMessage}${dirty}` } },
      check: (hub) => {
        const body = finalBody(hub, 'pause');
        assert.equal(body.reason, 'captcha');
        assert.ok(isStorable(body));
        assert.ok(codePoints(body.detail) <= 2000);
      },
    },
    {
      name: 'receipt',
      result: { ...okChapterResult(), message: dirty, resultPayload: { ...okChapterResult().resultPayload, platform_message: dirty } },
      check: (hub) => {
        const receipt = finalBody(hub, 'receipt');
        assert.ok(isStorable(receipt));
        assert.equal(receipt.raw.evidence.confirmation_text, 'bad�text�tail�');
        assert.equal(finalBody(hub, 'result').outcome, 'succeeded');
      },
    },
  ];
  for (const item of cases) {
    const hub = makeFakeHub({ claims: [claimReply(hubChapterClaim())] });
    const fixture = makeHubController({ hub, deps: { runUploadCommand: async () => item.result } });
    await fixture.controller.dispatchPendingUploadJobs();
    item.check(hub);
  }
});

test('hub create reconcile with candidates pauses for a human and puts the candidates in the detail', async () => {
  const candidates = Array.from({ length: 40 }, (_value, index) => ({
    text: `港区旧案 第${index}卷 ${'很长的作品卡片文字'.repeat(30)}\u0000`,
    remote_book_id: String(8000 + index),
    remote_url: `https://fanqienovel.com/main/writer/chapter-manage/${8000 + index}`,
    exact_title: null,
  }));
  const hub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const fixture = makeHubController({
    hub,
    deps: {
      runReconcileCreateCommand: async () => ({
        finding: 'unknown',
        candidates,
        works_list_complete: false,
        reason: 'exact-title-matching-and-list-completeness-unavailable',
      }),
    },
  });

  await fixture.controller.dispatchPendingUploadJobs();

  assert.equal(finalBody(hub, 'phase').phase, 'observation_started');
  assert.equal(hub.byAction('reconcile').length, 0);
  const pause = finalBody(hub, 'pause');
  assert.equal(pause.reason, 'needs_human');
  assert.ok(isStorable(pause));
  assert.ok(codePoints(pause.detail) <= 2000);
  assert.match(pause.detail, /港区旧案/);
  assert.match(pause.detail, /8000/);
  assert.match(pause.detail, /https:\/\/fanqienovel\.com\/main\/writer\/chapter-manage\/8000/);
  assert.match(pause.detail, /8001/);

  const riskHub = makeFakeHub({ claims: [claimReply(hubCreateClaim({ mode: 'reconcile' }))] });
  const risk = makeHubController({
    hub: riskHub,
    deps: {
      runReconcileCreateCommand: async () => ({ finding: 'unknown', outcome: 'risk_pause', riskReason: 'captcha', candidates }),
    },
  });
  await risk.controller.dispatchPendingUploadJobs();
  assert.equal(finalBody(riskHub, 'pause').reason, 'captcha');
});

test('the hub client id is available before any job, stable across reloads and the one claims use', async () => {
  let stored = '';
  const storage = { read: async () => stored, write: async (value) => { stored = value; } };
  const firstStore = createHubClientIdStore(storage);
  const first = makeHubController({ deps: { getHubClientId: () => firstStore.ensure() } });
  const shown = await first.controller.handleMessage({ action: 'hub-client-id', payload: {} });
  assert.match(shown.hubClientId, /^magi3-ext-[a-z0-9]{8}$/);

  const afterReloadStore = createHubClientIdStore(storage);
  const hub = makeFakeHub();
  const reloaded = makeHubController({ hub, deps: { getHubClientId: () => afterReloadStore.ensure() } });
  assert.deepEqual(await reloaded.controller.handleMessage({ action: 'hub-client-id', payload: {} }), shown);
  await reloaded.controller.dispatchPendingUploadJobs();
  assert.equal(hub.byAction('executor-heartbeat')[0].body.client_id, shown.hubClientId);
  assert.equal(hub.byAction('claim')[0].body.client_id, shown.hubClientId);
});

test('options page and background share the hub client id key and the options page asks for it on open and save', async () => {
  const [optionsSource, backgroundSource] = await Promise.all([
    readFile(new URL('../options.js', import.meta.url), 'utf8'),
    readFile(new URL('../background.js', import.meta.url), 'utf8'),
  ]);
  assert.equal(HUB_CLIENT_ID_STORAGE_KEY, 'forwinPublisherHubClientId');
  for (const source of [optionsSource, backgroundSource]) {
    assert.match(source, /HUB_CLIENT_ID_STORAGE_KEY/);
    assert.match(source, /createHubClientIdStore/);
    assert.doesNotMatch(source, /'forwinPublisherHubClientId'/);
  }
  assert.match(optionsSource, /action:\s*'hub-client-id'/);
  assert.match(optionsSource, /async function boot\(\)[\s\S]*renderHubClientId\(\)/);
  assert.match(optionsSource, /settings-updated[\s\S]*renderHubClientId\(\)/);
});

test('background alarm bookkeeping follows the whole heartbeat round and hub rounds are recorded', async () => {
  const source = await readFile(new URL('../background.js', import.meta.url), 'utf8');
  assert.match(source, /controller\.heartbeatRound\(\)\s*\.then\(\(\)\s*=>\s*updateBackgroundStatus\(\{\s*lastHeartbeatSuccessAt/);
  assert.match(source, /recordHubRound,/);
  assert.match(source, /reportError:\s*reportBackgroundError/);
  assert.match(source, /lastHubError/);
});
