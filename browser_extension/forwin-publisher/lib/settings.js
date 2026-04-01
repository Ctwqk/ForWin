export const DEFAULT_SETTINGS = {
  backendBaseUrl: '',
  apiKey: '',
  syncSessionToBackend: true,
};

export function normalizeSettings(input = {}) {
  const backendBaseUrl = String(input.backendBaseUrl || '')
    .trim()
    .replace(/\/+$/, '');
  const apiKey = String(input.apiKey || '').trim();
  const syncSessionToBackend = input.syncSessionToBackend !== false;
  return {
    backendBaseUrl,
    apiKey,
    syncSessionToBackend,
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
  return `${parsed.protocol}//${parsed.hostname}/*`;
}
