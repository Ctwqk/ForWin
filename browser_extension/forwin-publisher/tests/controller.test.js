import test from 'node:test';
import assert from 'node:assert/strict';

import { PublisherExtensionController } from '../lib/controller.js';
import { createUploadJournal } from '../lib/upload-journal.js';

const CONTENT_SHA256 = 'a'.repeat(64);
const NORMALIZED_CONTENT_SHA256 = 'b'.repeat(64);

function executionContentEvidence() {
  return {
    observed_content_sha256: CONTENT_SHA256,
    expected_normalized_sha256: NORMALIZED_CONTENT_SHA256,
    observed_normalized_sha256: NORMALIZED_CONTENT_SHA256,
    content_match_basis: 'normalized-editor-text-sha256',
  };
}

function makeUploadClaim(overrides = {}) {
  const jobOverrides = overrides.job || {};
  const replacesInput = Boolean(
    jobOverrides.task_kind && jobOverrides.task_kind !== 'chapter_upload',
  );
  const claim = {
    execution_mode: 'execute',
    job: {
      job_id: 'job-1',
      idempotency_key: 'publisher-job:v1:job-1',
      task_kind: 'chapter_upload',
      platform: 'qidian',
      content_sha256: CONTENT_SHA256,
      input: {
        book_name: 'Test Book',
        chapter_title: 'Chapter 1',
        body: 'Body',
        publish: false,
        upload_url: 'https://write.qq.com/portal/dashboard',
      },
    },
    attempt: {
      attempt_id: 'attempt-1',
      attempt_number: 1,
      lease_epoch: 7,
      phase: 'claimed',
      lease_expires_at: '2026-07-21T12:01:30Z',
      heartbeat_interval_seconds: 3600,
    },
  };
  return {
    ...claim,
    ...overrides,
    job: {
      ...claim.job,
      ...jobOverrides,
      input: replacesInput
        ? { ...(jobOverrides.input || {}) }
        : { ...claim.job.input, ...(jobOverrides.input || {}) },
    },
    attempt: { ...claim.attempt, ...(overrides.attempt || {}) },
  };
}

function makeMemoryUploadJournalStore(initialSnapshot) {
  let snapshot = initialSnapshot == null ? initialSnapshot : structuredClone(initialSnapshot);
  const writes = [];
  const read = async () => (snapshot == null ? snapshot : structuredClone(snapshot));
  const write = async (next) => {
    snapshot = structuredClone(next);
    writes.push(structuredClone(next));
  };
  return {
    createJournal: () => createUploadJournal({ read, write }),
    get snapshot() {
      return snapshot == null ? snapshot : structuredClone(snapshot);
    },
    writes,
  };
}

function makeController(overrides = {}, journalStore = makeMemoryUploadJournalStore()) {
  const events = [];
  const loginQrNotifications = [];
  const loginQrStatusEvents = [];
  const closedTabs = [];
  const restoredCookies = [];
  const backendOverrides = overrides.backend || {};
  const defaultBackend = {
    heartbeat: async () => ({ ok: true }),
    syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
    getBrowserSession: async () => null,
    claimNextUploadJob: async () => ({ found: false, claim: null }),
    heartbeatUploadAttempt: async () => ({
      attempt_status: 'running',
      job_status: 'running',
      abort_requested: false,
      next_action: 'execute',
    }),
    updateUploadAttemptPhase: async () => ({
      attempt_status: 'running',
      job_status: 'running',
      abort_requested: false,
      next_action: 'heartbeat',
    }),
    submitUploadAttemptResult: async () => ({
      attempt_status: 'succeeded',
      job_status: 'succeeded',
    }),
    submitUploadReceipt: async () => ({
      attempt_status: 'running',
      job_status: 'running',
    }),
    reconcileUploadAttempt: async () => ({
      attempt_status: 'succeeded',
      job_status: 'succeeded',
    }),
    pauseUploadAttempt: async () => ({
      attempt_status: 'paused',
      job_status: 'paused',
      next_action: 'operator_review',
    }),
    claimNextCommentSyncJob: async () => ({ found: false, job: null }),
    syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
    notifyLoginQr: async (payload) => {
      loginQrNotifications.push(payload);
      return { ok: true, dispatched: true };
    },
    updateCommentSyncJobResult: async () => ({ ok: true }),
  };
  const deps = {
    ensureClientId: async () => 'client-1',
    getClientId: async () => 'client-1',
    ensureHeartbeatAlarm: async () => {},
    getSettings: async () => ({
      backendBaseUrl: 'http://192.168.31.10:8899',
      apiKey: 'secret',
      syncSessionToBackend: true,
      loginQrNotificationsEnabled: true,
      loginQrNotificationsAllowed: true,
      loginQrNotificationsAllowedUntilMs: Number.MAX_SAFE_INTEGER,
    }),
    getExtensionVersion: () => '0.1.3',
    getBrowserInfo: async () => ({ browserName: 'Chrome', browserVersion: '123.0' }),
    openOptionsPage: async () => {},
    refreshContentBridge: async () => {},
    openLoginPopup: async () => ({ windowId: 7, tabId: 42 }),
    openUploadTab: async () => ({ tabId: 77 }),
    getTab: async () => ({ id: 42, url: 'https://write.qq.com/portal/dashboard' }),
    getCookies: async () => [{ name: 'AppAuthToken' }, { name: 'pubtoken' }, { name: 'ywtab' }],
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/dashboard',
      platform: 'qidian',
      authenticated: true,
      loginVisible: false,
      summary: '作品管理 章节管理 写新章',
    }),
    navigateTab: async () => {},
    captureLoginQrImage: async () => null,
    closePopup: async () => {},
    closeTab: async (tabId) => {
      closedTabs.push(tabId);
    },
    notifyPage: async (tabId, eventName, payload) => {
      events.push({ tabId, eventName, payload });
    },
    getPlatformState: async () => ({}),
    setPlatformState: async () => {},
    setCookies: async (platformId, cookies) => {
      restoredCookies.push({ platformId, cookies });
      return { applied: cookies.length };
    },
    runUploadCommand: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/dashboard',
      message: '章节发布动作已提交。',
      resultPayload: { mode: 'publish' },
    }),
    runCoverUploadCommand: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/book/123',
      message: '封面上传动作已提交。',
      resultPayload: { cover_state: 'uploaded' },
    }),
    runAuditSyncCommand: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/book/123',
      message: '审核状态已同步。',
      resultPayload: { work: { audit_state: 'under_review' }, chapters: [] },
    }),
    runReconciliationCommand: async () => ({
      outcome: 'indeterminate',
      reason: 'No conclusive remote evidence.',
    }),
    inspectPlatformRiskCommand: async () => ({ detected: false }),
    runCommentSyncCommand: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/',
      message: '评论同步已完成。',
      comments: [],
      resultPayload: { source: 'fanqie-author-api' },
    }),
    uploadJournal: journalStore.createJournal(),
    recordLoginQrNotification: async (event) => {
      loginQrStatusEvents.push(event);
    },
    ...overrides,
    backend: { ...defaultBackend, ...backendOverrides },
  };
  return {
    controller: new PublisherExtensionController(deps),
    events,
    journalStore,
    loginQrNotifications,
    loginQrStatusEvents,
    closedTabs,
    restoredCookies,
  };
}

test('controller opens login popup and closes it after successful tab update', async () => {
  const { controller, events } = makeController();

  const response = await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );
  assert.match(response.message, /登录弹窗已打开/);

  await controller.handleTabUpdated(42, { url: 'https://write.qq.com/portal/dashboard' }, { id: 42 });

  assert.equal(events.length, 2);
  assert.equal(events.at(-1).eventName, 'login-status');
  assert.equal(events.at(-1).payload.platform, 'qidian');
  assert.equal(events.at(-1).payload.connected, true);
});

test('controller dispatches upload and comment jobs after successful login confirmation', async () => {
  const { controller } = makeController();
  let uploadDispatches = 0;
  let commentDispatches = 0;
  controller.dispatchPendingUploadJobs = async () => {
    uploadDispatches += 1;
    return { found: false };
  };
  controller.dispatchPendingCommentSyncJobs = async () => {
    commentDispatches += 1;
    return { found: false };
  };

  await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );
  await controller.handleTabUpdated(42, { url: 'https://write.qq.com/portal/dashboard' }, { id: 42 });

  assert.equal(uploadDispatches, 1);
  assert.equal(commentDispatches, 1);
});

