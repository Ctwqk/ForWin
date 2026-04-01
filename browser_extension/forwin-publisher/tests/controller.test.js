import test from 'node:test';
import assert from 'node:assert/strict';

import { PublisherExtensionController } from '../lib/controller.js';

function makeController(overrides = {}) {
  const events = [];
  const uploadResults = [];
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
    closePopup: async () => {},
    notifyPage: async (tabId, eventName, payload) => {
      events.push({ tabId, eventName, payload });
    },
    getPlatformState: async () => ({}),
    setPlatformState: async () => {},
    runUploadCommand: async () => ({
      ok: true,
      currentUrl: 'https://write.qq.com/portal/dashboard',
      message: '章节发布动作已提交。',
      resultPayload: { mode: 'publish' },
    }),
    backend: {
      heartbeat: async () => ({ ok: true }),
      syncBrowserSession: async () => ({ ok: true, cookie_count: 3 }),
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
      updateUploadJobResult: async (_jobId, payload) => {
        uploadResults.push(payload);
        return { ok: true };
      },
    },
    ...overrides,
  };
  return { controller: new PublisherExtensionController(deps), events, uploadResults };
}

test('controller opens login popup and closes it after successful tab update', async () => {
  const { controller, events } = makeController();

  const response = await controller.handleMessage(
    { action: 'open-login', payload: { platform: 'qidian' } },
    { tab: { id: 99 } },
  );
  assert.match(response.message, /登录弹窗已打开/);

  await controller.handleTabUpdated(42, { url: 'https://write.qq.com/portal/dashboard' }, { id: 42 });

  assert.equal(events.length >= 2, true);
  assert.equal(events.at(-1).payload.connected, true);
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

  assert.equal(synced >= 1, true);
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
