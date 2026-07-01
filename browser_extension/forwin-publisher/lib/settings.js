export const DEFAULT_SETTINGS = {
  backendBaseUrl: '',
  apiKey: '',
  syncSessionToBackend: true,
  loginQrNotificationsEnabled: false,
  loginQrNotificationsAllowed: false,
  loginQrNotificationsAllowedUntilMs: 0,
};

export function normalizeSettings(input = {}, options = {}) {
  const backendBaseUrl = String(input.backendBaseUrl || '')
    .trim()
    .replace(/\/+$/, '');
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
  };
}

export function getBackendOrigin(input) {
  const settings = normalizeSettings(input);
  if (!settings.backendBaseUrl) {
    return '';
  }
  try {
    const parsed = new URL(settings.backendBaseUrl);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
      return '';
    }
    return parsed.origin;
  } catch (_error) {
    return '';
  }
}

export function getOriginMatchPattern(input) {
  const origin = getBackendOrigin(input);
  if (!origin) {
    return '';
  }
  const parsed = new URL(origin);
  return `${parsed.protocol}//${parsed.host}/*`;
}