test('controller does not let popup close event reset a confirmed login', async () => {
  const platformStateWrites = [];
  const heartbeatPayloads = [];
  let popupClosed = false;
  let controllerRef;
  const fixture = makeController({
    getCookies: async () => [],
    inspectPlatformState: async (platformId) => (
      !popupClosed && platformId === 'qidian'
        ? {
          ok: true,
          currentUrl: 'https://write.qq.com/portal/dashboard',
          platform: 'qidian',
          authenticated: true,
          loginVisible: false,
        }
        : null
    ),
    setPlatformState: async (platformId, state) => {
      platformStateWrites.push({ platformId, state });
    },
    closePopup: async () => {
      popupClosed = true;
      await controllerRef.handleTabRemoved(42);
    },
    backend: {
      heartbeat: async (payload) => {
        heartbeatPayloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      getBrowserSession: async () => null,
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });
  controllerRef = fixture.controller;

  await fixture.controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );
  await fixture.controller.handleTabUpdated(42, { url: 'https://write.qq.com/portal/dashboard' }, { id: 42 });

  assert.deepEqual(
    platformStateWrites.map((item) => item.state.connected),
    [false, true],
  );
  assert.equal(fixture.events.at(-1).payload.connected, true);
  const qidianHeartbeats = heartbeatPayloads
    .flatMap((payload) => payload.platforms)
    .filter((item) => item.platform === 'qidian');
  assert.equal(qidianHeartbeats.at(-1).connected, true);
});

test('controller still opens login popup when heartbeat sync fails', async () => {
  const { controller, events } = makeController({
    backend: {
      heartbeat: async () => {
        throw new TypeError('Failed to fetch');
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      getBrowserSession: async () => null,
    },
  });

  const response = await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );

  assert.match(response.message, /登录弹窗已打开/);
  assert.match(response.message, /状态同步失败/);
  assert.equal(events.length, 1);
  assert.match(events[0].payload.message, /状态同步稍后重试/);
});

test('controller sends login QR notification once when scan login is visible', async () => {
  let inspectCount = 0;
  const { controller, events, loginQrNotifications } = makeController({
    inspectLoginState: async () => {
      inspectCount += 1;
      return {
        ok: true,
        currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=secret',
        platform: 'fanqie',
        authenticated: false,
        loginVisible: true,
      };
    },
    captureLoginQrImage: async (tabId, platformId) => ({
      ok: true,
      imageDataUrl: 'data:image/png;base64,cXI=',
      source: `${platformId}:${tabId}`,
    }),
  });

  await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'fanqie' } },
    { tab: { id: 99 } },
  );
  await controller.handleTabUpdated(42, { url: 'https://fanqienovel.com/main/writer/login' }, { id: 42 });
  await controller.handleTabUpdated(42, { url: 'https://fanqienovel.com/main/writer/login' }, { id: 42 });

  assert.equal(inspectCount, 2);
  assert.equal(loginQrNotifications.length, 1);
  assert.equal(loginQrNotifications[0].client_id, 'client-1');
  assert.equal(loginQrNotifications[0].platform, 'fanqie');
  assert.equal(loginQrNotifications[0].current_url, 'https://fanqienovel.com/main/writer/login?ticket=secret');
  assert.equal(loginQrNotifications[0].image_data_url, 'data:image/png;base64,cXI=');
  assert.equal(loginQrNotifications[0].source, 'fanqie:42');
  assert.equal(events.at(-1).payload.connected, false);
});

test('controller does not capture or send login QR notifications unless enabled', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    getSettings: async () => ({
      backendBaseUrl: 'http://192.168.31.10:8899',
      apiKey: 'secret',
      syncSessionToBackend: true,
    }),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  const result = await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(result.reason, 'login-qr-notifications-disabled-by-settings');
  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.equal(loginQrStatusEvents.length, 1);
  assert.equal(loginQrStatusEvents[0].phase, 'skipped');
  assert.equal(loginQrStatusEvents[0].reason, 'login-qr-notifications-disabled-by-settings');
});

test('controller does not send login QR notifications without hidden operator allowance', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    getSettings: async () => ({
      backendBaseUrl: 'http://192.168.31.10:8899',
      apiKey: 'secret',
      syncSessionToBackend: true,
      loginQrNotificationsEnabled: true,
    }),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  const result = await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(result.reason, 'login-qr-notifications-disabled-by-settings');
  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.equal(loginQrStatusEvents.length, 1);
  assert.equal(loginQrStatusEvents[0].reason, 'login-qr-notifications-disabled-by-settings');
});

test('controller does not send login QR notifications with stale hidden operator allowance', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    getSettings: async () => ({
      backendBaseUrl: 'http://192.168.31.10:8899',
      apiKey: 'secret',
      syncSessionToBackend: true,
      loginQrNotificationsEnabled: true,
      loginQrNotificationsAllowed: true,
    }),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  const result = await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(result.reason, 'login-qr-notifications-disabled-by-settings');
  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.equal(loginQrStatusEvents.length, 1);
  assert.equal(loginQrStatusEvents[0].reason, 'login-qr-notifications-disabled-by-settings');
});

