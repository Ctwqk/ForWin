import test from 'node:test';
import assert from 'node:assert/strict';

import { PublisherExtensionController } from '../lib/controller.js';

function makeController(overrides = {}) {
  const events = [];
  const uploadResults = [];
  const loginQrNotifications = [];
  const loginQrStatusEvents = [];
  const closedTabs = [];
  const restoredCookies = [];
  const deps = {
    ensureClientId: async () => 'client-1',
    getClientId: async () => 'client-1',
    ensureHeartbeatAlarm: async () => {},
    getSettings: async () => ({ backendBaseUrl: 'http://192.168.31.10:8899', apiKey: 'secret', syncSessionToBackend: true }),
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
    runCommentSyncCommand: async () => ({
      ok: true,
      currentUrl: 'https://fanqienovel.com/main/writer/',
      message: '评论同步已完成。',
      comments: [],
      resultPayload: { source: 'fanqie-author-api' },
    }),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      getBrowserSession: async () => null,
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'qidian',
        display_name: '起点小说',
        book_name: '测试书',
        chapter_title: '第一章',
        body: '正文',
        upload_url: null,
        publish: true,
      }),
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async (payload) => {
        loginQrNotifications.push(payload);
        return { ok: true, dispatched: true };
      },
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
    recordLoginQrNotification: async (event) => {
      loginQrStatusEvents.push(event);
    },
    ...overrides,
  };
  return {
    controller: new PublisherExtensionController(deps),
    events,
    uploadResults,
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'qidian',
        display_name: '起点小说',
        book_name: '测试书',
        chapter_title: '第一章',
        body: '正文',
        upload_url: null,
        publish: true,
      }),
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async () => {
        throw new Error('network down');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
  await controller.sendHeartbeat();

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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('not used');
      },
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      updateUploadJobResult: async () => ({ ok: true }),
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  await controller.bootstrap();

  assert.equal(restoredCookies.length, 1);
  assert.equal(restoredCookies[0].platformId, 'fanqie');
  assert.equal(restoredCookies[0].cookies.length, 2);
  assert.equal(heartbeatCalls.length, 1);
});

test('controller marks upload job running then succeeded', async () => {
  const { controller, uploadResults } = makeController();

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(uploadResults[0].status, 'running');
  assert.equal(uploadResults.at(-1).status, 'succeeded');
});

test('controller gives qidian draft upload a longer execution timeout', async () => {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const delays = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    delays.push(Number(delay || 0));
    return originalSetTimeout(callback, delay, ...args);
  };
  globalThis.clearTimeout = (timer) => originalClearTimeout(timer);
  try {
    const { controller } = makeController({
      backend: {
        heartbeat: async () => ({ ok: true }),
        syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
        getBrowserSession: async () => null,
        claimNextUploadJob: async () => ({ found: false, job: null }),
        claimNextCommentSyncJob: async () => ({ found: false, job: null }),
        getUploadJob: async () => ({
          job_id: 'job-qidian-draft',
          platform: 'qidian',
          display_name: '起点小说',
          book_name: '测试书',
          chapter_title: '第一章',
          body: '正文',
          upload_url: null,
          publish: false,
        }),
        syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
        updateUploadJobResult: async () => ({ ok: true }),
        updateCommentSyncJobResult: async () => ({ ok: true }),
      },
    });

    await controller.handleMessage(
      { action: 'execute-upload-job', payload: { jobId: 'job-qidian-draft' } },
      { tab: { id: 100 } },
    );
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }

  assert.ok(Math.max(...delays) >= 420000);
});

