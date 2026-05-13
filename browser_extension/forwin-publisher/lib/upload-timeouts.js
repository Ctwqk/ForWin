export function uploadMessageTimeoutMs(platformId) {
  const platform = String(platformId || '').trim();
  if (platform === 'qidian') {
    return 240000;
  }
  if (platform === 'fanqie') {
    return 45000;
  }
  return 45000;
}

export function uploadExecutionTimeoutMs(platformId) {
  const platform = String(platformId || '').trim();
  if (platform === 'qidian') {
    return 420000;
  }
  if (platform === 'fanqie') {
    return 150000;
  }
  return 150000;
}