test('controller suppresses concurrent login QR sends for the same session', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications } = makeController({
    captureLoginQrImage: async () => {
      captureCalls += 1;
      await new Promise((resolve) => { setTimeout(resolve, 20); });
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  await Promise.all([
    controller.maybeNotifyLoginQr(session, inspection),
    controller.maybeNotifyLoginQr(session, inspection),
  ]);

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller waits for the login QR throttle window before sending another code for the same page', async () => {
  let captureCalls = 0;
  let nowMs = 1_000_000;
  const { controller, loginQrNotifications } = makeController({
    nowMs: () => nowMs,
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,cXI${captureCalls}=`,
        source: `image-${captureCalls}`,
      };
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  await controller.maybeNotifyLoginQr(session, inspection);
  await controller.maybeNotifyLoginQr(session, inspection);
  nowMs += 60_001;
  await controller.maybeNotifyLoginQr(session, inspection);
  nowMs += 60_000;
  await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(captureCalls, 2);
  assert.equal(loginQrNotifications.length, 2);
  assert.equal(loginQrNotifications[0].image_data_url, 'data:image/png;base64,cXI1=');
  assert.equal(loginQrNotifications[1].image_data_url, 'data:image/png;base64,cXI2=');
});

test('controller stops repeating login QR notifications when backend reports notifications disabled', async () => {
  let captureCalls = 0;
  let nowMs = 1_000_000;
  const loginQrNotifications = [];
  const { controller } = makeController({
    nowMs: () => nowMs,
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,disabled${captureCalls}=`,
        source: `disabled-${captureCalls}`,
      };
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return {
          ok: true,
          dispatched: false,
          disabled: true,
          message: 'Discord login QR webhook is not configured.',
        };
      },
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  const inspection = {
    currentUrl: 'https://fanqienovel.com/main/writer/login',
    authenticated: false,
    loginVisible: true,
  };

  await controller.maybeNotifyLoginQr(session, inspection);
  nowMs += 121_000;
  await controller.maybeNotifyLoginQr(session, inspection);

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller keeps login QR notifications disabled across refreshed login URLs', async () => {
  let captureCalls = 0;
  const loginQrNotifications = [];
  const { controller } = makeController({
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,refreshed${captureCalls}=`,
        source: `refreshed-${captureCalls}`,
      };
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return {
          ok: true,
          dispatched: false,
          disabled: true,
          message: 'Discord login QR webhook is not configured.',
        };
      },
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };

  await controller.maybeNotifyLoginQr(session, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=first',
    authenticated: false,
    loginVisible: true,
  });
  await controller.maybeNotifyLoginQr(session, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=second',
    authenticated: false,
    loginVisible: true,
  });

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller keeps disabled login QR notifications across service worker restart and refreshed login URLs', async () => {
  let captureCalls = 0;
  let nowMs = 1_000_000;
  const throttleState = new Map();
  const disabledState = new Map();
  const loginQrNotifications = [];
  const makeRestartedController = () => makeController({
    nowMs: () => nowMs,
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,restart${captureCalls}=`,
        source: `restart-${captureCalls}`,
      };
    },
    getLoginQrLastNotifiedAtMs: async (platformId, currentUrl) => (
      throttleState.get(`${platformId}:${currentUrl}`) || 0
    ),
    setLoginQrLastNotifiedAtMs: async (platformId, currentUrl, notifiedAtMs) => {
      throttleState.set(`${platformId}:${currentUrl}`, Number(notifiedAtMs || 0));
    },
    getLoginQrNotificationsDisabled: async (platformId) => (
      Boolean(disabledState.get(platformId))
    ),
    setLoginQrNotificationsDisabled: async (platformId, disabled) => {
      disabledState.set(platformId, Boolean(disabled));
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return {
          ok: true,
          dispatched: false,
          disabled: true,
          message: 'Discord login QR webhook is not configured.',
        };
      },
    },
  }).controller;

  const firstSession = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  await makeRestartedController().maybeNotifyLoginQr(firstSession, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=first',
    authenticated: false,
    loginVisible: true,
  });
  nowMs += 10 * 60_000;

  const restartedSession = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };
  await makeRestartedController().maybeNotifyLoginQr(restartedSession, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=second',
    authenticated: false,
    loginVisible: true,
  });

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller treats accepted non-dispatched login QR result as disabled', async () => {
  let captureCalls = 0;
  let nowMs = 1_000_000;
  const loginQrNotifications = [];
  const { controller } = makeController({
    nowMs: () => nowMs,
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,nodispatch${captureCalls}=`,
        source: `no-dispatch-${captureCalls}`,
      };
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return {
          ok: true,
          dispatched: false,
          message: 'notification not dispatched',
        };
      },
    },
  });
  const session = {
    platformId: 'fanqie',
    popupTabId: 42,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  };

  await controller.maybeNotifyLoginQr(session, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=first',
    authenticated: false,
    loginVisible: true,
  });
  nowMs += 10 * 60_000;
  await controller.maybeNotifyLoginQr(session, {
    currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=second',
    authenticated: false,
    loginVisible: true,
  });

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller ignores login QR notification failures while login remains visible', async () => {
  const { controller, events } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/login',
      platform: 'qidian',
      authenticated: false,
      loginVisible: true,
    }),
    captureLoginQrImage: async () => ({
      ok: true,
      imageDataUrl: 'data:image/png;base64,cXI=',
      source: 'canvas',
    }),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      getBrowserSession: async () => null,
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async () => {
        throw new Error('network down');
      },
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );
  await controller.handleTabUpdated(42, { url: 'https://write.qq.com/login' }, { id: 42 });

  assert.equal(events.at(-1).eventName, 'login-status');
  assert.equal(events.at(-1).payload.connected, false);
});

test('controller refuses to send debugger screenshots as active login QR notifications', async () => {
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/login',
      platform: 'fanqie',
      authenticated: false,
      loginVisible: true,
    }),
    captureLoginQrImage: async () => ({
      ok: true,
      imageDataUrl: 'data:image/png;base64,c2NyZWVuc2hvdA==',
      source: 'debugger-screenshot',
    }),
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  controller.loginSessions.set(321, {
    platformId: 'fanqie',
    popupTabId: 321,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  });
  await controller.handleTabUpdated(
    321,
    { url: 'https://fanqienovel.com/main/writer/login' },
    { id: 321 },
  );

  assert.equal(loginQrNotifications.length, 0);
  assert.deepEqual(
    loginQrStatusEvents.map((event) => event.phase),
    ['capture-start', 'capture-rejected'],
  );
  assert.equal(loginQrStatusEvents[1].reason, 'non-qr-screenshot-capture');
});

test('controller restores backend sessions before heartbeat even when local browser already has cookies', async () => {
  const heartbeatCalls = [];
  const { controller, restoredCookies } = makeController({
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'sid_tt' }]
        : []
    ),
    backend: {
      heartbeat: async (payload) => {
        heartbeatCalls.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 2 }),
      getBrowserSession: async (platformId) => {
        if (platformId !== 'fanqie') {
          return null;
        }
        return {
          platform: 'fanqie',
          client_id: 'laptop-client',
          cookie_count: 2,
          cookies: [
            {
              name: 'sessionid',
              value: 'cookie-value',
              domain: '.fanqienovel.com',
              path: '/',
              secure: true,
              httpOnly: true,
              sameSite: 'none',
              expirationDate: 1893456000,
            },
            {
              name: 'sid_tt',
              value: 'cookie-value-2',
              domain: '.fanqienovel.com',
              path: '/',
              secure: true,
              httpOnly: true,
              sameSite: 'none',
              expirationDate: 1893456000,
            },
          ],
        };
      },
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  await controller.bootstrap();

  assert.equal(restoredCookies.length, 1);
  assert.equal(restoredCookies[0].platformId, 'fanqie');
  assert.equal(restoredCookies[0].cookies.length, 2);
  assert.equal(heartbeatCalls.length, 1);
});

test('controller syncs pending upload journal entries before claiming new work', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const seedJournal = journalStore.createJournal();
  const pendingClaim = makeUploadClaim({
    job: {
      job_id: 'job-recovery',
      idempotency_key: 'publisher-job:v1:job-recovery',
    },
    attempt: { attempt_id: 'attempt-recovery' },
  });
  await seedJournal.recordClaim({ clientId: 'client-1', claim: pendingClaim });
  await seedJournal.saveResult('attempt-recovery', {
    outcome: 'failed',
    message: '',
    current_url: '',
    error_code: 'extension-worker-restarted',
    error_message: 'Worker restarted.',
    details: {},
  });

  const calls = [];
  const { controller } = makeController({
    getPlatformState: async (platformId) => (
      platformId === 'qidian' ? { connected: true } : {}
    ),
    backend: {
      submitUploadAttemptResult: async () => {
        calls.push('journal-result');
        return { attempt_status: 'failed', job_status: 'failed' };
      },
      claimNextUploadJob: async () => {
        calls.push('claim');
        return { found: false, claim: null };
      },
    },
  }, journalStore);

  const dispatchResult = await controller.dispatchPendingUploadJobs();

  assert.deepEqual(calls, ['journal-result', 'claim']);
  assert.deepEqual(dispatchResult, { found: false });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller persists and acknowledges mutation_started before running the mutation command', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const calls = [];
  const { controller } = makeController({
    runUploadCommand: async () => {
      const record = journalStore.snapshot.records[0];
      assert.equal(record.local_phase, 'mutation_started');
      assert.deepEqual(calls, ['phase']);
      calls.push('mutation');
      return {
        ok: false,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        errorCode: 'platform-rejected',
        error: 'Rejected by platform.',
      };
    },
    backend: {
      updateUploadAttemptPhase: async (_jobId, _attemptId, payload) => {
        assert.equal(journalStore.snapshot.records[0].local_phase, 'mutation_started');
        assert.equal(payload.phase, 'mutation_started');
        calls.push('phase');
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'heartbeat',
        };
      },
      submitUploadAttemptResult: async () => {
        calls.push('result');
        return { attempt_status: 'failed', job_status: 'failed' };
      },
    },
  }, journalStore);

  await controller.executeUploadClaim(makeUploadClaim());

  assert.deepEqual(calls, ['phase', 'mutation', 'result']);
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller journals a pre-mutation risk pause before reporting it and never mutates', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const calls = [];
  const { controller } = makeController({
    inspectPlatformRiskCommand: async () => ({
      detected: true,
      riskPause: true,
      riskReason: 'captcha',
      currentUrl: 'https://write.qq.com/portal/dashboard',
      riskEvidence: {
        detector: 'publisher-risk-v1',
        boundary: 'pre-mutation',
        selector: 'iframe[src*="captcha"]',
        matchedText: '',
      },
    }),
    runUploadCommand: async () => {
      calls.push('mutation');
      throw new Error('mutation must not run while a challenge is visible');
    },
    backend: {
      updateUploadAttemptPhase: async () => {
        calls.push('phase');
        throw new Error('mutation phase must not start while paused');
      },
      pauseUploadAttempt: async (_jobId, _attemptId, payload) => {
        assert.equal(journalStore.snapshot.records[0].local_phase, 'paused');
        assert.equal(journalStore.snapshot.records[0].pause.risk_reason, 'captcha');
        assert.equal(payload.risk_reason, 'captcha');
        calls.push('pause');
        return {
          attempt_status: 'paused',
          job_status: 'paused',
          next_action: 'operator_review',
        };
      },
    },
  }, journalStore);

  const outcome = await controller.executeUploadClaim(makeUploadClaim());

  assert.deepEqual(calls, ['pause']);
  assert.deepEqual(outcome, { status: 'paused' });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
  assert.equal(journalStore.snapshot.records[0].pause.risk_reason, 'captcha');
});

test('controller fails closed when the pre-mutation risk inspection times out', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  let mutationCalls = 0;
  let phaseCalls = 0;
  let submittedResult;
  const { controller } = makeController({
    inspectPlatformRiskCommand: async () => ({
      ok: false,
      errorCode: 'platform-agent-timeout',
      error: '平台页面风险检查超时。',
      resultPayload: { phase: 'message-timeout' },
    }),
    runUploadCommand: async () => {
      mutationCalls += 1;
      return { ok: true };
    },
    backend: {
      updateUploadAttemptPhase: async () => {
        phaseCalls += 1;
        return { attempt_status: 'running', job_status: 'running' };
      },
      submitUploadAttemptResult: async (_jobId, _attemptId, payload) => {
        submittedResult = payload;
        return { attempt_status: 'failed', job_status: 'pending' };
      },
    },
  }, journalStore);

  const outcome = await controller.executeUploadClaim(makeUploadClaim());

  assert.equal(mutationCalls, 0);
  assert.equal(phaseCalls, 0);
  assert.equal(submittedResult.error_code, 'platform-agent-timeout');
  assert.deepEqual(outcome, { status: 'pending' });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller journals a risk detected after mutation_started before backend pause', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const calls = [];
  const { controller } = makeController({
    inspectPlatformRiskCommand: async () => ({ detected: false }),
    runUploadCommand: async () => {
      calls.push('mutation');
      return {
        ok: false,
        riskPause: true,
        riskReason: 'account_risk',
        currentUrl: 'https://write.qq.com/portal/dashboard',
        errorCode: 'publisher-risk-pause',
        error: 'Account risk signal detected.',
        riskEvidence: {
          detector: 'publisher-risk-v1',
          boundary: 'post-save',
          matchedText: '账号存在风险',
        },
      };
    },
    backend: {
      updateUploadAttemptPhase: async () => {
        calls.push('phase');
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'heartbeat',
        };
      },
      pauseUploadAttempt: async (_jobId, _attemptId, payload) => {
        assert.equal(journalStore.snapshot.records[0].local_phase, 'paused');
        assert.equal(journalStore.snapshot.records[0].pause.risk_reason, 'account_risk');
        assert.equal(payload.evidence.boundary, 'post-save');
        calls.push('pause');
        return {
          attempt_status: 'paused',
          job_status: 'paused',
          next_action: 'operator_review',
        };
      },
    },
  }, journalStore);

  const outcome = await controller.executeUploadClaim(makeUploadClaim());

  assert.deepEqual(calls, ['phase', 'mutation', 'pause']);
  assert.deepEqual(outcome, { status: 'paused' });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller restart replays a durable risk pause without reporting failure', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const seedJournal = journalStore.createJournal();
  const claim = makeUploadClaim();
  await seedJournal.recordClaim({ clientId: 'client-1', claim });
  await seedJournal.savePause('attempt-1', {
    risk_reason: 'mfa',
    observed_at: '2026-07-21T12:01:00Z',
    current_url: 'https://write.qq.com/portal/dashboard',
    evidence: {
      detector: 'publisher-risk-v1',
      boundary: 'before-confirm',
      matched_text: '短信验证码',
    },
  });
  const calls = [];
  const { controller } = makeController({
    backend: {
      pauseUploadAttempt: async (_jobId, _attemptId, payload) => {
        calls.push(['pause', payload.risk_reason]);
        return { attempt_status: 'paused', job_status: 'paused' };
      },
      submitUploadAttemptResult: async () => {
        calls.push(['result']);
        throw new Error('risk pause must not become a failure result');
      },
    },
  }, journalStore);

  const replay = await controller.syncUploadJournal();

  assert.deepEqual(replay, { handled: 1 });
  assert.deepEqual(calls, [['pause', 'mfa']]);
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
  assert.equal(journalStore.snapshot.records[0].result, null);
});

test('controller keeps a durable risk pause when an expired fence is not covered', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const seedJournal = journalStore.createJournal();
  await seedJournal.recordClaim({ clientId: 'client-1', claim: makeUploadClaim() });
  await seedJournal.savePause('attempt-1', {
    risk_reason: 'captcha',
    observed_at: '2026-07-21T12:01:00Z',
    current_url: 'https://write.qq.com/portal/dashboard',
    evidence: { detector: 'publisher-risk-v1', boundary: 'pre-mutation' },
  });
  const { controller } = makeController({
    backend: {
      pauseUploadAttempt: async () => {
        const error = new Error('publisher attempt lease has expired');
        error.code = 'lease_expired';
        error.payload = {
          detail: {
            code: 'lease_expired',
            job_status: 'pending',
            current_attempt_id: '',
          },
        };
        throw error;
      },
    },
  }, journalStore);

  await assert.rejects(controller.syncUploadJournal(), /lease has expired/);

  assert.equal(journalStore.snapshot.records[0].local_phase, 'paused');
  assert.equal(journalStore.snapshot.records[0].pause.risk_reason, 'captcha');
});

test('controller retires a durable pause after a later terminal operation', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const seedJournal = journalStore.createJournal();
  await seedJournal.recordClaim({ clientId: 'client-1', claim: makeUploadClaim() });
  await seedJournal.savePause('attempt-1', {
    risk_reason: 'account_risk',
    observed_at: '2026-07-21T12:01:00Z',
    current_url: 'https://write.qq.com/portal/dashboard',
    evidence: { detector: 'publisher-risk-v1', boundary: 'post-save' },
  });
  const { controller } = makeController({
    backend: {
      pauseUploadAttempt: async () => {
        const error = new Error('publisher attempt fence is stale');
        error.code = 'stale_attempt';
        error.payload = {
          detail: {
            code: 'stale_attempt',
            job_status: 'cancelled',
            current_attempt_id: '',
          },
        };
        throw error;
      },
    },
  }, journalStore);

  assert.deepEqual(await controller.syncUploadJournal(), { handled: 1 });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller never mutates when the backend does not acknowledge mutation_started', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  let mutationCalls = 0;
  let resultCalls = 0;
  const { controller, closedTabs } = makeController({
    runUploadCommand: async () => {
      mutationCalls += 1;
      return { ok: true };
    },
    backend: {
      updateUploadAttemptPhase: async () => {
        const error = new Error('phase endpoint unavailable');
        error.status = 503;
        throw error;
      },
      submitUploadAttemptResult: async () => {
        resultCalls += 1;
        return { attempt_status: 'failed', job_status: 'reconciling' };
      },
    },
  }, journalStore);

  await assert.rejects(
    controller.executeUploadClaim(makeUploadClaim()),
    /phase endpoint unavailable/,
  );

  assert.equal(mutationCalls, 0);
  assert.equal(resultCalls, 0);
  assert.deepEqual(closedTabs, [77]);
  assert.equal(journalStore.snapshot.records[0].local_phase, 'mutation_started');
});

test('controller adopts unparented tabs while exactly one execution is active', async () => {
  const { controller, closedTabs } = makeController();
  controller.registerExecutionTask('upload:job-1:attempt-1', 77);

  await controller.handleTabCreated({ id: 88, openerTabId: 0 });
  const cleanup = await controller.cleanupExecutionTabs('upload:job-1:attempt-1');

  assert.deepEqual(cleanup.closed_tab_ids, [77, 88]);
  assert.deepEqual(closedTabs, [77, 88]);
});

test('controller persists receipt and result before backend acknowledgements on success', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const calls = [];
  let submittedReceipt;
  let submittedResult;
  const { controller } = makeController({
    runUploadCommand: async () => {
      calls.push('mutation');
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
        message: 'Saved.',
        resultPayload: {
          official_status: 'drafted',
          ...executionContentEvidence(),
        },
      };
    },
    backend: {
      updateUploadAttemptPhase: async (_jobId, _attemptId, payload) => {
        assert.equal(journalStore.snapshot.records[0].local_phase, 'mutation_started');
        assert.equal(payload.phase, 'mutation_started');
        calls.push('phase');
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'heartbeat',
        };
      },
      submitUploadReceipt: async (_jobId, _attemptId, payload) => {
        const record = journalStore.snapshot.records[0];
        const { client_id: clientId, lease_epoch: leaseEpoch, ...receipt } = payload;
        assert.equal(record.local_phase, 'ack_pending');
        assert.equal(clientId, 'client-1');
        assert.equal(leaseEpoch, 7);
        assert.deepEqual(record.receipt, receipt);
        assert.ok(record.result);
        submittedReceipt = payload;
        calls.push('receipt');
        return { attempt_status: 'running', job_status: 'running' };
      },
      submitUploadAttemptResult: async (_jobId, _attemptId, payload) => {
        const record = journalStore.snapshot.records[0];
        const { client_id: clientId, lease_epoch: leaseEpoch, ...result } = payload;
        assert.equal(record.local_phase, 'ack_pending');
        assert.equal(clientId, 'client-1');
        assert.equal(leaseEpoch, 7);
        assert.deepEqual(record.result, result);
        assert.ok(record.receipt);
        submittedResult = payload;
        calls.push('result');
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
    },
  }, journalStore);

  const outcome = await controller.executeUploadClaim(makeUploadClaim());

  assert.deepEqual(calls, ['phase', 'mutation', 'receipt', 'result']);
  assert.equal(submittedReceipt.remote_book_id, '222');
  assert.equal(submittedReceipt.remote_chapter_id, '333');
  assert.equal(submittedResult.outcome, 'succeeded');
  assert.equal(outcome.status, 'succeeded');
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
  assert.deepEqual(
    journalStore.writes.map((snapshot) => snapshot.records[0].local_phase),
    ['claimed', 'mutation_started', 'receipt_observed', 'ack_pending', 'acked'],
  );
});

test('controller restart replays receipt and result without repeating a mutation', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  let mutationCalls = 0;
  let firstResultCalls = 0;
  const firstWorker = makeController({
    runUploadCommand: async () => {
      mutationCalls += 1;
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
        message: 'Saved.',
        resultPayload: executionContentEvidence(),
      };
    },
    backend: {
      submitUploadReceipt: async () => {
        throw new Error('receipt endpoint unavailable');
      },
      submitUploadAttemptResult: async () => {
        firstResultCalls += 1;
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
    },
  }, journalStore);

  await assert.rejects(
    firstWorker.controller.executeUploadClaim(makeUploadClaim()),
    /receipt endpoint unavailable/,
  );
  assert.equal(mutationCalls, 1);
  assert.equal(firstResultCalls, 0);
  assert.equal(journalStore.snapshot.records[0].local_phase, 'ack_pending');
  assert.ok(journalStore.snapshot.records[0].receipt);
  assert.ok(journalStore.snapshot.records[0].result);

  const replayCalls = [];
  const restartedWorker = makeController({
    runUploadCommand: async () => {
      throw new Error('mutation command must not run during journal replay');
    },
    runCoverUploadCommand: async () => {
      throw new Error('cover mutation command must not run during journal replay');
    },
    backend: {
      submitUploadReceipt: async () => {
        replayCalls.push('receipt');
        return { attempt_status: 'running', job_status: 'running' };
      },
      submitUploadAttemptResult: async () => {
        replayCalls.push('result');
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
    },
  }, journalStore);

  const syncResult = await restartedWorker.controller.syncUploadJournal();

  assert.deepEqual(replayCalls, ['receipt', 'result']);
  assert.equal(mutationCalls, 1);
  assert.deepEqual(syncResult, { handled: 1 });
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller recovers a mutation_started crash through read-only reconciliation only', async () => {
  const journalStore = makeMemoryUploadJournalStore();
  const seedJournal = journalStore.createJournal();
  const interruptedClaim = makeUploadClaim();
  await seedJournal.recordClaim({ clientId: 'client-1', claim: interruptedClaim });
  await seedJournal.markMutationStarted(interruptedClaim.attempt.attempt_id);

  const calls = [];
  let claimCalls = 0;
  const reconcileClaim = makeUploadClaim({
    execution_mode: 'reconcile',
    attempt: {
      attempt_id: 'attempt-2',
      attempt_number: 2,
      lease_epoch: 8,
    },
  });
  const { controller } = makeController({
    getPlatformState: async (platformId) => (
      platformId === 'qidian' ? { connected: true } : {}
    ),
    runUploadCommand: async () => {
      calls.push('mutation');
      throw new Error('recovery must not repeat the remote mutation');
    },
    runCoverUploadCommand: async () => {
      calls.push('cover-mutation');
      throw new Error('recovery must not invoke a cover mutation');
    },
    runReconciliationCommand: async () => {
      calls.push('read-only-reconcile');
      return {
        outcome: 'matched',
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
        matchedContentSha256: CONTENT_SHA256,
        officialState: 'drafted',
        confirmationText: 'Saved.',
        matchBasis: ['chapter_title', 'content_sha256'],
      };
    },
    backend: {
      submitUploadAttemptResult: async (_jobId, attemptId, payload) => {
        calls.push(`retire:${attemptId}:${payload.outcome}`);
        return { attempt_status: 'failed', job_status: 'reconciling' };
      },
      claimNextUploadJob: async () => {
        claimCalls += 1;
        return claimCalls === 1
          ? { found: true, claim: reconcileClaim }
          : { found: false, claim: null };
      },
      updateUploadAttemptPhase: async (_jobId, attemptId, payload) => {
        calls.push(`phase:${attemptId}:${payload.phase}`);
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'reconcile',
        };
      },
      reconcileUploadAttempt: async (_jobId, attemptId, payload) => {
        calls.push(`reconcile:${attemptId}:${payload.outcome}`);
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
      submitUploadReceipt: async () => {
        throw new Error('matched reconciliation submits its receipt atomically');
      },
    },
  }, journalStore);

  const outcome = await controller.dispatchPendingUploadJobs();

  assert.deepEqual(calls, [
    'retire:attempt-1:failed',
    'phase:attempt-2:observation_started',
    'read-only-reconcile',
    'reconcile:attempt-2:matched',
  ]);
  assert.deepEqual(outcome, { found: true, handled: 1 });
  assert.equal(claimCalls, 2);
  assert.deepEqual(
    journalStore.snapshot.records.map((record) => record.local_phase),
    ['acked', 'acked'],
  );
});

test('controller routes reconcile claims only through read-only reconciliation', async () => {
  const commandCalls = [];
  const backendCalls = [];
  let reconciliationRequest;
  const { controller, journalStore } = makeController({
    runUploadCommand: async () => {
      commandCalls.push('upload');
      throw new Error('upload mutation must not run for reconciliation');
    },
    runCoverUploadCommand: async () => {
      commandCalls.push('cover');
      throw new Error('cover mutation must not run for reconciliation');
    },
    runReconciliationCommand: async () => {
      commandCalls.push('reconcile');
      return {
        outcome: 'matched',
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
        matchedContentSha256: CONTENT_SHA256,
        officialState: 'drafted',
        confirmationText: 'Saved.',
        matchBasis: ['content_sha256'],
      };
    },
    backend: {
      updateUploadAttemptPhase: async (_jobId, _attemptId, payload) => {
        backendCalls.push(payload.phase);
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'reconcile',
        };
      },
      reconcileUploadAttempt: async (_jobId, _attemptId, payload) => {
        backendCalls.push('reconcile-result');
        reconciliationRequest = payload;
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
      submitUploadAttemptResult: async () => {
        throw new Error('typed mutation result endpoint must not handle reconciliation');
      },
    },
  });

  const outcome = await controller.executeUploadClaim(
    makeUploadClaim({ execution_mode: 'reconcile' }),
  );

  assert.deepEqual(commandCalls, ['reconcile']);
  assert.deepEqual(backendCalls, ['observation_started', 'reconcile-result']);
  assert.equal(reconciliationRequest.outcome, 'matched');
  assert.equal(reconciliationRequest.receipt.remote_book_id, '222');
  assert.equal(outcome.status, 'succeeded');
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller consumes nested claim envelopes and caps each dispatch at eight', async () => {
  let claimCalls = 0;
  const receivedClaims = [];
  const { controller } = makeController({
    getPlatformState: async (platformId) => (
      platformId === 'qidian' ? { connected: true } : {}
    ),
    backend: {
      claimNextUploadJob: async () => {
        claimCalls += 1;
        const claim = makeUploadClaim({
          job: {
            job_id: 'job-' + claimCalls,
            idempotency_key: 'publisher-job:v1:job-' + claimCalls,
          },
          attempt: { attempt_id: 'attempt-' + claimCalls },
        });
        return { found: true, claim };
      },
    },
  });
  controller.executeUploadClaim = async (claim) => {
    receivedClaims.push(claim);
    return { status: 'succeeded' };
  };

  const result = await controller.dispatchPendingUploadJobs();

  assert.deepEqual(result, { found: true, handled: 8, truncated: true });
  assert.equal(claimCalls, 8);
  assert.equal(receivedClaims.length, 8);
  assert.equal(receivedClaims[0].execution_mode, 'execute');
  assert.equal(receivedClaims[0].job.job_id, 'job-1');
  assert.equal(receivedClaims[0].attempt.attempt_id, 'attempt-1');
  assert.equal(receivedClaims[7].job.job_id, 'job-8');
});

test('controller reports audit observations through the typed result endpoint', async () => {
  const commandCalls = [];
  const backendCalls = [];
  let submittedResult;
  const auditClaim = makeUploadClaim({
    execution_mode: 'reconcile',
    job: {
      job_id: 'audit-job-1',
      idempotency_key: 'publisher-job:v1:audit-job-1',
      task_kind: 'audit_sync',
      input: {
        book_name: 'Test Book',
        work_binding_id: 'binding-1',
        upload_url: 'https://write.qq.com/portal/book/222',
        remote_book_id: '222',
      },
    },
    attempt: { attempt_id: 'audit-attempt-1' },
  });
  const { controller, journalStore } = makeController({
    runUploadCommand: async () => {
      commandCalls.push('upload');
      throw new Error('chapter mutation must not run for audit observation');
    },
    runCoverUploadCommand: async () => {
      commandCalls.push('cover');
      throw new Error('cover mutation must not run for audit observation');
    },
    runAuditSyncCommand: async (_tabId, payload) => {
      commandCalls.push('audit');
      assert.equal(payload.remote_book_id, '222');
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/book/222',
        message: 'Audit state synchronized.',
        resultPayload: {
          work: {
            work_binding_id: 'binding-1',
            remote_book_id: '222',
            remote_url: 'https://write.qq.com/portal/book/222',
            audit_state: 'under_review',
            official_status: 'Review pending',
            platform_message: 'Audit state synchronized.',
          },
          chapters: [{
            chapter_number: 1,
            chapter_title: 'Chapter 1',
            remote_chapter_id: 'chapter-1',
            remote_chapter_url: 'https://write.qq.com/portal/book/222/chapter/1',
            publish_state: 'published',
            audit_state: 'approved',
            audit_reason: '',
            word_count: 1200,
          }],
          cover: { cover_state: 'under_review' },
          milestones: [{
            milestone_type: 'first_review',
            state: 'open',
            message: 'Review started.',
          }],
        },
      };
    },
    backend: {
      updateUploadAttemptPhase: async (_jobId, _attemptId, payload) => {
        backendCalls.push(payload.phase);
        return {
          attempt_status: 'running',
          job_status: 'running',
          abort_requested: false,
          next_action: 'reconcile',
        };
      },
      submitUploadAttemptResult: async (_jobId, _attemptId, payload) => {
        backendCalls.push('typed-result');
        submittedResult = payload;
        return { attempt_status: 'succeeded', job_status: 'succeeded' };
      },
      reconcileUploadAttempt: async () => {
        throw new Error('audit observations must use the typed result endpoint');
      },
    },
  });

  const outcome = await controller.executeUploadClaim(auditClaim);

  assert.deepEqual(commandCalls, ['audit']);
  assert.deepEqual(backendCalls, ['observation_started', 'typed-result']);
  assert.equal(submittedResult.outcome, 'succeeded');
  assert.equal(submittedResult.details.work.audit_state, 'under_review');
  assert.equal(submittedResult.details.chapters[0].audit_state, 'approved');
  assert.deepEqual(Object.keys(submittedResult.details).sort(), [
    'chapters',
    'cover',
    'milestones',
    'work',
  ]);
  assert.equal(outcome.status, 'succeeded');
  assert.equal(journalStore.snapshot.records[0].local_phase, 'acked');
});

test('controller auto-dispatches claimed comment sync job for connected platform', async () => {
  let claimedOnce = false;
  const commentResults = [];
  const syncedPayloads = [];
  const { controller } = makeController({
    getTab: async () => ({ id: 77, url: 'https://fanqienovel.com/main/writer/' }),
    getPlatformState: async (platformId) => (platformId === 'fanqie' ? { connected: true } : {}),
    runCommentSyncCommand: async (_tabId, payload) => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/',
      message: '评论同步已完成。',
      comments: [
        {
          remote_comment_id: 'book:comment-1',
          work_id: payload.work_id,
          work_name: payload.work_name,
          chapter_id: payload.chapter_id,
          chapter_title: payload.chapter_title,
          author_id: 'reader-1',
          author_name: '读者A',
          body: '催更',
          parent_remote_comment_id: '',
          created_at: '2026-04-04T10:00:00Z',
          like_count: 2,
          reply_count: 1,
          raw_payload: { body: '催更' },
        },
      ],
      resultPayload: { source: 'fanqie-author-api' },
    }),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextCommentSyncJob: async () => {
        if (claimedOnce) {
          return { found: false, job: null };
        }
        claimedOnce = true;
        return {
          found: true,
          job: {
            job_id: 'comment-job-1',
            platform: 'fanqie',
            status: 'running',
            work_id: 'book-1',
            work_name: '测试书',
            chapter_id: 'chapter-1',
            chapter_title: '第一章',
            limit: 20,
          },
        };
      },
      syncCommentsBatch: async (payload) => {
        syncedPayloads.push(payload);
        return { ok: true, inserted: 1, updated: 0 };
      },
      updateCommentSyncJobResult: async (_jobId, payload) => {
        commentResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.dispatchPendingCommentSyncJobs();

  assert.equal(syncedPayloads.length, 1);
  assert.equal(syncedPayloads[0].job_id, 'comment-job-1');
  assert.equal(syncedPayloads[0].comments.length, 1);
  assert.equal(commentResults.at(-1).status, 'succeeded');
  assert.equal(commentResults.at(-1).result_payload.inserted, 1);
});

test('controller heartbeat reports cookie summary without leaking full cookies', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
    },
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken', value: 'secret-token' }, { name: 'pubtoken', value: 'secret-cookie' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.equal(payloads.length, 1);
  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(Array.isArray(qidian.cookies), false);
  assert.equal(qidian.raw_state.cookie_count, 2);
  assert.deepEqual(qidian.raw_state.cookie_names, ['AppAuthToken', 'pubtoken']);
});

test('controller heartbeat uses inspected login page to override stale auth cookies', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? {
          ok: true,
          currentUrl: 'https://write.qq.com/portal/login',
          platform: 'qidian',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, false);
  assert.equal(qidian.last_error, 'login-required');
  assert.equal(qidian.raw_state.cookie_signal, true);
  assert.equal(qidian.raw_state.page_login_visible, true);
});

test('controller heartbeat probes dashboard when login page has strong cookies', async () => {
  const payloads = [];
  const probes = [];
  let captureCalls = 0;
  const { controller, loginQrNotifications } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? {
          ok: true,
          tabId: 123,
          currentUrl: 'https://write.qq.com/portal/login',
          platform: 'qidian',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'qidian') {
        return null;
      }
      return {
        ok: true,
        tabId: 88,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        platform: 'qidian',
        authenticated: true,
        loginVisible: false,
        summary: 'probe dashboard evidence',
      };
    },
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(probes, ['qidian']);
  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, true);
  assert.equal(qidian.raw_state.cookie_signal, true);
  assert.equal(qidian.raw_state.page_authenticated, true);
  assert.equal(qidian.raw_state.current_url, 'https://write.qq.com/portal/dashboard');
});

test('controller heartbeat does not send login QR notification without an active login session', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 321,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'plain-heartbeat',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  await controller.sendHeartbeat();
  await controller.sendHeartbeat();

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.deepEqual(
    loginQrStatusEvents.map((event) => event.reason),
    [
      'heartbeat-login-page-without-active-login-session',
      'heartbeat-login-page-without-active-login-session',
    ],
  );
});

test('controller heartbeat suppresses login QR sends for an active login session', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 321,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      await new Promise((resolve) => { setTimeout(resolve, 20); });
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  controller.loginSessions.set(321, {
    platformId: 'fanqie',
    popupTabId: 321,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  });
  await Promise.all([
    controller.sendHeartbeat(),
    controller.sendHeartbeat(),
  ]);

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.deepEqual(
    loginQrStatusEvents.map((event) => event.reason),
    [
      'heartbeat-active-login-session-qr-suppressed',
      'heartbeat-active-login-session-qr-suppressed',
    ],
  );
});

test('controller heartbeat never sends a fresh QR for repeated plain login-page heartbeats', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 321,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,aHI${captureCalls}=`,
        source: `heartbeat-${captureCalls}`,
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  await controller.sendHeartbeat();
  await controller.sendHeartbeat();
  await controller.sendHeartbeat();

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
});

