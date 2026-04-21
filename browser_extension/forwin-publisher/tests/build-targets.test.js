import test from 'node:test';
import assert from 'node:assert/strict';

import { buildManifestForTarget } from '../build-targets.js';

const sourceManifest = {
  manifest_version: 3,
  permissions: ['alarms', 'cookies', 'debugger', 'storage', 'tabs'],
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
