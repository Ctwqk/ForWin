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

test('controller still opens login popup when heartbeat sync fails', async () => {
  const { controller, events } = makeController({
    backend: {
      heartbeat: async () => {
        throw new TypeError('Failed to fetch');
      },
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

test('controller marks upload job running then succeeded', async () => {
  const { controller, uploadResults } = makeController();

  await controller.handleMessage(
    { action: 'execute-upload-job', payload: { jobId: 'job-1' } },
    { tab: { id: 100 } },
  );

  assert.equal(uploadResults[0].status, 'running');
  assert.equal(uploadResults.at(-1).status, 'succeeded');
});

test('controller forwards create-if-missing book metadata to upload command', async () => {
  const payloads = [];
  const { controller } = makeController({
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
            protagonist_names: ['沈砚', '林雾'],
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
  assert.deepEqual(payloads[0].book_meta.protagonist_names, ['沈砚', '林雾']);
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

test('controller records extension error code in upload job payload', async () => {
  const uploadResults = [];
  const { controller } = makeController({
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
  assert.equal(uploadResults.length, 8);
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