test('controller heartbeat keeps repeated skipped login pages from dispatching QR', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 654,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,aHI${captureCalls}=`,
        source: `heartbeat-throttle-${captureCalls}`,
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  await controller.sendHeartbeat();
  await controller.sendHeartbeat();
  await controller.sendHeartbeat();

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
});

test('controller heartbeat does not dispatch login QR after service worker restart', async () => {
  let captureCalls = 0;
  const throttleState = new Map();
  const loginQrNotifications = [];
  const makeRestartedController = () => makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? {
          ok: true,
          tabId: 987,
          currentUrl: 'https://write.qq.com/portal/login',
          platform: 'qidian',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,cXI${captureCalls}=`,
        source: `restart-${captureCalls}`,
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
    getLoginQrLastNotifiedAtMs: async (platformId, currentUrl) => (
      throttleState.get(`${platformId}:${currentUrl}`) || 0
    ),
    setLoginQrLastNotifiedAtMs: async (platformId, currentUrl, notifiedAtMs) => {
      throttleState.set(`${platformId}:${currentUrl}`, Number(notifiedAtMs || 0));
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return { ok: true, dispatched: true };
      },
    },
  }).controller;

  await makeRestartedController().sendHeartbeat();
  await makeRestartedController().sendHeartbeat();
  await makeRestartedController().sendHeartbeat();

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.equal(throttleState.size, 0);
});

