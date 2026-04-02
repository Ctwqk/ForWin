import { createBackendClient } from './lib/backend-client.js';
import { BRIDGE_CHANNEL, PLATFORM_AGENT_CHANNEL } from './lib/channels.js';
import { PublisherExtensionController } from './lib/controller.js';
import { PLATFORM_ADAPTERS, getPlatformAdapter } from './lib/platforms.js';
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

async function runUploadCommand(tabId, payload) {
  if (!tabId) {
    return { ok: false, error: '未能打开上传页面。' };
  }
  let ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 6000);
  let attempt = 0;
  while (attempt < 8) {
    attempt += 1;
    try {
      if (!ready && attempt === 1) {
        return { ok: false, error: '平台页面初始化超时，无法执行上传。' };
      }
      const response = await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
        channel: PLATFORM_AGENT_CHANNEL,
        action: 'run-upload',
        payload,
      });
      if (response) {
        if (!response.ok && response.errorCode === 'editor-navigation-pending') {
          ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 8000);
          if (!ready) {
            return { ok: false, error: '平台页面跳转超时，未能进入章节编辑页。' };
          }
          continue;
        }
        return response;
      }
    } catch (_error) {
      ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 2500);
    }
  }
  return { ok: false, error: '平台页面没有准备好，无法执行上传。' };
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
      const settings = await getSettings();
      return createBackendClient(globalThis.fetch.bind(globalThis), settings).heartbeat(payload);
    },
    async getUploadJob(jobId) {
      const settings = await getSettings();
      return createBackendClient(globalThis.fetch.bind(globalThis), settings).getUploadJob(jobId);
    },
    async updateUploadJobResult(jobId, payload) {
      const settings = await getSettings();
      return createBackendClient(globalThis.fetch.bind(globalThis), settings).updateUploadJobResult(jobId, payload);
    },
    async claimNextUploadJob(payload) {
      const settings = await getSettings();
      return createBackendClient(globalThis.fetch.bind(globalThis), settings).claimNextUploadJob(payload);
    },
    async syncBrowserSession(payload) {
      const settings = await getSettings();
      return createBackendClient(globalThis.fetch.bind(globalThis), settings).syncBrowserSession(payload);
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
  refreshContentBridge: async () => {},
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
