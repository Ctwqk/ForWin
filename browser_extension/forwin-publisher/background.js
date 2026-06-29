import { createBackendClient } from './lib/backend-client.js';
import { BRIDGE_CHANNEL, PLATFORM_AGENT_CHANNEL } from './lib/channels.js';
import { PublisherExtensionController } from './lib/controller.js?v=0.1.31';
import { verifyFanqieDraftWithRetries } from './lib/fanqie-draft-verifier.js';
import { findLoginQrFrameTargets } from './lib/login-qr-frames.js';
import { getPlatformAdapter } from './lib/platforms.js';
import { DEFAULT_SETTINGS, getBackendOrigin, normalizeSettings } from './lib/settings.js';
import { READY_CHANNELS, TabReadyRegistry } from './lib/tab-ready-registry.js';
import { uploadMessageTimeoutMs } from './lib/upload-timeouts.js?v=0.1.23';
import {
  assertDebuggerCapability,
  extensionCapabilities,
  extensionApi,
  reportBackgroundError as logBackgroundError,
  wrapCall,
} from './lib/extension-runtime.js';

const SETTINGS_KEY = 'forwinPublisherSettings';
const CLIENT_ID_KEY = 'forwinPublisherClientId';
const PLATFORM_STATE_KEY = 'forwinPublisherPlatformStates';
const BACKGROUND_STATUS_KEY = 'forwinPublisherBackgroundStatus';
const BACKGROUND_ERRORS_KEY = 'forwinPublisherBackgroundErrors';
const LOGIN_QR_NOTIFICATIONS_KEY = 'forwinPublisherLoginQrNotifications';
const HEARTBEAT_PLATFORM_STATES_KEY = 'forwinPublisherHeartbeatPlatformStates';
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

async function getBackgroundStatus() {
  return await getStorageValue(BACKGROUND_STATUS_KEY, {});
}

async function updateBackgroundStatus(patch) {
  const current = await getBackgroundStatus();
  await setStorageValue(BACKGROUND_STATUS_KEY, {
    ...(current || {}),
    ...(patch || {}),
    updatedAt: new Date().toISOString(),
  });
}

async function appendBackgroundError(context, error) {
  const message = error instanceof Error ? error.message : String(error || '');
  if (!message) {
    return;
  }
  const entry = {
    at: new Date().toISOString(),
    context,
    message,
  };
  const existing = await getStorageValue(BACKGROUND_ERRORS_KEY, []);
  const errors = Array.isArray(existing) ? existing : [];
  await setStorageValue(BACKGROUND_ERRORS_KEY, [entry, ...errors].slice(0, 20));
  await updateBackgroundStatus({
    lastErrorAt: entry.at,
    lastErrorContext: context,
    lastErrorMessage: message,
  });
}

function safeStatusUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) {
    return '';
  }
  try {
    const parsed = new URL(raw);
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`.slice(0, 500);
  } catch (_error) {
    return raw.slice(0, 500);
  }
}

async function appendLoginQrNotificationStatus(event) {
  const entry = {
    at: String(event?.at || new Date().toISOString()),
    platform: String(event?.platform || ''),
    tab_id: Number(event?.tab_id || 0),
    current_url: safeStatusUrl(event?.current_url),
    phase: String(event?.phase || ''),
    reason: String(event?.reason || ''),
    source: String(event?.source || ''),
    image_data_url_length: Number(event?.image_data_url_length || 0),
    ok: Boolean(event?.ok),
    dispatched: Boolean(event?.dispatched),
    message: String(event?.message || '').slice(0, 500),
    error: String(event?.error || '').slice(0, 500),
  };
  const existing = await getStorageValue(LOGIN_QR_NOTIFICATIONS_KEY, []);
  const events = Array.isArray(existing) ? existing : [];
  await setStorageValue(LOGIN_QR_NOTIFICATIONS_KEY, [entry, ...events].slice(0, 20));
  await updateBackgroundStatus({
    lastLoginQrNotificationAt: entry.at,
    lastLoginQrNotificationPlatform: entry.platform,
    lastLoginQrNotificationPhase: entry.phase,
    lastLoginQrNotificationDispatched: entry.dispatched,
    lastLoginQrNotificationError: entry.error,
  });
}

async function appendHeartbeatPlatformState(event) {
  const entry = {
    at: String(event?.at || new Date().toISOString()),
    platform: String(event?.platform || ''),
    inspection_ok: Boolean(event?.inspection_ok),
    inspection_tab_id: Number(event?.inspection_tab_id || 0),
    inspection_login_visible: Boolean(event?.inspection_login_visible),
    inspection_authenticated: Boolean(event?.inspection_authenticated),
    inspection_current_url: safeStatusUrl(event?.inspection_current_url),
    raw_page_login_visible: Boolean(event?.raw_page_login_visible),
    raw_page_authenticated: Boolean(event?.raw_page_authenticated),
    raw_current_url: safeStatusUrl(event?.raw_current_url),
  };
  const existing = await getStorageValue(HEARTBEAT_PLATFORM_STATES_KEY, []);
  const events = Array.isArray(existing) ? existing : [];
  await setStorageValue(HEARTBEAT_PLATFORM_STATES_KEY, [entry, ...events].slice(0, 40));
  await updateBackgroundStatus({
    lastHeartbeatPlatformStateAt: entry.at,
    lastHeartbeatPlatformStatePlatform: entry.platform,
    lastHeartbeatPlatformStateInspectionOk: entry.inspection_ok,
    lastHeartbeatPlatformStateLoginVisible: entry.raw_page_login_visible,
  });
}

function reportBackgroundError(context, error) {
  logBackgroundError(context, error);
  appendBackgroundError(context, error).catch(() => {});
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

function normalizeSameSite(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'strict') {
    return 'strict';
  }
  if (raw === 'no_restriction' || raw === 'none') {
    return 'no_restriction';
  }
  if (raw === 'unspecified') {
    return 'unspecified';
  }
  return 'lax';
}

function cookieSetDetails(cookie) {
  const domain = String(cookie?.domain || '').trim();
  const host = domain.replace(/^\./, '');
  const path = String(cookie?.path || '/').trim() || '/';
  const secure = Boolean(cookie?.secure);
  const details = {
    url: host ? `${secure ? 'https' : 'http'}://${host}${path}` : '',
    name: String(cookie?.name || '').trim(),
    value: String(cookie?.value || ''),
    path,
    secure,
    httpOnly: Boolean(cookie?.httpOnly),
    sameSite: normalizeSameSite(cookie?.sameSite),
  };
  if (domain) {
    details.domain = domain;
  }
  const expirationDate = Number(cookie?.expirationDate);
  if (Number.isFinite(expirationDate) && expirationDate > 0) {
    details.expirationDate = expirationDate;
  }
  return details;
}

function cookieDebuggerDetails(cookie) {
  const name = String(cookie?.name || '').trim();
  const domain = String(cookie?.domain || '').trim();
  if (!name || !domain) {
    return null;
  }
  const details = {
    name,
    value: String(cookie?.value || ''),
    domain,
    path: String(cookie?.path || '/').trim() || '/',
    secure: Boolean(cookie?.secure),
    httpOnly: Boolean(cookie?.httpOnly),
  };
  const sameSite = normalizeSameSite(cookie?.sameSite);
  if (sameSite === 'strict') {
    details.sameSite = 'Strict';
  } else if (sameSite === 'no_restriction') {
    details.sameSite = 'None';
  } else {
    details.sameSite = 'Lax';
  }
  const expirationDate = Number(cookie?.expirationDate);
  if (Number.isFinite(expirationDate) && expirationDate > 0) {
    details.expires = expirationDate;
  }
  return details;
}

async function setCookiesViaDebugger(platformId, cookies = []) {
  assertDebuggerCapability(
    extensionCapabilities,
    '请在 Chromium 版扩展中执行调试协议 cookie 恢复，或退回普通 cookies API 恢复。',
  );
  const adapter = getPlatformAdapter(platformId);
  const cookieDetails = cookies
    .map((item) => cookieDebuggerDetails(item))
    .filter(Boolean);
  if (!cookieDetails.length) {
    return { applied: 0, mode: 'debugger' };
  }

  const tab = await wrapCall(extensionApi.tabs, 'create', {
    url: adapter.dashboardUrl,
    active: false,
  });
  const tabId = tab?.id || 0;
  if (!tabId) {
    throw new Error(`未能为 ${platformId} 创建临时恢复标签页。`);
  }

  let applied = 0;
  try {
    await sleep(1200);
    await attachDebugger(tabId);
    try {
      await sendDebuggerCommand(tabId, 'Network.enable');
      for (const details of cookieDetails) {
        const result = await sendDebuggerCommand(tabId, 'Network.setCookie', details);
        if (result?.success) {
          applied += 1;
        }
      }
      await sleep(400);
    } finally {
      await detachDebugger(tabId);
    }
  } finally {
    await closeTab(tabId);
  }

  return { applied, mode: 'debugger' };
}