test('controller locally throttles active login QR when backend accepts without dispatch', async () => {
  let captureCalls = 0;
  const loginQrNotifications = [];
  const throttleState = new Map();
  const { controller } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/login',
      platform: 'qidian',
      authenticated: false,
      loginVisible: true,
    }),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: `data:image/png;base64,cXI${captureCalls}=`,
        source: `not-dispatched-${captureCalls}`,
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
    getLoginQrLastNotifiedAtMs: async (platformId, currentUrl) => (
      throttleState.get(`${platformId}:${currentUrl}`) || 0
    ),
    setLoginQrLastNotifiedAtMs: async (platformId, currentUrl, notifiedAtMs) => {
      throttleState.set(`${platformId}:${currentUrl}`, Number(notifiedAtMs || 0));
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return { ok: true, dispatched: false, message: 'Discord login QR webhook is not configured.' };
      },
    },
  });

  controller.loginSessions.set(987, {
    platformId: 'qidian',
    popupTabId: 987,
    lastUrl: 'https://write.qq.com/portal/login',
  });
  await controller.handleTabUpdated(
    987,
    { url: 'https://write.qq.com/portal/login' },
    { id: 987 },
  );
  await controller.handleTabUpdated(
    987,
    { url: 'https://write.qq.com/portal/login' },
    { id: 987 },
  );

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
  assert.equal(throttleState.size, 2);
});

