import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_SETTINGS,
  createHubClientIdStore,
  getBackendOrigin,
  getOriginMatchPattern,
  isHubConfigured,
  normalizeSettings,
} from '../lib/settings.js';

test('normalizeSettings trims trailing slash and api key whitespace', () => {
  const settings = normalizeSettings({
    backendBaseUrl: ' http://192.168.31.10:8899/ ',
    apiKey: '  secret-key  ',
  });

  assert.equal(settings.backendBaseUrl, 'http://192.168.31.10:8899');
  assert.equal(settings.apiKey, 'secret-key');
  assert.equal(settings.syncSessionToBackend, true);
  assert.equal(settings.loginQrNotificationsEnabled, false);
});

test('normalizeSettings only enables login QR notifications explicitly', () => {
  assert.equal(normalizeSettings({ loginQrNotificationsEnabled: true }).loginQrNotificationsEnabled, false);
  assert.equal(
    normalizeSettings({
      loginQrNotificationsEnabled: true,
      loginQrNotificationsAllowed: true,
    }, { nowMs: 1_000 }).loginQrNotificationsEnabled,
    false,
  );
  assert.equal(
    normalizeSettings({
      loginQrNotificationsEnabled: true,
      loginQrNotificationsAllowed: true,
      loginQrNotificationsAllowedUntilMs: 2_000,
    }, { nowMs: 1_000 }).loginQrNotificationsEnabled,
    true,
  );
  assert.equal(normalizeSettings({ loginQrNotificationsEnabled: 'true' }).loginQrNotificationsEnabled, false);
  assert.equal(normalizeSettings({}).loginQrNotificationsEnabled, false);
});

test('getOriginMatchPattern converts backend URL into bridge match pattern', () => {
  assert.equal(
    getOriginMatchPattern({ backendBaseUrl: 'http://192.168.31.10:8899' }),
    'http://192.168.31.10:8899/*',
  );
  assert.equal(getBackendOrigin({ backendBaseUrl: 'https://forwin.local:8899/app' }), 'https://forwin.local:8899');
  assert.equal(getBackendOrigin({ backendBaseUrl: 'javascript:alert(1)' }), '');
});

test('normalizeSettings keeps the publish-hub url and key', () => {
  const settings = normalizeSettings({
    backendBaseUrl: 'http://forwin:8899',
    apiKey: 'k',
    hubBaseUrl: ' http://host.docker.internal:18930// ',
    hubApiKey: '  hub-key  ',
  });
  assert.equal(settings.hubBaseUrl, 'http://host.docker.internal:18930');
  assert.equal(settings.hubApiKey, 'hub-key');
  assert.equal(normalizeSettings({}).hubBaseUrl, '');
  assert.equal(normalizeSettings({}).hubApiKey, '');
  assert.equal(DEFAULT_SETTINGS.hubBaseUrl, '');
  assert.equal(DEFAULT_SETTINGS.hubApiKey, '');
});

test('isHubConfigured needs a valid http url and a key', () => {
  assert.equal(isHubConfigured({ hubBaseUrl: 'http://host.docker.internal:18930', hubApiKey: 'k' }), true);
  assert.equal(isHubConfigured({ hubBaseUrl: 'https://hub.example:18930/', hubApiKey: 'k' }), true);
  assert.equal(isHubConfigured({ hubBaseUrl: '', hubApiKey: 'k' }), false);
  assert.equal(isHubConfigured({ hubBaseUrl: 'http://host.docker.internal:18930', hubApiKey: ' ' }), false);
  assert.equal(isHubConfigured({ hubBaseUrl: 'javascript:alert(1)', hubApiKey: 'k' }), false);
  assert.equal(isHubConfigured(undefined), false);
});

test('the hub client id is generated once as magi3-ext-<8 chars> and survives reloads', async () => {
  let stored = '';
  let writes = 0;
  const storage = {
    read: async () => stored,
    write: async (value) => {
      writes += 1;
      stored = value;
    },
  };
  const first = createHubClientIdStore(storage);
  const [a, b] = await Promise.all([first.ensure(), first.ensure()]);
  assert.match(a, /^magi3-ext-[a-z0-9]{8}$/);
  assert.equal(b, a);
  assert.equal(await first.ensure(), a);
  assert.equal(writes, 1);

  const afterReload = createHubClientIdStore(storage);
  assert.equal(await afterReload.ensure(), a);
  assert.equal(writes, 1);

  const other = createHubClientIdStore({ read: async () => '', write: async () => {} });
  assert.notEqual(await other.ensure(), a);

  const fixed = createHubClientIdStore({
    read: async () => '',
    write: async () => {},
    randomBytes: (count) => Uint8Array.from({ length: count }, (_value, index) => index),
  });
  assert.equal(await fixed.ensure(), 'magi3-ext-abcdefgh');
});
