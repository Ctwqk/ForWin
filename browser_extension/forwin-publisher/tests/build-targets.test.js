import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { buildManifestForTarget } from '../build-targets.js';

const sourceManifest = {
  manifest_version: 3,
  permissions: ['alarms', 'cookies', 'debugger', 'storage', 'tabs', 'webNavigation'],
  background: {
    service_worker: 'background.js',
    type: 'module',
  },
};

test('buildManifestForTarget keeps service worker and debugger for chromium', () => {
  const manifest = buildManifestForTarget('chromium', sourceManifest);

  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.background.service_worker, 'background.js');
  assert.equal(manifest.background.type, 'module');
  assert.equal(manifest.permissions.includes('debugger'), true);
  assert.equal(manifest.permissions.includes('webNavigation'), true);
  assert.equal(manifest.browser_specific_settings, undefined);
});

test('buildManifestForTarget switches to Firefox background scripts and gecko settings', () => {
  const manifest = buildManifestForTarget('firefox', sourceManifest);

  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.background.scripts, ['background.js']);
  assert.equal(manifest.background.type, 'module');
  assert.equal('service_worker' in manifest.background, false);
  assert.equal(manifest.permissions.includes('debugger'), false);
  assert.equal(manifest.options_page, undefined);
  assert.equal(manifest.options_ui.page, 'options.html');
  assert.equal(manifest.options_ui.open_in_tab, true);
  assert.equal(manifest.browser_specific_settings.gecko.id, 'forwin-publisher@example.com');
});

test('source manifest injects platform agent into Qidian WeChat login frames', async () => {
  const manifest = JSON.parse(
    await readFile(new URL('../manifest.json', import.meta.url), 'utf8'),
  );
  const platformContentScript = manifest.content_scripts.find((script) => (
    Array.isArray(script.js) && script.js.includes('platform-agent.js')
  ));

  assert.equal(manifest.permissions.includes('webNavigation'), true);
  assert.equal(manifest.host_permissions.includes('https://open.weixin.qq.com/*'), true);
  assert.equal(platformContentScript.matches.includes('https://open.weixin.qq.com/*'), true);
  assert.equal(platformContentScript.all_frames, true);
});