test('controller lets active login sessions own QR notifications for their tabs', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/login',
      platform: 'fanqie',
      authenticated: false,
      loginVisible: true,
    }),
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 42,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async () => [],
  });

  await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'fanqie' } },
    { tab: { id: 99 } },
  );
  await controller.handleTabUpdated(42, { url: 'https://fanqienovel.com/main/writer/login' }, { id: 42 });

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
});

test('controller heartbeat does not send login QR notification when known login URL is visible', async () => {
  let captureCalls = 0;
  const { controller, loginQrNotifications, loginQrStatusEvents } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? {
          ok: true,
          tabId: 456,
          currentUrl: 'https://write.qq.com/portal/login',
          platform: 'qidian',
          authenticated: false,
          loginVisible: false,
        }
        : null
    ),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'known-url',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.equal(captureCalls, 0);
  assert.equal(loginQrNotifications.length, 0);
  assert.equal(loginQrStatusEvents.length, 1);
  assert.equal(loginQrStatusEvents[0].reason, 'heartbeat-login-page-without-active-login-session');
});

test('controller records local login-required state when heartbeat sees a login page', async () => {
  const platformStateWrites = [];
  const { controller } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? {
          ok: true,
          tabId: 456,
          currentUrl: 'https://write.qq.com/portal/login',
          platform: 'qidian',
          authenticated: false,
          loginVisible: false,
        }
        : null
    ),
    setPlatformState: async (platformId, state) => {
      platformStateWrites.push({ platformId, state });
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(platformStateWrites, [
    {
      platformId: 'qidian',
      state: {
        connected: false,
        loginMethod: 'scan',
        lastError: 'login-required',
      },
    },
  ]);
});