async function setCookies(platformId, cookies = []) {
  getPlatformAdapter(platformId);
  try {
    return await setCookiesViaDebugger(platformId, cookies);
  } catch (error) {
    reportBackgroundError(`setCookies debugger restore (${platformId})`, error);
  }
  let applied = 0;
  for (const item of cookies) {
    const details = cookieSetDetails(item);
    if (!details.name || !details.url) {
      continue;
    }
    await wrapCall(extensionApi.cookies, 'set', details);
    applied += 1;
  }
  return { applied, mode: 'cookies-api' };
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

async function closeTab(tabId) {
  if (!tabId) {
    return;
  }
  try {
    await wrapCall(extensionApi.tabs, 'remove', tabId);
  } catch (_error) {
    // Ignore already-closed tabs.
  }
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

async function refreshContentBridge() {
  const origin = getBackendOrigin(await getSettings());
  if (!origin) {
    return;
  }
  const tabs = await queryTabs({}) || [];
  await Promise.all(
    tabs
      .filter((tab) => String(tab?.url || '').startsWith(origin))
      .map(async (tab) => {
        const tabId = Number(tab?.id || 0);
        if (!tabId) {
          return;
        }
        try {
          await wrapCall(extensionApi.tabs, 'reload', tabId);
        } catch (_error) {
          // Ignore tabs that can no longer be reloaded.
        }
      }),
  );
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

async function inspectQidianEditorState(tabId) {
  if (!tabId) {
    return {
      ok: false,
      wordCount: 0,
      trustedTitleTarget: null,
      trustedBodyTarget: null,
      currentUrl: '',
    };
  }
  const ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 5000);
  if (!ready) {
    return {
      ok: false,
      wordCount: 0,
      trustedTitleTarget: null,
      trustedBodyTarget: null,
      currentUrl: '',
    };
  }
  try {
    return await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action: 'inspect-qidian-editor-state',
    }) || {
      ok: false,
      wordCount: 0,
      trustedTitleTarget: null,
      trustedBodyTarget: null,
      currentUrl: '',
    };
  } catch (_error) {
    return {
      ok: false,
      wordCount: 0,
      trustedTitleTarget: null,
      trustedBodyTarget: null,
      currentUrl: '',
    };
  }
}

async function inspectPlatformAgentDebug(tabId) {
  if (!tabId) {
    return { ok: false, debug: null, currentUrl: '' };
  }
  const ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 5000);
  if (!ready) {
    return { ok: false, debug: null, currentUrl: '' };
  }
  try {
    return await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action: 'inspect-platform-agent-debug',
    }) || { ok: false, debug: null, currentUrl: '' };
  } catch (_error) {
    return { ok: false, debug: null, currentUrl: '' };
  }
}

async function probePlatformAgentResponsive(tabId) {
  if (!tabId) {
    return false;
  }
  try {
    const response = await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action: 'inspect-login-state',
    });
    return Boolean(response);
  } catch (_error) {
    return false;
  }
}

async function sleep(ms) {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

async function sendPlatformAgentMessage(tabId, action, payload, timeoutMs = 12000) {
  return Promise.race([
    wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
      channel: PLATFORM_AGENT_CHANNEL,
      action,
      payload,
    }),
    new Promise((resolve) => {
      globalThis.setTimeout(() => {
        resolve({
          ok: false,
          error: '平台页面执行超时。',
          errorCode: 'platform-agent-timeout',
          currentUrl: '',
          resultPayload: {
            phase: 'message-timeout',
            action,
          },
        });
      }, timeoutMs);
    }),
  ]);
}

