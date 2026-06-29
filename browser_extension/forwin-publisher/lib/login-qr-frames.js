const FRAME_HOST_PRIORITIES = [
  { host: 'open.weixin.qq.com', priority: 40 },
  { host: 'passport.yuewen.com', priority: 30 },
  { host: 'pcwrite.yuewen.com', priority: 25 },
  { host: 'write.qq.com', priority: 20 },
];

export function sanitizeFrameUrlForStatus(value) {
  try {
    const parsed = new URL(String(value || ''));
    return `${parsed.origin}${parsed.pathname}`;
  } catch (_error) {
    return '';
  }
}

export function framePriorityForUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    const hostname = parsed.hostname.toLowerCase();
    const matched = FRAME_HOST_PRIORITIES.find((item) => (
      hostname === item.host || hostname.endsWith(`.${item.host}`)
    ));
    return matched?.priority || 0;
  } catch (_error) {
    return 0;
  }
}

export function isLoginQrFrameUrl(value) {
  return framePriorityForUrl(value) > 0;
}

export function findLoginQrFrameTargets(frames = []) {
  const seenFrameIds = new Set();
  const normalized = [];
  for (const frame of Array.isArray(frames) ? frames : []) {
    const frameId = Number(frame?.frameId ?? 0);
    const priority = framePriorityForUrl(frame?.url);
    if (!Number.isInteger(frameId) || seenFrameIds.has(frameId) || priority <= 0) {
      continue;
    }
    seenFrameIds.add(frameId);
    normalized.push({
      frameId,
      url: sanitizeFrameUrlForStatus(frame?.url),
      priority,
    });
  }
  normalized.sort((left, right) => right.priority - left.priority || left.frameId - right.frameId);
  return normalized;
}