test('controller skips platform probe when local state says login is required without cookie signal', async () => {
  let probeCalls = 0;
  const { controller } = makeController({
    inspectPlatformState: async () => null,
    ensurePlatformProbeInspection: async () => {
      probeCalls += 1;
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/login',
        platform: 'qidian',
        authenticated: false,
        loginVisible: true,
      };
    },
    getPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? { connected: false, loginMethod: 'scan', lastError: 'login-required' }
        : {}
    ),
    getCookies: async () => [],
  });

  await controller.sendHeartbeat();

  assert.equal(probeCalls, 0);
});

test('controller probes Fanqie dashboard when strong cookies outlive stale login-required state', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async () => null,
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'fanqie') {
        return null;
      }
      return {
        ok: true,
        tabId: 92,
        currentUrl: 'https://fanqienovel.com/main/writer/',
        platform: 'fanqie',
        authenticated: true,
        loginVisible: false,
        summary: '作家专区 工作台 作品管理',
      };
    },
    getPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? { connected: false, loginMethod: 'scan', lastError: 'login-required' }
        : {}
    ),
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'has_biz_token' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(probes, ['fanqie']);
  const fanqie = payloads[0].platforms.find((item) => item.platform === 'fanqie');
  assert.equal(fanqie.connected, true);
  assert.equal(fanqie.last_error, '');
  assert.equal(fanqie.raw_state.cookie_signal, true);
  assert.equal(fanqie.raw_state.page_authenticated, true);
});

test('controller records skipped login QR notification status for plain heartbeat login page', async () => {
  const { controller, loginQrStatusEvents } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 321,
          currentUrl: 'https://fanqienovel.com/main/writer/login?ticket=secret',
          platform: 'fanqie',
          authenticated: false,
          loginVisible: true,
        }
        : null
    ),
    captureLoginQrImage: async () => ({
      ok: true,
      imageDataUrl: 'data:image/png;base64,cXI=',
      source: 'image',
    }),
    getPlatformState: async () => ({}),
    getCookies: async () => [],
    backend: {
      heartbeat: async () => ({ ok: true }),
      notifyLoginQr: async () => ({ ok: true, dispatched: false, message: 'not configured' }),
    },
  });

  await controller.sendHeartbeat();

  assert.deepEqual(
    loginQrStatusEvents.map((event) => event.phase),
    ['skipped'],
  );
  assert.equal(loginQrStatusEvents[0].platform, 'fanqie');
  assert.equal(loginQrStatusEvents[0].reason, 'heartbeat-login-page-without-active-login-session');
});

test('controller retries active login QR notification after a failed attempt', async () => {
  let notifyAttempts = 0;
  const loginQrNotifications = [];
  const { controller, loginQrStatusEvents } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/login',
      platform: 'fanqie',
      authenticated: false,
      loginVisible: true,
    }),
    captureLoginQrImage: async () => ({
      ok: true,
      imageDataUrl: 'data:image/png;base64,cXI=',
      source: 'image',
    }),
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'passport_auth_status' }]
        : []
    ),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      getBrowserSession: async () => null,
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async (payload) => {
        notifyAttempts += 1;
        if (notifyAttempts === 1) {
          throw new Error('network down');
        }
        loginQrNotifications.push(payload);
        return { message: 'queued', dispatched: false };
      },
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  controller.loginSessions.set(789, {
    platformId: 'fanqie',
    popupTabId: 789,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  });
  await controller.handleTabUpdated(
    789,
    { url: 'https://fanqienovel.com/main/writer/login' },
    { id: 789 },
  );
  await controller.handleTabUpdated(
    789,
    { url: 'https://fanqienovel.com/main/writer/login' },
    { id: 789 },
  );

  assert.equal(notifyAttempts, 2);
  assert.equal(loginQrNotifications.length, 1);
  assert.equal(loginQrNotifications[0].platform, 'fanqie');
  assert.equal(loginQrStatusEvents.filter((event) => event.phase === 'failed').length, 1);
  assert.equal(loginQrStatusEvents.filter((event) => event.phase === 'sent').length, 1);
});