async function attachDebugger(tabId) {
  assertDebuggerCapability(
    extensionCapabilities,
    '请在 Chromium 版扩展中执行可信输入、可信点击和调试协议注入相关动作。',
  );
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

async function captureTabScreenshotWithDebugger(tabId) {
  await attachDebugger(tabId);
  try {
    const screenshot = await sendDebuggerCommand(tabId, 'Page.captureScreenshot', {
      format: 'png',
      fromSurface: true,
      captureBeyondViewport: false,
    });
    if (!screenshot?.data) {
      return { ok: false, error: 'debugger-screenshot-empty' };
    }
    return {
      ok: true,
      imageDataUrl: `data:image/png;base64,${screenshot.data}`,
      source: 'debugger-screenshot',
    };
  } finally {
    await detachDebugger(tabId);
  }
}

async function setFileInputFiles(tabId, selector, files) {
  await attachDebugger(tabId);
  try {
    const documentResult = await sendDebuggerCommand(tabId, 'DOM.getDocument', { depth: -1, pierce: true });
    const rootNodeId = documentResult?.root?.nodeId;
    if (!rootNodeId) {
      throw new Error('无法读取页面 DOM。');
    }
    const queryResult = await sendDebuggerCommand(tabId, 'DOM.querySelector', {
      nodeId: rootNodeId,
      selector,
    });
    const nodeId = Number(queryResult?.nodeId || 0);
    if (!nodeId) {
      throw new Error(`未找到文件输入控件：${selector}`);
    }
    await sendDebuggerCommand(tabId, 'DOM.setFileInputFiles', {
      nodeId,
      files,
    });
  } finally {
    await detachDebugger(tabId);
  }
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

async function trustedKeyPress(tabId, key, code, windowsVirtualKeyCode, modifiers = 0) {
  await sendDebuggerCommand(tabId, 'Input.dispatchKeyEvent', {
    type: 'rawKeyDown',
    key,
    code,
    windowsVirtualKeyCode,
    nativeVirtualKeyCode: windowsVirtualKeyCode,
    modifiers,
  });
  await sendDebuggerCommand(tabId, 'Input.dispatchKeyEvent', {
    type: 'keyUp',
    key,
    code,
    windowsVirtualKeyCode,
    nativeVirtualKeyCode: windowsVirtualKeyCode,
    modifiers,
  });
}

async function trustedSelectAllAndDelete(tabId) {
  await trustedKeyPress(tabId, 'a', 'KeyA', 65, 2);
  await sleep(120);
  await trustedKeyPress(tabId, 'Backspace', 'Backspace', 8, 0);
  await sleep(120);
}

async function trustedDeleteChars(tabId, count, delayMs = 50) {
  for (let index = 0; index < count; index += 1) {
    await trustedKeyPress(tabId, 'Backspace', 'Backspace', 8, 0);
    await sleep(delayMs);
  }
}

async function trustedFanqieEditorNudge(tabId, target) {
  if (!target?.x || !target?.y) {
    throw new Error('未能定位番茄正文编辑器。');
  }
  const triggerText = ' 海风从旧港吹来，雨声更急了。';
  await attachDebugger(tabId);
  try {
    await trustedClick(tabId, target.x, target.y);
    await sleep(150);
    await trustedKeyPress(tabId, 'End', 'End', 35, 0);
    await sleep(120);
    await trustedInsertText(tabId, triggerText);
    await sleep(500);
    for (const _char of Array.from(triggerText)) {
      await trustedKeyPress(tabId, 'Backspace', 'Backspace', 8, 0);
      await sleep(40);
    }
    await sleep(800);
  } finally {
    await detachDebugger(tabId);
  }
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
        const inspected = await inspectFanqieEditorState(tabId);
        if (Number(inspected?.wordCount || 0) > 0) {
          return;
        }
        await trustedFanqieEditorNudge(tabId, target);
        const nudged = await inspectFanqieEditorState(tabId);
        if (Number(nudged?.wordCount || 0) > 0) {
          return;
        }
      }
    } catch (_error) {
      // Fall through to the debugger text path below.
    }
    await trustedClick(tabId, target.x, target.y);
    await sleep(150);
    await trustedSelectAllAndDelete(tabId);
    await trustedInsertText(tabId, String(body || ''));
    await sleep(1200);
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

async function applyTrustedQidianEditorInput(tabId, chapterTitle, body, titleTarget, bodyTarget) {
  if ((!titleTarget?.x || !titleTarget?.y) && (!bodyTarget?.x || !bodyTarget?.y)) {
    throw new Error('未能定位起点编辑器输入区域。');
  }
  const normalizedTitle = String(chapterTitle || '');
  const normalizedBody = String(body || '');
  const triggerText = 'x';
  await attachDebugger(tabId);
  try {
    if (titleTarget?.x && titleTarget?.y) {
      await trustedClick(tabId, titleTarget.x, titleTarget.y);
      await sleep(220);
      await trustedSelectAllAndDelete(tabId);
      await trustedInsertText(tabId, normalizedTitle);
      await sleep(320);
    }
    if (bodyTarget?.x && bodyTarget?.y) {
      await trustedClick(tabId, bodyTarget.x, bodyTarget.y);
      await sleep(260);
      await trustedSelectAllAndDelete(tabId);
      await trustedInsertText(tabId, normalizedBody);
      await sleep(400);
      await trustedKeyPress(tabId, 'End', 'End', 35, 0);
      await sleep(120);
      await trustedInsertText(tabId, triggerText);
      await sleep(220);
      await trustedDeleteChars(tabId, triggerText.length, 60);
      await sleep(1200);
    }
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
      const response = await sendPlatformAgentMessage(
        activeTabId,
        'run-upload',
        payload,
        uploadMessageTimeoutMs(payload.platform),
      );
      if (response) {
        if (!response.ok && response.errorCode === 'platform-agent-timeout') {
          lastError = response.error || '平台页面执行超时。';
          const workflowTabId = await waitForPlatformWorkflowTab(payload.platform, activeTabId, 6000);
          if (workflowTabId) {
            activeTabId = workflowTabId;
          }
          const redirectedTabId = await waitForUploadEditorTab(payload.platform, activeTabId, 6000);
          if (redirectedTabId) {
            activeTabId = redirectedTabId;
          }
          readyState = await waitForRunnablePlatformTab(payload.platform, activeTabId, 6000);
          if (readyState) {
            const debugState = await inspectPlatformAgentDebug(activeTabId);
            response.resultPayload = {
              ...(response.resultPayload || {}),
              debug_step: debugState?.debug?.step || '',
              debug_extra: debugState?.debug?.extra || null,
            };
            ready = Boolean(readyState.ready);
            await sleep(1000);
            continue;
          }
          const currentTab = await getTab(activeTabId);
          const debugState = await inspectPlatformAgentDebug(activeTabId);
          return {
            ok: false,
            error: '平台页面执行超时，且未能确认进入章节编辑流程。',
            errorCode: 'chapter-editor-navigation-failed',
            currentUrl: String(currentTab?.url || ''),
            resultPayload: {
              ...(response.resultPayload || {}),
              phase: 'platform-agent-timeout',
              debug_step: debugState?.debug?.step || '',
              debug_extra: debugState?.debug?.extra || null,
            },
          };
        }
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
        if (!response.ok && response.errorCode === 'trusted-body-input-missing') {
          await trustedFanqieEditorNudge(activeTabId, response.trustedBodyTarget);
          await sleep(1200);
          await inspectFanqieEditorState(activeTabId);
          ready = await tabReadyRegistry.waitFor(activeTabId, READY_CHANNELS.PLATFORM_AGENT, 1500);
          payload = {
            ...payload,
            trustedBodyDone: true,
          };
          continue;
        }
        if (
          !response.ok
          && (
            response.errorCode === 'trusted-qidian-editor-input-required'
            || response.errorCode === 'trusted-qidian-editor-input-missing'
          )
        ) {
          await applyTrustedQidianEditorInput(
            activeTabId,
            payload.chapter_title,
            payload.body,
            response.trustedTitleTarget,
            response.trustedBodyTarget,
          );
          await sleep(1200);
          await inspectQidianEditorState(activeTabId);
          ready = await tabReadyRegistry.waitFor(activeTabId, READY_CHANNELS.PLATFORM_AGENT, 1500);
          payload = {
            ...payload,
            trustedQidianEditorDone: true,
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
        if (!response.ok && response.errorCode === 'fanqie-draft-verify-required') {
          const verifyUrl = String(response.resultPayload?.verify_url || '');
          if (!verifyUrl) {
            return response;
          }
          await navigateTab(activeTabId, verifyUrl);
          const workflowTabId = await waitForPlatformWorkflowTab(payload.platform, activeTabId, 15000);
          if (workflowTabId) {
            activeTabId = workflowTabId;
          }
          const runnable = await waitForRunnableWorkflowTab(payload.platform, activeTabId, 12000);
          if (!runnable) {
            return {
              ok: false,
              error: '番茄章节管理页跳转超时，未能核验草稿。',
              errorCode: 'chapter-editor-navigation-failed',
              currentUrl: verifyUrl,
            };
          }
          await sleep(1800);
          return verifyFanqieDraftOnPage(activeTabId, payload.chapter_title);
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

async function runCommentSyncCommand(tabId, payload) {
  if (!tabId) {
    return { ok: false, error: '未能打开评论同步页面。' };
  }
  const readyState = await waitForRunnablePlatformTab(payload.platform, tabId, 12000);
  if (!readyState?.ready) {
    const tab = await getTab(tabId);
    return {
      ok: false,
      error: '平台评论页面没有准备好，无法执行评论同步。',
      currentUrl: String(tab?.url || ''),
      errorCode: 'comment-sync-page-not-ready',
    };
  }
  return sendPlatformAgentMessage(tabId, 'run-comment-sync', payload, 45000);
}

async function runCoverUploadCommand(tabId, payload) {
  if (!tabId) {
    return { ok: false, error: '未能打开封面上传页面。' };
  }
  const readyState = await waitForRunnablePlatformTab(payload.platform, tabId, 12000);
  if (!readyState?.ready) {
    const tab = await getTab(tabId);
    return {
      ok: false,
      error: '平台封面页面没有准备好，无法执行封面上传。',
      currentUrl: String(tab?.url || ''),
      errorCode: 'cover-upload-page-not-ready',
    };
  }
  const prepare = await sendPlatformAgentMessage(tabId, 'prepare-cover-upload', payload, 20000);
  if (!prepare?.ok) {
    return prepare || {
      ok: false,
      error: '未找到封面上传控件。',
      errorCode: 'cover-upload-control-not-found',
    };
  }
  try {
    await setFileInputFiles(tabId, prepare.fileInputSelector || 'input[type="file"]', [payload.file_path]);
  } catch (error) {
    return {
      ok: false,
      currentUrl: String(prepare.currentUrl || ''),
      error: error instanceof Error ? error.message : String(error),
      errorCode: 'cover-upload-file-injection-failed',
      resultPayload: {
        phase: 'set-file-input-files',
        file_input_selector: prepare.fileInputSelector || '',
      },
    };
  }
  return sendPlatformAgentMessage(
    tabId,
    'run-cover-upload',
    {
      ...payload,
      fileInjected: true,
      fileInputSelector: prepare.fileInputSelector || 'input[type="file"]',
    },
    45000,
  );
}

async function runAuditSyncCommand(tabId, payload) {
  if (!tabId) {
    return { ok: false, error: '未能打开审核同步页面。' };
  }
  const readyState = await waitForRunnablePlatformTab(payload.platform, tabId, 12000);
  if (!readyState?.ready) {
    const tab = await getTab(tabId);
    return {
      ok: false,
      error: '平台审核页面没有准备好，无法执行审核同步。',
      currentUrl: String(tab?.url || ''),
      errorCode: 'audit-sync-page-not-ready',
    };
  }
  return sendPlatformAgentMessage(tabId, 'run-audit-sync', payload, 45000);
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
    return (url.includes('write.qq.com') || url.includes('pcwrite.yuewen.com')) && (
      url.includes('/portal/dashboard')
      || url.includes('/create-novel')
      || url.includes('/chaptertmp/')
      || url.includes('/portal/booknovels/chaptertmp/')
      || url.includes('/authorh5/message-notify')
    );
  }
  return false;
}

function isPlatformStatusUrl(platformId, url = '') {
  if (!url) {
    return false;
  }
  if (platformId === 'fanqie') {
    return url.includes('fanqienovel.com');
  }
  if (platformId === 'qidian') {
    return url.includes('write.qq.com') || url.includes('pcwrite.yuewen.com');
  }
  return false;
}

function isPlatformLoginUrl(platformId, url = '') {
  const value = String(url || '');
  if (platformId === 'fanqie') {
    return value.includes('fanqienovel.com') && value.includes('/main/writer/login');
  }
  if (platformId === 'qidian') {
    return value.includes('write.qq.com') && value.includes('/portal/login');
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
      if (candidateReady) {
        return candidateTabId;
      }
      if (candidateStatus === 'complete' && await probePlatformAgentResponsive(candidateTabId)) {
        tabReadyRegistry.markReady(candidateTabId, READY_CHANNELS.PLATFORM_AGENT);
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
      if (candidateReady) {
        return candidateTabId;
      }
      if (candidateStatus === 'complete' && await probePlatformAgentResponsive(candidateTabId)) {
        tabReadyRegistry.markReady(candidateTabId, READY_CHANNELS.PLATFORM_AGENT);
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
      if (ready) {
        return { tabId, url, ready: true };
      }
      if (tab?.status === 'complete' && await probePlatformAgentResponsive(tabId)) {
        tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
        return { tabId, url, ready: true };
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
      if (ready) {
        return { tabId, url, ready: true };
      }
      if (tab?.status === 'complete' && await probePlatformAgentResponsive(tabId)) {
        tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
        return { tabId, url, ready: true };
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

async function sendLoginQrExtractionMessage(tabId, options = null) {
  const message = {
    channel: PLATFORM_AGENT_CHANNEL,
    action: 'extract-login-qr-image',
  };
  if (options) {
    return wrapCall(extensionApi.tabs, 'sendMessage', tabId, message, options);
  }
  return wrapCall(extensionApi.tabs, 'sendMessage', tabId, message);
}

async function queryLoginQrFrames(tabId) {
  if (!extensionApi.webNavigation?.getAllFrames) {
    return [];
  }
  try {
    const frames = await wrapCall(extensionApi.webNavigation, 'getAllFrames', { tabId });
    return findLoginQrFrameTargets(frames);
  } catch (_error) {
    return [];
  }
}

async function extractLoginQrFromFrames(tabId) {
  for (let frameExtractionAttempt = 0; frameExtractionAttempt < 2; frameExtractionAttempt += 1) {
    const targets = (await queryLoginQrFrames(tabId))
      .filter((target) => target.frameId !== 0);
    for (const target of targets) {
      try {
        const response = await sendLoginQrExtractionMessage(tabId, { frameId: target.frameId });
        if (response?.imageDataUrl) {
          return {
            ...response,
            frameUrl: target.url,
            source: response.source
              ? `frame:${target.frameId}:${response.source}`
              : `frame:${target.frameId}:image`,
          };
        }
      } catch (_error) {
        // Continue to the next frame and then to the debugger fallback.
      }
    }
    if (frameExtractionAttempt === 0) {
      await sleep(500);
    }
  }
  return null;
}

async function extractLoginQrFromTopFrame(tabId) {
  try {
    const response = await sendLoginQrExtractionMessage(tabId);
    if (response?.imageDataUrl) {
      return response;
    }
    return null;
  } catch (_error) {
    return null;
  }
}

async function captureLoginQrImage(tabId) {
  if (!tabId) {
    return { ok: false, error: 'missing-tab-id' };
  }
  let ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 3000);
  if (!ready) {
    ready = await probePlatformAgentResponsive(tabId);
    if (ready) {
      tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
    }
  }
  const topFrameResponse = await extractLoginQrFromTopFrame(tabId);
  if (topFrameResponse?.imageDataUrl) {
    return topFrameResponse;
  }
  const frameResponse = await extractLoginQrFromFrames(tabId);
  if (frameResponse?.imageDataUrl) {
    return frameResponse;
  }
  try {
    const screenshot = await captureTabScreenshotWithDebugger(tabId);
    if (screenshot?.imageDataUrl) {
      return screenshot;
    }
    return screenshot;
  } catch (_error) {
    return { ok: false, error: 'debugger-screenshot-failed' };
  }
  return { ok: false, error: 'login-qr-not-found' };
}

async function inspectPlatformState(platformId) {
  const tabs = await queryTabs({}) || [];
  const candidates = tabs
    .filter((tab) => isPlatformStatusUrl(platformId, String(tab?.url || '')))
    .sort((left, right) => {
      if (left?.active && !right?.active) {
        return -1;
      }
      if (right?.active && !left?.active) {
        return 1;
      }
      return (right?.id || 0) - (left?.id || 0);
    });
  for (const candidate of candidates) {
    const tabId = candidate?.id || 0;
    if (!tabId) {
      continue;
    }
    const candidateUrl = String(candidate?.url || '');
    const ready = tabReadyRegistry.isReady(tabId, READY_CHANNELS.PLATFORM_AGENT)
      || await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 800)
      || (candidate?.status === 'complete' && await probePlatformAgentResponsive(tabId));
    if (ready && candidate?.status === 'complete') {
      tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
    }
    const inspection = ready ? await inspectLoginState(tabId) : null;
    if (inspection?.ok) {
      return { ...inspection, tabId };
    }
    if (isPlatformLoginUrl(platformId, candidateUrl)) {
      return {
        ok: true,
        tabId,
        currentUrl: candidateUrl,
        platform: platformId,
        authenticated: false,
        loginVisible: true,
        summary: 'known login url',
      };
    }
  }
  return null;
}

async function verifyFanqieDraftOnPage(tabId, chapterTitle) {
  let ready = tabReadyRegistry.isReady(tabId, READY_CHANNELS.PLATFORM_AGENT)
    || await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 5000);
  if (!ready) {
    ready = await probePlatformAgentResponsive(tabId);
    if (ready) {
      tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
    }
  }
  return verifyFanqieDraftWithRetries({
    chapterTitle,
    maxAttempts: 24,
    verify: async () => {
      try {
        return await wrapCall(extensionApi.tabs, 'sendMessage', tabId, {
          channel: PLATFORM_AGENT_CHANNEL,
          action: 'verify-fanqie-draft',
          payload: { chapterTitle },
        });
      } catch (_error) {
        ready = tabReadyRegistry.isReady(tabId, READY_CHANNELS.PLATFORM_AGENT)
          || await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 2000);
        if (!ready) {
          ready = await probePlatformAgentResponsive(tabId);
          if (ready) {
            tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
          }
        }
        return null;
      }
    },
    reload: async () => {
      try {
        tabReadyRegistry.reset(tabId, READY_CHANNELS.PLATFORM_AGENT);
        await wrapCall(extensionApi.tabs, 'reload', tabId);
        ready = await tabReadyRegistry.waitFor(tabId, READY_CHANNELS.PLATFORM_AGENT, 8000);
        if (!ready) {
          ready = await probePlatformAgentResponsive(tabId);
          if (ready) {
            tabReadyRegistry.markReady(tabId, READY_CHANNELS.PLATFORM_AGENT);
          }
        }
      } catch (_error) {
        // Ignore reload failures and keep polling the page.
      }
    },
    sleep,
  });
}

async function ensureHeartbeatAlarm() {
  const existing = await wrapCall(extensionApi.alarms, 'get', HEARTBEAT_ALARM);
  if (existing?.periodInMinutes === 1) {
    await updateBackgroundStatus({
      heartbeatAlarmReady: true,
      heartbeatAlarmPeriodMinutes: 1,
    });
    return;
  }
  await wrapCall(extensionApi.alarms, 'create', HEARTBEAT_ALARM, { periodInMinutes: 1 });
  await updateBackgroundStatus({
    heartbeatAlarmReady: true,
    heartbeatAlarmPeriodMinutes: 1,
  });
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
    async claimNextCommentSyncJob(payload) {
      return withBackendClient((client) => client.claimNextCommentSyncJob(payload));
    },
    async syncBrowserSession(payload) {
      return withBackendClient((client) => client.syncBrowserSession(payload));
    },
    async notifyLoginQr(payload) {
      return withBackendClient((client) => client.notifyLoginQr(payload));
    },
    async getBrowserSession(platformId) {
      return withBackendClient((client) => client.getBrowserSession(platformId));
    },
    async syncCommentsBatch(payload) {
      return withBackendClient((client) => client.syncCommentsBatch(payload));
    },
    async updateCommentSyncJobResult(jobId, payload) {
      return withBackendClient((client) => client.updateCommentSyncJobResult(jobId, payload));
    },
    async getCommentSyncJob(jobId) {
      return {
        job_id: String(jobId || ''),
        platform: '',
      };
    },
  },
  ensureClientId,
  getClientId: ensureClientId,
  getSettings,
  setSettings,
  getPlatformState,
  setPlatformState,
  getCookies,
  setCookies,
  openLoginPopup,
  openUploadTab,
  closeTab,
  getTab,
  navigateTab,
  closePopup,
  notifyPage,
  openOptionsPage,
  refreshContentBridge,
  getExtensionVersion,
  getBrowserInfo,
  runUploadCommand,
  runCoverUploadCommand,
  runAuditSyncCommand,
  runCommentSyncCommand,
  inspectLoginState,
  inspectPlatformState,
  captureLoginQrImage,
  recordLoginQrNotification: appendLoginQrNotificationStatus,
  recordHeartbeatPlatformState: appendHeartbeatPlatformState,
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

extensionApi.tabs.onCreated.addListener((tab) => {
  controller.handleTabCreated(tab).catch((error) => {
    reportBackgroundError('handleTabCreated', error);
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
    updateBackgroundStatus({
      lastHeartbeatAttemptAt: new Date().toISOString(),
      lastHeartbeatAttemptSource: 'alarm',
    }).catch(() => {});
    controller.sendHeartbeat()
      .then(() => controller.dispatchPendingUploadJobs())
      .then(() => controller.dispatchPendingCommentSyncJobs())
      .then(() => updateBackgroundStatus({
        lastHeartbeatSuccessAt: new Date().toISOString(),
        lastHeartbeatError: '',
      }))
      .catch((error) => {
        updateBackgroundStatus({
          lastHeartbeatError: error instanceof Error ? error.message : String(error || ''),
        }).catch(() => {});
        reportBackgroundError('heartbeat alarm', error);
      });
  }
});

if (extensionApi.runtime.onInstalled) {
  extensionApi.runtime.onInstalled.addListener(() => {
    updateBackgroundStatus({
      lastBootstrapAttemptAt: new Date().toISOString(),
      lastBootstrapSource: 'onInstalled',
    }).catch(() => {});
    controller.bootstrap().catch((error) => {
      updateBackgroundStatus({
        lastBootstrapError: error instanceof Error ? error.message : String(error || ''),
      }).catch(() => {});
      reportBackgroundError('onInstalled bootstrap', error);
    });
  });
}

if (extensionApi.runtime.onStartup) {
  extensionApi.runtime.onStartup.addListener(() => {
    updateBackgroundStatus({
      lastBootstrapAttemptAt: new Date().toISOString(),
      lastBootstrapSource: 'onStartup',
    }).catch(() => {});
    controller.bootstrap().catch((error) => {
      updateBackgroundStatus({
        lastBootstrapError: error instanceof Error ? error.message : String(error || ''),
      }).catch(() => {});
      reportBackgroundError('onStartup bootstrap', error);
    });
  });
}

updateBackgroundStatus({
  workerLoadedAt: new Date().toISOString(),
}).catch(() => {});
controller.bootstrap()
  .then(() => updateBackgroundStatus({
    lastBootstrapSuccessAt: new Date().toISOString(),
    lastBootstrapError: '',
  }))
  .catch((error) => {
    updateBackgroundStatus({
      lastBootstrapError: error instanceof Error ? error.message : String(error || ''),
    }).catch(() => {});
    reportBackgroundError('initial bootstrap', error);
  });
