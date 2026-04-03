import { createBackendClient } from './lib/backend-client.js';
import { BRIDGE_CHANNEL, PLATFORM_AGENT_CHANNEL } from './lib/channels.js';
import { PublisherExtensionController } from './lib/controller.js';
import { getPlatformAdapter } from './lib/platforms.js';
import { DEFAULT_SETTINGS, normalizeSettings } from './lib/settings.js';
import { READY_CHANNELS, TabReadyRegistry } from './lib/tab-ready-registry.js';
import {
  extensionApi,
  reportBackgroundError,
  wrapCall,
} from './lib/extension-runtime.js';

const SETTINGS_KEY = 'forwinPublisherSettings';
const CLIENT_ID_KEY = 'forwinPublisherClientId';
const PLATFORM_STATE_KEY = 'forwinPublisherPlatformStates';
const HEARTBEAT_ALARM = 'forwinPublisherHeartbeat';
const tabReadyRegistry = new TabReadyRegistry();

function randomId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `forwin-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function getStorageValue(key, fallbackValue) {
  const result = await wrapCall(extensionApi.storage.local, 'get', key);
  return result?.[key] ?? fallbackValue;
}

async function setStorageValue(key, value) {
  await wrapCall(extensionApi.storage.local, 'set', { [key]: value });
}

async function ensureClientId() {
  let clientId = await getStorageValue(CLIENT_ID_KEY, '');
  if (!clientId) {
    clientId = randomId();
    await setStorageValue(CLIENT_ID_KEY, clientId);
  }
  return clientId;
}

async function getSettings() {
  return normalizeSettings(await getStorageValue(SETTINGS_KEY, DEFAULT_SETTINGS));
}

async function setSettings(settings) {
  await setStorageValue(SETTINGS_KEY, normalizeSettings(settings));
}

async function getPlatformStates() {
  return await getStorageValue(PLATFORM_STATE_KEY, {});
}

async function getPlatformState(platformId) {
  const states = await getPlatformStates();
  return states?.[platformId] || {};
}

async function setPlatformState(platformId, value) {
  const states = await getPlatformStates();
  states[platformId] = {
    ...(states[platformId] || {}),
    ...value,
  };
  await setStorageValue(PLATFORM_STATE_KEY, states);
}

async function getCookies(platformId) {
  const adapter = getPlatformAdapter(platformId);
  const cookies = [];
  for (const url of adapter.cookieUrls) {
    const rows = await wrapCall(extensionApi.cookies, 'getAll', { url });
    cookies.push(...(rows || []));
  }
  return cookies;
}

async function openLoginPopup(url) {
  const created = await wrapCall(extensionApi.windows, 'create', {
    url,
    type: 'popup',
    width: 760,
    height: 920,
  });
  const tab = created?.tabs?.[0];
  return {
    windowId: created?.id || 0,
    tabId: tab?.id || 0,
  };
}

async function openUploadTab(url) {
  const tab = await wrapCall(extensionApi.tabs, 'create', {
    url,
    active: true,
  });
  return { tabId: tab?.id || 0 };
}

async function queryTabs(queryInfo = {}) {
  return wrapCall(extensionApi.tabs, 'query', queryInfo);
}

async function getTab(tabId) {
  if (!tabId) {
    return null;
  }
  try {
    return await wrapCall(extensionApi.tabs, 'get', tabId);
  } catch (_error) {
    return null;
  }
}

async function navigateTab(tabId, url) {
  if (!tabId || !url) {
    return null;
  }
  return wrapCall(extensionApi.tabs, 'update', tabId, { url });
}

async function closePopup(windowId) {
  if (!windowId) {
    return;
  }
  try {
    await wrapCall(extensionApi.windows, 'remove', windowId);
  } catch (_error) {
    // Ignore already-closed windows.
  }
}

async function notifyPage(tabId, eventName, payload) {
  if (!tabId) {
    return;
  }
  try {
    await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: BRIDGE_CHANNEL,
      kind: 'event',
      event: eventName,
      payload,
    });
  } catch (_error) {
    // Page may be gone or not bridged yet.
  }
}

async function openOptionsPage() {
  await wrapCall(extensionApi.runtime, 'openOptionsPage');
}

function getExtensionVersion() {
  return extensionApi.runtime.getManifest().version;
}

async function getBrowserInfo() {
  const userAgent = globalThis.navigator?.userAgent || '';
  let browserName = 'Chromium';
  let browserVersion = '';
  const firefoxMatch = userAgent.match(/Firefox\/([^\s]+)/);
  if (firefoxMatch) {
    browserName = 'Firefox';
    browserVersion = firefoxMatch[1];
  } else {
    const chromeMatch = userAgent.match(/Chrome\/([^\s]+)/);
    if (chromeMatch) {
      browserName = 'Chrome';
      browserVersion = chromeMatch[1];
    }
  }
  return { browserName, browserVersion };
}

async function withBackendClient(callback) {
  const settings = await getSettings();
  const client = createBackendClient(globalThis.fetch.bind(globalThis), settings);
  return callback(client);
}

async function inspectFanqieEditorState(tabId) {
  if (!tabId) {
    return { ok: false, wordCount: 0, trustedBodyTarget: null, currentUrl: '' };
  }
  const ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 5000);
  if (!ready) {
    return { ok: false, wordCount: 0, trustedBodyTarget: null, currentUrl: '' };
  }
  try {
    return await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action: 'inspect-fanqie-editor-state',
    }) || { ok: false, wordCount: 0, trustedBodyTarget: null, currentUrl: '' };
  } catch (_error) {
    return { ok: false, wordCount: 0, trustedBodyTarget: null, currentUrl: '' };
  }
}

async function sleep(ms) {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

async function attachDebugger(tabId) {
  if (!extensionApi.debugger?.attach) {
    throw new Error('当前浏览器不支持扩展调试协议。');
  }
  try {
    await wrapCall(extensionApi.debugger, 'attach', { tabId }, '1.3');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!message.includes('already attached') && !message.includes('Another debugger')) {
      throw error;
    }
  }
}

async function detachDebugger(tabId) {
  if (!extensionApi.debugger?.detach) {
    return;
  }
  try {
    await wrapCall(extensionApi.debugger, 'detach', { tabId });
  } catch (_error) {
    // Ignore detach errors when nothing was attached.
  }
}

async function sendDebuggerCommand(tabId, method, commandParams = {}) {
  return wrapCall(extensionApi.debugger, 'sendCommand', { tabId }, method, commandParams);
}

async function trustedClick(tabId, x, y) {
  await sendDebuggerCommand(tabId, 'Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x,
    y,
    button: 'none',
    buttons: 0,
    clickCount: 0,
  });
  await sendDebuggerCommand(tabId, 'Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x,
    y,
    button: 'left',
    buttons: 1,
    clickCount: 1,
  });
  await sendDebuggerCommand(tabId, 'Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x,
    y,
    button: 'left',
    buttons: 0,
    clickCount: 1,
  });
}

async function trustedInsertText(tabId, text) {
  await sendDebuggerCommand(tabId, 'Input.insertText', { text });
}

async function applyTrustedFanqieBodyInput(tabId, body, target) {
  if (!target?.x || !target?.y) {
    throw new Error('未能定位番茄正文编辑器。');
  }
  await attachDebugger(tabId);
  try {
    await trustedClick(tabId, target.x, target.y);
    await sleep(250);
    try {
      const applied = await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
        channel: PLATFORM_AGENT_CHANNEL,
        action: 'apply-fanqie-trusted-body',
        payload: { body: String(body || '') },
      });
      if (applied?.ok) {
        await sleep(800);
        return;
      }
    } catch (_error) {
      // Fall through to the debugger text path below.
    }
    await trustedInsertText(tabId, String(body || ''));
    await sleep(800);
  } finally {
    await detachDebugger(tabId);
  }
}

async function applyTrustedQidianPublishConfirm(tabId, target) {
  if (!target?.x || !target?.y) {
    throw new Error('未能定位起点确认发布按钮。');
  }
  await attachDebugger(tabId);
  try {
    await trustedClick(tabId, target.x, target.y);
    await sleep(1200);
  } finally {
    await detachDebugger(tabId);
  }
}

async function runUploadCommand(tabId, payload) {
  if (!tabId) {
    return { ok: false, error: '未能打开上传页面。' };
  }
  let activeTabId = tabId;
  let readyState = await waitForRunnablePlatformTab(payload.platform, activeTabId, 8000);
  let ready = Boolean(readyState?.ready);
  let attempt = 0;
  let lastError = '';
  while (attempt < 12) {
    attempt += 1;
    try {
      if (!ready) {
        readyState = await waitForRunnablePlatformTab(
          payload.platform,
          activeTabId,
          attempt === 1 ? 4000 : 3000,
        );
        if (!readyState) {
          await sleep(500);
          continue;
        }
        ready = Boolean(readyState.ready);
        if (!ready) {
          await sleep(1200);
        }
      }
      const response = await wrapCall(extensionApi.tabs, 'sendMessage', activeTabId, {
        channel: PLATFORM_AGENT_CHANNEL,
        action: 'run-upload',
        payload,
      });
      if (response) {
        if (!response.ok && response.errorCode === 'trusted-body-input-required') {
          await applyTrustedFanqieBodyInput(activeTabId, payload.body, response.trustedBodyTarget);
          await sleep(1200);
          await inspectFanqieEditorState(activeTabId);
          ready = await tabReadyRegistry.waitFor(activeTabId, READY_CHANNELS.PLATFORM_AGENT, 1500);
          payload = {
            ...payload,
            trustedBodyDone: true,
          };
          continue;
        }
        if (!response.ok && response.errorCode === 'trusted-qidian-confirm-required') {
          await applyTrustedQidianPublishConfirm(activeTabId, response.trustedConfirmTarget);
          await sleep(1200);
          ready = await tabReadyRegistry.waitFor(activeTabId, READY_CHANNELS.PLATFORM_AGENT, 1500);
          payload = {
            ...payload,
            trustedPublishDone: true,
          };
          continue;
        }
        if (!response.ok && response.errorCode === 'editor-navigation-pending') {
          const redirectedTabId = await waitForUploadEditorTab(payload.platform, activeTabId, 15000);
          if (redirectedTabId) {
            activeTabId = redirectedTabId;
          }
          readyState = await waitForRunnablePlatformTab(payload.platform, activeTabId, 12000);
          ready = Boolean(readyState?.ready);
          if (!readyState) {
            return { ok: false, error: '平台页面跳转超时，未能进入章节编辑页。', errorCode: 'chapter-editor-navigation-failed' };
          }
          await sleep(5000);
          ready = true;
          continue;
        }
        if (!response.ok && response.errorCode === 'create-book-page-pending') {
          const workflowTabId = await waitForPlatformWorkflowTab(payload.platform, activeTabId, 15000);
          if (workflowTabId) {
            activeTabId = workflowTabId;
          }
          const runnable = await waitForRunnableWorkflowTab(payload.platform, activeTabId, 12000);
          ready = Boolean(runnable?.ready);
          if (!runnable) {
            return { ok: false, error: '起点创建作品页跳转超时。', errorCode: 'chapter-editor-navigation-failed' };
          }
          await sleep(5000);
          ready = true;
          continue;
        }
        return response;
      }
    } catch (_error) {
      lastError = _error instanceof Error ? _error.message : String(_error || '');
      const currentTab = await getTab(activeTabId);
      const currentUrl = String(currentTab?.url || '');
      if (isReceivingEndError(lastError) && isPlatformWorkflowUrl(payload.platform, currentUrl)) {
        const runnable = await waitForRunnableWorkflowTab(payload.platform, activeTabId, 5000);
        if (runnable) {
          ready = Boolean(runnable.ready);
          await sleep(800);
          continue;
        }
      }
      const workflowTabId = await waitForPlatformWorkflowTab(payload.platform, activeTabId, 2500);
      if (workflowTabId) {
        activeTabId = workflowTabId;
      }
      const redirectedTabId = await waitForUploadEditorTab(payload.platform, activeTabId, 2500);
      if (redirectedTabId) {
        activeTabId = redirectedTabId;
      }
      readyState = await waitForRunnablePlatformTab(payload.platform, activeTabId, 4000);
      ready = Boolean(readyState?.ready);
    }
  }
  const currentTab = await getTab(activeTabId);
  return {
    ok: false,
    error: lastError || '平台页面没有准备好，无法执行上传。',
    currentUrl: String(currentTab?.url || ''),
  };
}

function isUploadEditorUrl(platformId, url = '') {
  if (!url) {
    return false;
  }
  if (platformId === 'fanqie') {
    return url.includes('fanqienovel.com') && url.includes('/publish/');
  }
  if (platformId === 'qidian') {
    return url.includes('write.qq.com') && (
      url.includes('/chaptertmp/')
      || url.includes('/portal/booknovels/chaptertmp/')
    );
  }
  return false;
}

function isPlatformWorkflowUrl(platformId, url = '') {
  if (!url) {
    return false;
  }
  if (platformId === 'fanqie') {
    return url.includes('fanqienovel.com') && (
      url.includes('/main/writer/')
      || url.includes('/publish/')
      || url.includes('/main/writer/create')
    );
  }
  if (platformId === 'qidian') {
    return url.includes('write.qq.com') && (
      url.includes('/portal/dashboard')
      || url.includes('/create-novel')
      || url.includes('/chaptertmp/')
      || url.includes('/portal/booknovels/chaptertmp/')
    );
  }
  return false;
}

async function waitForUploadEditorTab(platformId, currentTabId, timeoutMs = 8000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const tabs = await queryTabs({}) || [];
    const candidates = tabs
      .filter((tab) => isUploadEditorUrl(platformId, String(tab?.url || '')))
      .sort((left, right) => {
        if ((left?.id || 0) === currentTabId) {
          return 1;
        }
        if ((right?.id || 0) === currentTabId) {
          return -1;
        }
        return (right?.id || 0) - (left?.id || 0);
      });
    for (const candidate of candidates) {
      const candidateTabId = candidate?.id || 0;
      if (!candidateTabId) {
        continue;
      }
      const candidateStatus = String(candidate?.status || '');
      const candidateReady = tabReadyRegistry.isReady(candidateTabId, READY_CHANNELS.PLATFORM_AGENT)
        || await tabReadyRegistry.waitFor(candidateTabId, READY_CHANNELS.PLATFORM_AGENT, 1200);
      if (candidateReady || candidateStatus === 'complete') {
        return candidateTabId;
      }
    }
    await new Promise((resolve) => globalThis.setTimeout(resolve, 250));
  }
  return 0;
}

async function waitForPlatformWorkflowTab(platformId, currentTabId, timeoutMs = 8000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const tabs = await queryTabs({}) || [];
    const candidates = tabs
      .filter((tab) => isPlatformWorkflowUrl(platformId, String(tab?.url || '')))
      .sort((left, right) => {
        if ((left?.id || 0) === currentTabId) {
          return 1;
        }
        if ((right?.id || 0) === currentTabId) {
          return -1;
        }
        return (right?.id || 0) - (left?.id || 0);
      });
    for (const candidate of candidates) {
      const candidateTabId = candidate?.id || 0;
      if (!candidateTabId) {
        continue;
      }
      const candidateStatus = String(candidate?.status || '');
      const candidateReady = tabReadyRegistry.isReady(candidateTabId, READY_CHANNELS.PLATFORM_AGENT)
        || await tabReadyRegistry.waitFor(candidateTabId, READY_CHANNELS.PLATFORM_AGENT, 1200);
      if (candidateReady || candidateStatus === 'complete') {
        return candidateTabId;
      }
    }
    await new Promise((resolve) => globalThis.setTimeout(resolve, 250));
  }
  return 0;
}

function isReceivingEndError(message = '') {
  return String(message || '').includes('Receiving end does not exist');
}

async function waitForRunnableWorkflowTab(platformId, tabId, timeoutMs = 8000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const tab = await getTab(tabId);
    const url = String(tab?.url || '');
    if (isPlatformWorkflowUrl(platformId, url)) {
      const ready = tabReadyRegistry.isReady(tabId, READY_CHANNELS.PLATFORM_AGENT)
        || await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 800);
      if (ready || tab?.status === 'complete') {
        return { tabId, url, ready };
      }
    }
    await sleep(400);
  }
  return null;
}

async function waitForRunnablePlatformTab(platformId, tabId, timeoutMs = 8000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const tab = await getTab(tabId);
    const url = String(tab?.url || '');
    if (isPlatformWorkflowUrl(platformId, url) || isUploadEditorUrl(platformId, url)) {
      const ready = tabReadyRegistry.isReady(tabId, READY_CHANNELS.PLATFORM_AGENT)
        || await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 800);
      if (ready || tab?.status === 'complete') {
        return { tabId, url, ready };
      }
    }
    await sleep(400);
  }
  return null;
}

async function inspectLoginState(tabId) {
  if (!tabId) {
    return { ok: false, authenticated: false, loginVisible: false, currentUrl: '' };
  }
  let ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 5000);
  let attempt = 0;
  while (attempt < 8) {
    attempt += 1;
    try {
      if (!ready && attempt === 1) {
        return { ok: false, authenticated: false, loginVisible: false, currentUrl: '' };
      }
      const response = await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
        channel: PLATFORM_AGENT_CHANNEL,
        action: 'inspect-login-state',
      });
      if (response) {
        return response;
      }
    } catch (_error) {
      ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 2000);
    }
  }
  return { ok: false, authenticated: false, loginVisible: false, currentUrl: '' };
}

async function ensureHeartbeatAlarm() {
  const existing = await wrapCall(extensionApi.alarms, 'get', HEARTBEAT_ALARM);
  if (existing?.periodInMinutes === 1) {
    return;
  }
  await wrapCall(extensionApi.alarms, 'create', HEARTBEAT_ALARM, { periodInMinutes: 1 });
}

const controller = new PublisherExtensionController({
  backend: {
    async heartbeat(payload) {
      return withBackendClient((client) => client.heartbeat(payload));
    },
    async getUploadJob(jobId) {
      return withBackendClient((client) => client.getUploadJob(jobId));
    },
    async updateUploadJobResult(jobId, payload) {
      return withBackendClient((client) => client.updateUploadJobResult(jobId, payload));
    },
    async claimNextUploadJob(payload) {
      return withBackendClient((client) => client.claimNextUploadJob(payload));
    },
    async syncBrowserSession(payload) {
      return withBackendClient((client) => client.syncBrowserSession(payload));
    },
  },
  ensureClientId,
  getClientId: ensureClientId,
  getSettings,
  setSettings,
  getPlatformState,
  setPlatformState,
  getCookies,
  openLoginPopup,
  openUploadTab,
  getTab,
  navigateTab,
  closePopup,
  notifyPage,
  openOptionsPage,
  getExtensionVersion,
  getBrowserInfo,
  runUploadCommand,
  inspectLoginState,
  ensureHeartbeatAlarm,
});

extensionApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const action = String(message?.action || '').trim();
  if (action === 'content-bridge-ready') {
    if (sender?.tab?.id) {
      tabReadyRegistry.markReady(sender.tab.id, READY_CHANNELS.CONTENT_BRIDGE);
    }
    sendResponse({ ok: true, payload: { ready: true } });
    return false;
  }
  if (action === 'platform-agent-ready') {
    if (sender?.tab?.id) {
      tabReadyRegistry.markReady(sender.tab.id, READY_CHANNELS.PLATFORM_AGENT);
    }
    sendResponse({ ok: true, payload: { ready: true } });
    return false;
  }
  controller.handleMessage(message, sender)
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((error) => {
      sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    });
  return true;
});

extensionApi.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo?.status === 'loading' || changeInfo?.url) {
    tabReadyRegistry.reset(tabId, READY_CHANNELS.CONTENT_BRIDGE);
    tabReadyRegistry.reset(tabId, READY_CHANNELS.PLATFORM_AGENT);
  }
  controller.handleTabUpdated(tabId, changeInfo, tab).catch((error) => {
    reportBackgroundError('handleTabUpdated', error);
  });
});

extensionApi.tabs.onRemoved.addListener((tabId) => {
  tabReadyRegistry.reset(tabId);
  controller.handleTabRemoved(tabId).catch((error) => {
    reportBackgroundError('handleTabRemoved', error);
  });
});

extensionApi.cookies.onChanged.addListener(() => {
  controller.handleCookieChanged().catch((error) => {
    reportBackgroundError('handleCookieChanged', error);
  });
});

extensionApi.alarms.onAlarm.addListener((alarm) => {
  if (alarm?.name === HEARTBEAT_ALARM) {
    controller.sendHeartbeat()
      .then(() => controller.dispatchPendingUploadJobs())
      .catch((error) => {
        reportBackgroundError('heartbeat alarm', error);
      });
  }
});

if (extensionApi.runtime.onInstalled) {
  extensionApi.runtime.onInstalled.addListener(() => {
    controller.bootstrap().catch((error) => {
      reportBackgroundError('onInstalled bootstrap', error);
    });
  });
}

if (extensionApi.runtime.onStartup) {
  extensionApi.runtime.onStartup.addListener(() => {
    controller.bootstrap().catch((error) => {
      reportBackgroundError('onStartup bootstrap', error);
    });
  });
}

controller.bootstrap().catch((error) => {
  reportBackgroundError('initial bootstrap', error);
});