test('controller throttles active login QR notification after backend accepts it without Discord dispatch', async () => {
  let notifyAttempts = 0;
  let captureCalls = 0;
  const loginQrNotifications = [];
  const { controller, loginQrStatusEvents } = makeController({
    inspectLoginState: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/login',
      platform: 'fanqie',
      authenticated: false,
      loginVisible: true,
    }),
    captureLoginQrImage: async () => {
      captureCalls += 1;
      return {
        ok: true,
        imageDataUrl: 'data:image/png;base64,cXI=',
        source: 'image',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'passport_auth_status' }]
        : []
    ),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      getBrowserSession: async () => null,
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async (payload) => {
        notifyAttempts += 1;
        loginQrNotifications.push(payload);
        return { ok: true, dispatched: false, message: 'Discord login QR webhook is not configured.' };
      },
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  controller.loginSessions.set(789, {
    platformId: 'fanqie',
    popupTabId: 789,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  });
  await controller.handleTabUpdated(
    789,
    { url: 'https://fanqienovel.com/main/writer/login' },
    { id: 789 },
  );
  await controller.handleTabUpdated(
    789,
    { url: 'https://fanqienovel.com/main/writer/login' },
    { id: 789 },
  );

  assert.equal(notifyAttempts, 1);
  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
  assert.equal(loginQrStatusEvents.filter((event) => event.phase === 'sent').length, 1);
});

test('controller heartbeat does not display connected before page verification', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async () => null,
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, false);
  assert.equal(qidian.raw_state.cookie_signal, true);
  assert.equal(qidian.raw_state.page_evidence_required, true);
});

test('controller heartbeat probes dashboard when cookie signal has no platform tab', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async () => null,
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'qidian') {
        return null;
      }
      return {
        ok: true,
        tabId: 88,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        platform: 'qidian',
        authenticated: true,
        loginVisible: false,
        summary: 'probe dashboard evidence',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(probes, ['qidian']);
  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, true);
  assert.equal(qidian.raw_state.cookie_signal, true);
  assert.equal(qidian.raw_state.page_authenticated, true);
  assert.equal(qidian.raw_state.current_url, 'https://write.qq.com/portal/dashboard');
});

test('controller heartbeat probes Fanqie dashboard when inspection is inconclusive', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
            ok: true,
            tabId: 91,
            currentUrl: 'https://fanqienovel.com/main/writer/',
            platform: 'fanqie',
            authenticated: false,
            loginVisible: false,
            summary: '',
          }
        : null
    ),
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'fanqie') {
        return null;
      }
      return {
        ok: true,
        tabId: 92,
        currentUrl: 'https://fanqienovel.com/main/writer/',
        platform: 'fanqie',
        authenticated: true,
        loginVisible: false,
        summary: '作家专区 工作台 作品管理',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'has_biz_token' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(probes, ['fanqie']);
  const fanqie = payloads[0].platforms.find((item) => item.platform === 'fanqie');
  assert.equal(fanqie.connected, true);
  assert.equal(fanqie.raw_state.cookie_signal, true);
  assert.equal(fanqie.raw_state.page_authenticated, true);
  assert.equal(fanqie.raw_state.current_url, 'https://fanqienovel.com/main/writer/');
});

test('controller heartbeat probes dashboard when platform inspection is not ok', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    inspectPlatformState: async () => ({
      ok: false,
      authenticated: false,
      loginVisible: false,
      currentUrl: '',
    }),
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'qidian') {
        return null;
      }
      return {
        ok: true,
        tabId: 88,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        platform: 'qidian',
        authenticated: true,
        loginVisible: false,
        summary: 'probe dashboard evidence',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.sendHeartbeat();

  assert.deepEqual(probes, ['qidian']);
  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, true);
  assert.equal(qidian.raw_state.page_authenticated, true);
});

test('controller session sync carries unverified page evidence', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async (payload) => {
        payloads.push(payload);
        return { ok: true, cookie_count: 2 };
      },
    },
    inspectPlatformState: async () => null,
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.syncConnectedSessionsToBackend();

  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].platform, 'qidian');
  assert.equal(payloads[0].raw_state.connected, false);
  assert.equal(payloads[0].raw_state.cookie_signal, true);
  assert.equal(payloads[0].raw_state.page_evidence_required, true);
  assert.equal(payloads[0].raw_state.page_authenticated, false);
});

test('controller session sync uses dashboard probe before syncing cookie session', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async (payload) => {
        payloads.push(payload);
        return { ok: true, cookie_count: 2 };
      },
    },
    inspectPlatformState: async () => null,
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'qidian') {
        return null;
      }
      return {
        ok: true,
        tabId: 88,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        platform: 'qidian',
        authenticated: true,
        loginVisible: false,
        summary: 'probe dashboard evidence',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
  });

  await controller.syncConnectedSessionsToBackend();

  assert.deepEqual(probes, ['qidian']);
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].platform, 'qidian');
  assert.equal(payloads[0].raw_state.connected, true);
  assert.equal(payloads[0].raw_state.cookie_signal, true);
  assert.equal(payloads[0].raw_state.page_authenticated, true);
});

test('controller session sync probes Fanqie before syncing inconclusive cookie session', async () => {
  const payloads = [];
  const probes = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async (payload) => {
        payloads.push(payload);
        return { ok: true, cookie_count: 2 };
      },
    },
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
            ok: true,
            tabId: 91,
            currentUrl: 'https://fanqienovel.com/main/writer/',
            platform: 'fanqie',
            authenticated: false,
            loginVisible: false,
            summary: '',
          }
        : null
    ),
    ensurePlatformProbeInspection: async (platformId) => {
      probes.push(platformId);
      if (platformId !== 'fanqie') {
        return null;
      }
      return {
        ok: true,
        tabId: 92,
        currentUrl: 'https://fanqienovel.com/main/writer/',
        platform: 'fanqie',
        authenticated: true,
        loginVisible: false,
        summary: '作家专区 工作台 作品管理',
      };
    },
    getPlatformState: async () => ({}),
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'has_biz_token' }]
        : []
    ),
  });

  await controller.syncConnectedSessionsToBackend();

  assert.deepEqual(probes, ['fanqie']);
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].platform, 'fanqie');
  assert.equal(payloads[0].raw_state.connected, true);
  assert.equal(payloads[0].raw_state.cookie_signal, true);
  assert.equal(payloads[0].raw_state.page_authenticated, true);
});

test('controller heartbeat does not keep sticky connected=true without current strong cookies', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
    },
    getPlatformState: async (platformId) => (
      platformId === 'qidian'
        ? { connected: true, loginMethod: 'scan', lastError: '' }
        : {}
    ),
    getCookies: async () => [],
  });

  await controller.sendHeartbeat();

  const qidian = payloads[0].platforms.find((item) => item.platform === 'qidian');
  assert.equal(qidian.connected, false);
  assert.equal(qidian.raw_state.cookie_signal, false);
});

test('controller session sync sends only cookie fields needed by the backend uploader', async () => {
  const payloads = [];
  const { controller } = makeController({
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{
          name: 'AppAuthToken',
          value: 'secret-token',
          domain: '.write.qq.com',
          path: '/',
          secure: true,
          httpOnly: true,
          sameSite: 'Lax',
          expirationDate: 12345,
          storeId: 'profile-1',
          session: false,
        }, {
          name: 'pubtoken',
          value: 'secret-cookie',
          domain: '.write.qq.com',
          path: '/',
          secure: true,
          httpOnly: true,
          sameSite: 'Lax',
          expirationDate: 12345,
          storeId: 'profile-1',
          session: false,
        }]
        : []
    ),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async (payload) => {
        payloads.push(payload);
        return { ok: true, cookie_count: 1 };
      },
    },
  });

  await controller.syncConnectedSessionsToBackend();

  assert.equal(payloads.length, 1);
  assert.deepEqual(Object.keys(payloads[0].cookies[0]).sort(), [
    'domain',
    'expirationDate',
    'httpOnly',
    'name',
    'path',
    'sameSite',
    'secure',
    'value',
  ]);
  assert.equal('storeId' in payloads[0].cookies[0], false);
  assert.equal('session' in payloads[0].cookies[0], false);
});
