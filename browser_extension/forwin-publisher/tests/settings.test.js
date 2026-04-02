import test from 'node:test';
import assert from 'node:assert/strict';

import { getBackendOrigin, getOriginMatchPattern, normalizeSettings } from '../lib/settings.js';

test('normalizeSettings trims trailing slash and api key whitespace', () => {
  const settings = normalizeSettings({
    backendBaseUrl: ' http://192.168.31.10:8899/ ',
    apiKey: '  secret-key  ',
  });

  assert.equal(settings.backendBaseUrl, 'http://192.168.31.10:8899');
  assert.equal(settings.apiKey, 'secret-key');
});

test('getOriginMatchPattern converts backend URL into bridge match pattern', () => {
  assert.equal(
    getOriginMatchPattern({ backendBaseUrl: 'http://192.168.31.10:8899' }),
    'http://192.168.31.10:8899/*',
  );
  assert.equal(getBackendOrigin({ backendBaseUrl: 'https://forwin.local:8899/app' }), 'https://forwin.local:8899');
  assert.equal(getBackendOrigin({ backendBaseUrl: 'javascript:alert(1)' }), '');
});
