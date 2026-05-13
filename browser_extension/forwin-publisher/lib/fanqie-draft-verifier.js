function isRetryableDraftVerifyResult(response) {
  if (!response) {
    return true;
  }
  if (response.ok) {
    return false;
  }
  const error = String(response.error || '');
  const code = String(response.errorCode || '');
  return (
    code === 'publish-not-confirmed'
    || code === 'chapter-editor-navigation-failed'
    || error.includes('未找到新草稿')
    || error.includes('未响应草稿核验')
  );
}

export async function verifyFanqieDraftWithRetries({
  chapterTitle,
  verify,
  reload,
  sleep,
  maxAttempts = 24,
  reloadEvery = 4,
}) {
  let lastResponse = null;
  const totalAttempts = Math.max(1, Number(maxAttempts || 24));
  for (let attempt = 1; attempt <= totalAttempts; attempt += 1) {
    lastResponse = await verify({ chapterTitle, attempt });
    if (!isRetryableDraftVerifyResult(lastResponse)) {
      return lastResponse;
    }
    if (attempt >= totalAttempts) {
      break;
    }
    const delayMs = attempt < 8 ? 1500 : 2500;
    await sleep(delayMs);
    if (reload && reloadEvery > 0 && attempt % reloadEvery === 0) {
      await reload({ attempt });
      await sleep(2000);
    }
  }
  return {
    ok: false,
    currentUrl: String(lastResponse?.currentUrl || ''),
    error: '番茄章节管理页未响应草稿核验。',
    errorCode: 'chapter-editor-navigation-failed',
    resultPayload: {
      ...(lastResponse?.resultPayload || {}),
      mode: 'draft',
      chapter_title: chapterTitle,
      verify_phase: 'chapter-manage',
      attempts: totalAttempts,
      last_error: String(lastResponse?.error || ''),
    },
  };
}
