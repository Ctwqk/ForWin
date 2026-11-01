export const DEFAULT_SETTINGS = {
  backendBaseUrl: '',
  apiKey: '',
  syncSessionToBackend: true,
  loginQrNotificationsEnabled: false,
  loginQrNotificationsAllowed: false,
  loginQrNotificationsAllowedUntilMs: 0,
  hubBaseUrl: '',
  hubApiKey: '',
};

// Where the publish-hub client id is stored; the background and the options page
// both read and generate it through createHubClientIdStore on this key.
export const HUB_CLIENT_ID_STORAGE_KEY = 'forwinPublisherHubClientId';
const HUB_CLIENT_ID_PREFIX = 'magi3-ext-';
const HUB_CLIENT_ID_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789';
const HUB_CLIENT_ID_LENGTH = 8;

function baseUrl(value) {
  return String(value || '')
    .trim()
    .replace(/\/+$/, '');
}

export function normalizeSettings(input = {}, options = {}) {
  const backendBaseUrl = baseUrl(input.backendBaseUrl);
  const apiKey = String(input.apiKey || '').trim();
  const syncSessionToBackend = input.syncSessionToBackend !== false;
  const nowMs = Number.isFinite(Number(options.nowMs)) ? Number(options.nowMs) : Date.now();
  const rawAllowedUntilMs = Number(input.loginQrNotificationsAllowedUntilMs || 0);
  const loginQrNotificationsAllowedUntilMs = Number.isFinite(rawAllowedUntilMs)
    && rawAllowedUntilMs > 0
    ? rawAllowedUntilMs
    : 0;
  const loginQrNotificationsAllowed = input.loginQrNotificationsAllowed === true
    && loginQrNotificationsAllowedUntilMs > nowMs;
  const loginQrNotificationsEnabled = input.loginQrNotificationsEnabled === true
    && loginQrNotificationsAllowed;
  return {
    backendBaseUrl,
    apiKey,
    syncSessionToBackend,
    loginQrNotificationsEnabled,
    loginQrNotificationsAllowed,
    loginQrNotificationsAllowedUntilMs,
    hubBaseUrl: baseUrl(input.hubBaseUrl),
    hubApiKey: String(input.hubApiKey || '').trim(),
  };
}

function httpOrigin(value) {
  if (!value) {
    return '';
  }
  try {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
      return '';
    }
    return parsed.origin;
  } catch (_error) {
    return '';
  }
}

// publish-hub is used only when both its URL and its key are set; otherwise the
// extension talks to ForWin alone, exactly as before.
export function isHubConfigured(input) {
  const settings = normalizeSettings(input || {});
  return Boolean(httpOrigin(settings.hubBaseUrl) && settings.hubApiKey);
}

function defaultRandomBytes(count) {
  const bytes = new Uint8Array(count);
  globalThis.crypto.getRandomValues(bytes);
  return bytes;
}

export function generateHubClientId(randomBytes = defaultRandomBytes) {
  const bytes = randomBytes(HUB_CLIENT_ID_LENGTH);
  let suffix = '';
  for (let index = 0; index < HUB_CLIENT_ID_LENGTH; index += 1) {
    suffix += HUB_CLIENT_ID_ALPHABET[Number(bytes[index] || 0) % HUB_CLIENT_ID_ALPHABET.length];
  }
  return `${HUB_CLIENT_ID_PREFIX}${suffix}`;
}

// The executor client_id the extension uses with publish-hub. Generated on first
// use and stored, so it survives extension reloads (it is the accounts'
// preferred_client_id). Concurrent first calls share one generation.
export function createHubClientIdStore({ read, write, randomBytes = defaultRandomBytes } = {}) {
  if (typeof read !== 'function' || typeof write !== 'function') {
    throw new TypeError('hub client id store requires read and write functions');
  }
  let pending = null;
  return {
    ensure() {
      if (!pending) {
        pending = (async () => {
          const stored = String((await read()) || '').trim();
          if (stored) {
            return stored;
          }
          const generated = generateHubClientId(randomBytes);
          await write(generated);
          return generated;
        })();
        pending.catch(() => {
          pending = null;
        });
      }
      return pending;
    },
  };
}

export function getBackendOrigin(input) {
  return httpOrigin(normalizeSettings(input).backendBaseUrl);
}

export function getOriginMatchPattern(input) {
  const origin = getBackendOrigin(input);
  if (!origin) {
    return '';
  }
  const parsed = new URL(origin);
  return `${parsed.protocol}//${parsed.host}/*`;
}
