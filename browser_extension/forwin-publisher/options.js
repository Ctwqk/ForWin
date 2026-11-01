import { extensionApi, wrapCall } from './lib/extension-runtime.js';
import {
  DEFAULT_SETTINGS,
  HUB_CLIENT_ID_STORAGE_KEY,
  createHubClientIdStore,
  getBackendOrigin,
  getOriginMatchPattern,
  isHubConfigured,
  normalizeSettings,
} from './lib/settings.js';

const SETTINGS_KEY = 'forwinPublisherSettings';

// Used only when the background cannot answer; same key and format as its store.
const fallbackHubClientIdStore = createHubClientIdStore({
  read: async () => {
    const result = await wrapCall(extensionApi.storage.local, 'get', HUB_CLIENT_ID_STORAGE_KEY);
    return String(result?.[HUB_CLIENT_ID_STORAGE_KEY] || '');
  },
  write: (value) => wrapCall(extensionApi.storage.local, 'set', { [HUB_CLIENT_ID_STORAGE_KEY]: value }),
});

function expectElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`扩展设置页面缺少必要节点: ${id}`);
  }
  return element;
}

async function readSettings() {
  const result = await wrapCall(extensionApi.storage.local, 'get', SETTINGS_KEY);
  return normalizeSettings(result?.[SETTINGS_KEY] || DEFAULT_SETTINGS);
}

async function writeSettings(settings) {
  await wrapCall(extensionApi.storage.local, 'set', {
    [SETTINGS_KEY]: normalizeSettings(settings),
  });
}

function renderPattern(settings) {
  const el = expectElement('match_pattern');
  const pattern = getOriginMatchPattern(settings);
  el.textContent = pattern
    ? `页面桥接匹配规则：${pattern}`
    : '保存后这里会显示扩展注入 ForWin 页面时使用的匹配规则。';
}

// The background generates (or loads) the id, so the page and the adapter share one
// generator; the page falls back to the same store only if the background is not
// reachable.
async function loadHubClientId() {
  try {
    const response = await wrapCall(extensionApi.runtime, 'sendMessage', {
      action: 'hub-client-id',
      payload: {},
    });
    const clientId = String(response?.payload?.hubClientId || '');
    if (clientId) {
      return clientId;
    }
  } catch (_error) {
    // Fall back to the shared store below.
  }
  return fallbackHubClientIdStore.ensure();
}

async function renderHubClientId() {
  const element = expectElement('hub_client_id');
  try {
    const clientId = await loadHubClientId();
    element.textContent = `publish-hub client_id：${clientId}（publish-hub create-account --preferred-client-id 填这个值）`;
  } catch (error) {
    element.textContent = `publish-hub client_id 读取失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function boot() {
  const settings = await readSettings();
  expectElement('backend_base_url').value = settings.backendBaseUrl;
  expectElement('api_key').value = settings.apiKey;
  expectElement('hub_base_url').value = settings.hubBaseUrl;
  expectElement('hub_api_key').value = settings.hubApiKey;
  expectElement('sync_session_to_backend').checked = settings.syncSessionToBackend;
  expectElement('login_qr_notifications_enabled').checked = settings.loginQrNotificationsEnabled;
  expectElement('status').textContent = '已读取当前扩展设置。';
  renderPattern(settings);
  await renderHubClientId();
}

expectElement('save_button').addEventListener('click', async () => {
  const nextSettings = normalizeSettings({
    backendBaseUrl: expectElement('backend_base_url').value,
    apiKey: expectElement('api_key').value,
    syncSessionToBackend: expectElement('sync_session_to_backend').checked,
    loginQrNotificationsEnabled: expectElement('login_qr_notifications_enabled').checked,
    hubBaseUrl: expectElement('hub_base_url').value,
    hubApiKey: expectElement('hub_api_key').value,
  });
  if (!getBackendOrigin(nextSettings)) {
    expectElement('status').textContent = 'ForWin Backend URL 无效，请填写 http(s)://主机:端口。';
    return;
  }
  if ((nextSettings.hubBaseUrl || nextSettings.hubApiKey) && !isHubConfigured(nextSettings)) {
    expectElement('status').textContent = 'publish-hub 需要同时填写有效的 http(s) URL 和 API Key，或者两项都留空。';
    return;
  }
  await writeSettings(nextSettings);
  renderPattern(nextSettings);
  expectElement('status').textContent = '设置已保存，扩展会自动刷新页面桥接并开始向后端发送心跳。';
  try {
    await wrapCall(extensionApi.runtime, 'sendMessage', {
      action: 'settings-updated',
      payload: {},
    });
  } catch (_error) {
    // Ignore background wake-up issues here; next page load will retry.
  }
  await renderHubClientId();
});

boot().catch((error) => {
  const status = document.getElementById('status');
  if (status) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
});