test('controller recovers qidian draft timeout when a real ccid draft url exists', async () => {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const timeoutSentinel = { qidianTimeout: true };
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (Number(delay || 0) >= 420000) {
      callback(...args);
      return timeoutSentinel;
    }
    return originalSetTimeout(callback, delay, ...args);
  };
  globalThis.clearTimeout = (timer) => {
    if (timer === timeoutSentinel) {
      return;
    }
    return originalClearTimeout(timer);
  };
  try {
    const { controller, uploadResults } = makeController({
      getTab: async (tabId) => ({
        id: tabId,
        status: 'complete',
        url: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/35512915704247809?entry=publish#ccid=96252466911310489',
      }),
      runUploadCommand: async () => new Promise(() => {}),
      backend: {
        heartbeat: async () => ({ ok: true }),
        syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
        getBrowserSession: async () => null,
        claimNextUploadJob: async () => ({ found: false, job: null }),
        claimNextCommentSyncJob: async () => ({ found: false, job: null }),
        getUploadJob: async () => ({
          job_id: 'job-qidian-timeout-recover',
          platform: 'qidian',
          display_name: '起点小说',
          book_name: '测试书',
          chapter_title: '第一章',
          body: '正文',
          upload_url: null,
          publish: false,
        }),
        syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
        updateUploadJobResult: async (_jobId, payload) => {
          uploadResults.push(payload);
          return { ok: true };
        },
        updateCommentSyncJobResult: async () => ({ ok: true }),
      },
    });

    await controller.handleMessage(
      { action: 'execute-upload-job', payload: { jobId: 'job-qidian-timeout-recover' } },
      { tab: { id: 100 } },
    );

    assert.equal(uploadResults.at(-1).status, 'succeeded');
    assert.equal(uploadResults.at(-1).error, '');
    assert.equal(uploadResults.at(-1).result_payload.verified_via, 'qidian-real-ccid-timeout-recovery');
    assert.equal(uploadResults.at(-1).result_payload.error_code, undefined);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

test('controller cancels upload job before execution when abort was requested', async () => {
  const { controller, uploadResults } = makeController({
    runUploadCommand: async () => {
      throw new Error('should not execute aborted upload');
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      getBrowserSession: async () => null,
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'qidian',
        display_name: '起点小说',
        status: 'terminating',
        abort_requested: true,
        book_name: '测试书',
        chapter_title: '第一章',
        body: '正文',
        upload_url: null,
        publish: true,
      }),
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(uploadResults.at(-1).status, 'cancelled');
  assert.equal(uploadResults.at(-1).result_payload.phase, 'abort-before-start');
});

test('controller closes execution tabs after successful upload', async () => {
  let controllerRef = null;
  const { controller, closedTabs, uploadResults } = makeController({
    runUploadCommand: async (_tabId, payload) => {
      await controllerRef.handleTabCreated({ id: 88, openerTabId: 77 });
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/dashboard',
        message: `章节发布动作已提交：${payload.chapter_title}`,
        resultPayload: { mode: 'publish' },
      };
    },
  });
  controllerRef = controller;

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.deepEqual(closedTabs.sort((a, b) => a - b), [77, 88]);
  assert.equal(uploadResults.at(-1).result_payload.tab_cleanup.closed_tab_ids.length, 2);
});

test('controller keeps execution tabs open when upload fails', async () => {
  let controllerRef = null;
  const { controller, closedTabs, uploadResults } = makeController({
    runUploadCommand: async () => {
      await controllerRef.handleTabCreated({ id: 99, openerTabId: 77 });
      return {
        ok: false,
        currentUrl: 'https://write.qq.com/portal/login',
        message: '上传失败。',
        error: '需要重新登录',
        resultPayload: { mode: 'publish' },
      };
    },
  });
  controllerRef = controller;

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.deepEqual(closedTabs, []);
  assert.equal(uploadResults.at(-1).status, 'failed');
  assert.equal(uploadResults.at(-1).result_payload.tab_cleanup.attempted, false);
});

test('controller forwards create-if-missing book metadata to upload command', async () => {
  const payloads = [];
  const { controller } = makeController({
    getTab: async () => ({ id: 77, url: 'https://fanqienovel.com/main/writer/' }),
    runUploadCommand: async (_tabId, payload) => {
      payloads.push(payload);
      return {
        ok: true,
        currentUrl: 'https://fanqienovel.com/main/writer/',
        message: '章节发布动作已提交。',
        resultPayload: { mode: 'publish' },
      };
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'fanqie',
        display_name: '番茄小说',
        book_name: '旧巷春灯',
        chapter_title: '第一章 雨巷来信',
        body: '正文',
        upload_url: null,
        publish: true,
        result_payload: {
          create_if_missing: true,
          book_meta: {
            audience: 'male',
            primary_category: '都市日常',
            protagonist_names: ['韩砚', '林雾'],
            intro: '一段关于旧城、旧案和失踪真相的故事。',
          },
        },
      }),
      updateUploadJobResult: async () => ({ ok: true }),
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].create_if_missing, true);
  assert.equal(payloads[0].book_meta.primary_category, '都市日常');
  assert.deepEqual(payloads[0].book_meta.protagonist_names, ['韩砚', '林雾']);
});

test('controller dispatches cover upload task kind to cover command', async () => {
  const payloads = [];
  const { controller, uploadResults } = makeController({
    getTab: async () => ({ id: 77, url: 'https://write.qq.com/portal/book/123' }),
    runCoverUploadCommand: async (_tabId, payload) => {
      payloads.push(payload);
      return {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/book/123',
        message: '封面上传动作已提交。',
        resultPayload: { cover_state: 'under_review' },
      };
    },
    runUploadCommand: async () => {
      throw new Error('chapter upload command should not run for cover_upload');
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'cover-job-1',
        task_kind: 'cover_upload',
        platform: 'qidian',
        display_name: '起点小说',
        book_name: '测试书',
        upload_url: null,
        result_payload: {
          work_binding_id: 'work-1',
          remote_book_id: 'book-1',
          remote_url: 'https://write.qq.com/portal/book/123',
          cover_asset_id: 'cover-1',
          file_path: '/tmp/cover.png',
        },
      }),
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'cover-job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].file_path, '/tmp/cover.png');
  assert.equal(payloads[0].cover_asset_id, 'cover-1');
  assert.equal(uploadResults.at(-1).status, 'succeeded');
  assert.equal(uploadResults.at(-1).result_payload.task_kind, 'cover_upload');
  assert.equal(uploadResults.at(-1).result_payload.cover_state, 'under_review');
});

test('controller dispatches audit sync task kind to audit sync command', async () => {
  const payloads = [];
  const { controller, uploadResults } = makeController({
    getTab: async () => ({ id: 77, url: 'https://fanqienovel.com/main/writer/book-info/456' }),
    runAuditSyncCommand: async (_tabId, payload) => {
      payloads.push(payload);
      return {
        ok: true,
        currentUrl: 'https://fanqienovel.com/main/writer/book-info/456',
        message: '审核状态已同步。',
        resultPayload: {
          work: { remote_book_id: 'book-456', audit_state: 'under_review' },
          chapters: [{ chapter_number: 1, audit_state: 'under_review' }],
          cover: { cover_state: 'under_review' },
        },
      };
    },
    runUploadCommand: async () => {
      throw new Error('chapter upload command should not run for audit_sync');
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'audit-job-1',
        task_kind: 'audit_sync',
        platform: 'fanqie',
        display_name: '番茄小说',
        book_name: '测试书',
        upload_url: null,
        result_payload: {
          work_binding_id: 'work-456',
          remote_book_id: 'book-456',
          remote_url: 'https://fanqienovel.com/main/writer/book-info/456',
        },
      }),
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'audit-job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].remote_book_id, 'book-456');
  assert.equal(uploadResults.at(-1).status, 'succeeded');
  assert.equal(uploadResults.at(-1).result_payload.task_kind, 'audit_sync');
  assert.equal(uploadResults.at(-1).result_payload.work.audit_state, 'under_review');
});

test('controller auto-dispatches claimed upload job for connected platform', async () => {
  let claimedOnce = false;
  const { controller, uploadResults } = makeController({
    getPlatformState: async (platformId) => (platformId === 'qidian' ? { connected: true } : {}),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => {
        if (claimedOnce) {
          return { found: false, job: null };
        }
        claimedOnce = true;
        return {
          found: true,
          job: {
            job_id: 'job-auto',
            platform: 'qidian',
            display_name: '起点小说',
            status: 'running',
            book_name: '测试书',
            chapter_title: '第一章',
            body: '正文',
            upload_url: null,
            publish: false,
          },
        };
      },
      getUploadJob: async () => {
        throw new Error('should not fetch job again');
      },
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.dispatchPendingUploadJobs();

  assert.equal(uploadResults.at(-1).status, 'succeeded');
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
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
      updateUploadJobResult: async () => ({ ok: true }),
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

test('controller resumes an already running claimed upload job for the same client', async () => {
  let claimedOnce = false;
  const { controller, uploadResults } = makeController({
    getTab: async () => ({ id: 77, url: 'https://fanqienovel.com/main/writer/' }),
    getPlatformState: async (platformId) => (platformId === 'fanqie' ? { connected: true } : {}),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => {
        if (claimedOnce) {
          return { found: false, job: null };
        }
        claimedOnce = true;
        return {
          found: true,
          job: {
            job_id: 'job-running',
            platform: 'fanqie',
            display_name: '番茄小说',
            status: 'running',
            book_name: '我的一本书dasdgasdf',
            chapter_title: '江潮入夜',
            body: '正文',
            upload_url: null,
            publish: false,
          },
        };
      },
      getUploadJob: async () => {
        throw new Error('should not fetch job again');
      },
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.dispatchPendingUploadJobs();

  assert.equal(uploadResults.length, 2);
  assert.equal(uploadResults[0].status, 'running');
  assert.equal(uploadResults[0].result_payload.phase, 'opened-upload-tab');
  assert.equal(uploadResults.at(-1).status, 'succeeded');
});

test('controller records extension error code in upload job payload', async () => {
  const uploadResults = [];
  const { controller } = makeController({
    getTab: async () => ({ id: 77, url: 'https://fanqienovel.com/main/writer/' }),
    runUploadCommand: async () => ({
      ok: false,
      currentUrl: 'https://fanqienovel.com/main/writer/create',
      error: '番茄当前账号已达到当日创建作品上限。',
      errorCode: 'create-book-rate-limited',
      resultPayload: { platform_reason: 'daily-create-limit' },
    }),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'fanqie',
        display_name: '番茄小说',
        book_name: '远潮夜灯',
        chapter_title: '第一章 雨声抵港',
        body: '正文',
        upload_url: null,
        publish: true,
        result_payload: {
          create_if_missing: true,
        },
      }),
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(uploadResults.at(-1).status, 'failed');
  assert.equal(uploadResults.at(-1).result_payload.error_code, 'create-book-rate-limited');
  assert.equal(uploadResults.at(-1).result_payload.platform_reason, 'daily-create-limit');
});

test('controller retries page-not-ready upload errors before failing', async () => {
  const uploadResults = [];
  let attempts = 0;
  const { controller } = makeController({
    runUploadCommand: async () => {
      attempts += 1;
      return {
        ok: attempts >= 3,
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/35512915704247809?entry=publish',
        error: attempts >= 3 ? '' : '平台页面没有准备好，无法执行上传。',
        message: attempts >= 3 ? '章节已进入平台审核。' : '',
        resultPayload: attempts >= 3 ? { official_status: 'review-pending' } : {},
      };
    },
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => ({
        job_id: 'job-1',
        platform: 'qidian',
        display_name: '起点小说',
        book_name: '寒港夜汐',
        chapter_title: '潮声过堤',
        body: '正文',
        upload_url: null,
        publish: true,
      }),
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(attempts, 3);
  assert.equal(uploadResults.at(-1).status, 'succeeded');
  assert.equal(uploadResults.at(-1).result_payload.official_status, 'review-pending');
});

test('controller dispatch caps claimed upload jobs per pass', async () => {
  let claimed = 0;
  const { controller, uploadResults } = makeController({
    getPlatformState: async (platformId) => (platformId === 'qidian' ? { connected: true } : {}),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
      claimNextUploadJob: async () => {
        claimed += 1;
        return {
          found: true,
          job: {
            job_id: `job-${claimed}`,
            platform: 'qidian',
            display_name: '起点小说',
            status: 'running',
            book_name: '测试书',
            chapter_title: `第${claimed}章`,
            body: '正文',
            upload_url: null,
            publish: false,
          },
        };
      },
      getUploadJob: async () => {
        throw new Error('should not fetch job again');
      },
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  const result = await controller.dispatchPendingUploadJobs();

  assert.equal(result.truncated, true);
  assert.equal(result.handled, 8);
  assert.equal(claimed, 8);
  assert.equal(uploadResults.filter((item) => item.status === 'succeeded').length, 8);
});

test('controller syncs and dispatches when strong cookies exist before saved connected state flips', async () => {
  let synced = 0;
  let claimed = 0;
  const { controller, uploadResults } = makeController({
    getPlatformState: async () => ({ connected: false }),
    getCookies: async (platformId) => (
      platformId === 'qidian'
        ? [{ name: 'AppAuthToken' }, { name: 'pubtoken' }]
        : []
    ),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => {
        synced += 1;
        return { ok: true, cookie_count: 2 };
      },
      claimNextUploadJob: async () => {
        claimed += 1;
        if (claimed > 1) {
          return { found: false, job: null };
        }
        return {
          found: true,
          job: {
            job_id: 'job-cookie-signal',
            platform: 'qidian',
            display_name: '起点小说',
            status: 'running',
            book_name: '测试书',
            chapter_title: '第一章',
            body: '正文',
            upload_url: null,
            publish: false,
          },
        };
      },
      getUploadJob: async () => {
        throw new Error('should not fetch job again');
      },
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
  });

  await controller.syncConnectedSessionsToBackend();
  await controller.dispatchPendingUploadJobs();

  assert.equal(synced, 2);
  assert.equal(uploadResults.at(-1).status, 'succeeded');
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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

test('controller heartbeat suppresses concurrent login QR sends for an active login session', async () => {
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

  assert.equal(captureCalls, 1);
  assert.equal(loginQrNotifications.length, 1);
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

test('controller does not locally throttle login QR when backend does not dispatch', async () => {
  let captureCalls = 0;
  const loginQrNotifications = [];
  const throttleState = new Map();
  const { controller } = makeController({
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
        return { ok: true, dispatched: false, throttled: true, message: 'login QR notification throttled' };
      },
    },
  });

  controller.loginSessions.set(987, {
    platformId: 'qidian',
    popupTabId: 987,
    lastUrl: 'https://write.qq.com/portal/login',
  });
  await controller.sendHeartbeat();
  await controller.sendHeartbeat();

  assert.equal(captureCalls, 2);
  assert.equal(loginQrNotifications.length, 2);
  assert.equal(throttleState.size, 0);
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

test('controller heartbeat retries active login QR notification after a failed attempt', async () => {
  let notifyAttempts = 0;
  const loginQrNotifications = [];
  const { controller, loginQrStatusEvents } = makeController({
    inspectPlatformState: async (platformId) => (
      platformId === 'fanqie'
        ? {
          ok: true,
          tabId: 789,
          currentUrl: 'https://fanqienovel.com/main/writer/login',
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
    getCookies: async (platformId) => (
      platformId === 'fanqie'
        ? [{ name: 'sessionid' }, { name: 'passport_auth_status' }]
        : []
    ),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      getBrowserSession: async () => null,
      claimNextUploadJob: async () => ({ found: false, job: null }),
      claimNextCommentSyncJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      syncCommentsBatch: async () => ({ ok: true, inserted: 0, updated: 0 }),
      notifyLoginQr: async (payload) => {
        notifyAttempts += 1;
        if (notifyAttempts === 1) {
          throw new Error('network down');
        }
        loginQrNotifications.push(payload);
        return { message: 'queued', dispatched: false };
      },
      updateUploadJobResult: async () => ({ ok: true }),
      updateCommentSyncJobResult: async () => ({ ok: true }),
    },
  });

  controller.loginSessions.set(789, {
    platformId: 'fanqie',
    popupTabId: 789,
    lastUrl: 'https://fanqienovel.com/main/writer/login',
  });
  await controller.sendHeartbeat();
  await controller.sendHeartbeat();

  assert.equal(notifyAttempts, 2);
  assert.equal(loginQrNotifications.length, 1);
  assert.equal(loginQrNotifications[0].platform, 'fanqie');
  assert.equal(loginQrStatusEvents.filter((event) => event.phase === 'failed').length, 1);
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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

test('controller heartbeat does not keep sticky connected=true without current strong cookies', async () => {
  const payloads = [];
  const { controller } = makeController({
    backend: {
      heartbeat: async (payload) => {
        payloads.push(payload);
        return { ok: true };
      },
      syncBrowserSession: async () => ({ ok: true, cookie_count: 0 }),
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
      claimNextUploadJob: async () => ({ found: false, job: null }),
      getUploadJob: async () => {
        throw new Error('unused');
      },
      updateUploadJobResult: async () => ({ ok: true }),
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
