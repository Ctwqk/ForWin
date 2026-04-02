import { extensionApi, wrapCall } from './lib/extension-runtime.js';
import { DEFAULT_SETTINGS, getBackendOrigin, getOriginMatchPattern, normalizeSettings } from './lib/settings.js';

const SETTINGS_KEY = 'forwinPublisherSettings';

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

async function boot() {
  const settings = await readSettings();
  expectElement('backend_base_url').value = settings.backendBaseUrl;
  expectElement('api_key').value = settings.apiKey;
  expectElement('sync_session_to_backend').checked = settings.syncSessionToBackend;
  expectElement('status').textContent = '已读取当前扩展设置。';
  renderPattern(settings);
}

expectElement('save_button').addEventListener('click', async () => {
  const nextSettings = normalizeSettings({
    backendBaseUrl: expectElement('backend_base_url').value,
    apiKey: expectElement('api_key').value,
    syncSessionToBackend: expectElement('sync_session_to_backend').checked,
  });
  if (!getBackendOrigin(nextSettings)) {
    expectElement('status').textContent = 'ForWin Backend URL 无效，请填写 http(s)://主机:端口。';
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
});

boot().catch((error) => {
  const status = document.getElementById('status');
  if (status) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
});
