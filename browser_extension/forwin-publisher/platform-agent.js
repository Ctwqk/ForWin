(function () {
  const CHANNEL = globalThis.__FORWIN_CHANNELS__?.PLATFORM_AGENT_CHANNEL || 'forwin-publisher-platform-agent';
  const runtime = (globalThis.browser && globalThis.browser.runtime) || (globalThis.chrome && globalThis.chrome.runtime);
  if (!runtime) {
    return;
  }

  function markPlatformAgentState(name, value = '1') {
    try {
      const root = document.documentElement;
      if (root instanceof HTMLElement) {
        root.setAttribute(name, String(value));
      }
    } catch (_error) {
      // Best effort only.
    }
  }

  markPlatformAgentState('data-forwin-platform-agent-boot', '1');

  function announceReady() {
    try {
      runtime.sendMessage({
        action: 'platform-agent-ready',
        payload: { href: window.location.href },
      });
    } catch (_error) {
      // Ignore while the extension worker spins up.
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function setDebugStep(step, extra = {}) {
    try {
      globalThis.__FORWIN_PLATFORM_AGENT_DEBUG__ = {
        step: String(step || '').trim(),
        extra: extra && typeof extra === 'object' ? extra : {},
        url: String(window.location.href || ''),
        at: Date.now(),
      };
      markPlatformAgentState('data-forwin-platform-agent-step', String(step || '').trim());
    } catch (_error) {
      // Best effort only.
    }
  }

  function waitForCondition(check, timeoutMs = 8000) {
    return new Promise((resolve) => {
      const finish = (value) => {
        if (observer) {
          observer.disconnect();
        }
        window.clearInterval(intervalId);
        window.clearTimeout(timeoutId);
        resolve(value);
      };

      const evaluate = () => {
        try {
          const value = check();
          if (value) {
            finish(value);
          }
        } catch (_error) {
          // Ignore transient DOM errors while the page is changing.
        }
      };

      let observer = null;
      if (document.documentElement) {
        observer = new MutationObserver(() => evaluate());
        observer.observe(document.documentElement, {
          subtree: true,
          childList: true,
          attributes: true,
          characterData: true,
        });
      }
      const intervalId = window.setInterval(evaluate, 150);
      const timeoutId = window.setTimeout(() => finish(null), timeoutMs);
      evaluate();
    });
  }

  async function waitForUrlContains(fragment, timeoutMs = 8000) {
    return waitForCondition(() => window.location.href.includes(fragment), timeoutMs);
  }

  async function waitForPageSignal(keywords, timeoutMs = 8000) {
    return waitForCondition(() => includesAny(pageText(), keywords), timeoutMs);
  }

  async function waitForEditorReady(timeoutMs = 10000) {
    return waitForCondition(() => {
      if (window.location.href.includes('write.qq.com')) {
        return document.querySelector('#inputTitle')
          || document.querySelector('iframe#mce_0_ifr')
          || document.querySelector('textarea#mce_0');
      }
      if (window.location.href.includes('fanqienovel.com')) {
        return document.querySelector('.ProseMirror[contenteditable="true"]')
          || document.querySelector('input[placeholder*="标题"]')
          || document.querySelector('textarea[placeholder*="标题"]');
      }
      return null;
    }, timeoutMs);
  }

  function pageText() {
    return String(document.body?.innerText || '').replace(/\s+/g, ' ').trim();
  }

  function makeUploadError(message, code = '', resultPayload = null) {
    const error = new Error(message);
    if (code) {
      error.code = code;
    }
    if (resultPayload && typeof resultPayload === 'object') {
      error.resultPayload = resultPayload;
    }
    return error;
  }

  function includesAny(text, keywords) {
    return keywords.some((keyword) => text.includes(keyword));
  }

  function normalizeText(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function firstNonEmpty(source, keys) {
    if (!source || typeof source !== 'object') {
      return '';
    }
    for (const key of keys) {
      const value = source[key];
      if (value === null || value === undefined) {
        continue;
      }
      const text = normalizeText(value);
      if (text) {
        return text;
      }
    }
    return '';
  }

  function firstNumber(source, keys, fallback = 0) {
    if (!source || typeof source !== 'object') {
      return fallback;
    }
    for (const key of keys) {
      const value = Number(source[key]);
      if (Number.isFinite(value)) {
        return value;
      }
    }
    return fallback;
  }

  function extractListPayload(payload) {
    const candidates = [
      payload,
      payload?.records,
      payload?.list,
      payload?.items,
      payload?.comment_list,
      payload?.messageList,
      payload?.data,
      payload?.data?.records,
      payload?.data?.list,
      payload?.data?.items,
      payload?.data?.comment_list,
      payload?.data?.messageList,
      payload?.result,
      payload?.result?.records,
      payload?.result?.list,
      payload?.result?.items,
    ];
    for (const candidate of candidates) {
      if (Array.isArray(candidate)) {
        return candidate;
      }
    }
    return [];
  }

  function extractQidianMenus(payload) {
    const candidates = [
      payload?.childrenMenuInfoList,
      payload?.menuList,
      payload?.data?.childrenMenuInfoList,
      payload?.data?.menuList,
      payload?.result?.childrenMenuInfoList,
      payload?.result?.menuList,
    ];
    for (const candidate of candidates) {
      if (Array.isArray(candidate)) {
        return candidate;
      }
    }
    return [];
  }

  async function fetchPlatformJson(path, options = {}) {
    const url = path.startsWith('http')
      ? path
      : new URL(path, window.location.origin).toString();
    const response = await fetch(url, {
      method: options.method || 'GET',
      credentials: 'include',
      headers: {
        Accept: 'application/json, text/plain, */*',
        ...(options.headers || {}),
      },
    });
    const rawText = await response.text();
    let payload = {};
    try {
      payload = rawText ? JSON.parse(rawText) : {};
    } catch (_error) {
      payload = { raw_text: rawText };
    }
    if (!response.ok) {
      throw makeUploadError(
        firstNonEmpty(payload, ['message', 'msg', 'detail']) || `HTTP ${response.status}`,
        'platform-api-request-failed',
        { path, status: response.status, payload },
      );
    }
    const code = Number(payload?.code);
    if (Number.isFinite(code) && code !== 0) {
      throw makeUploadError(
        firstNonEmpty(payload, ['message', 'msg']) || `平台接口返回 code=${code}`,
        'platform-api-request-failed',
        { path, code, payload },
      );
    }
    return payload;
  }

  function buildNormalizedComment(record, overrides = {}) {
    const category = normalizeText(overrides.category || firstNonEmpty(record, ['comment_type', 'message_type'])) || 'comment';
    const rawCommentId = firstNonEmpty(record, [
      'comment_id',
      'id',
      'message_id',
      'msgId',
      'msg_id',
      'item_id',
    ]);
    const baseParentId = normalizeText(overrides.parent_remote_comment_id || firstNonEmpty(record, ['parent_comment_id', 'root_comment_id']));
    return {
      remote_comment_id: `${category}:${rawCommentId || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`,
      work_id: normalizeText(overrides.work_id || firstNonEmpty(record, ['book_id', 'work_id', 'bookId'])),
      work_name: normalizeText(overrides.work_name || firstNonEmpty(record, ['book_name', 'work_name', 'bookName'])),
      chapter_id: normalizeText(overrides.chapter_id || firstNonEmpty(record, ['chapter_id', 'chapterId', 'item_id'])),
      chapter_title: normalizeText(overrides.chapter_title || firstNonEmpty(record, ['chapter_title', 'item_title', 'chapter_name', 'chapterName'])),
      author_id: normalizeText(firstNonEmpty(record, ['user_id', 'author_id', 'uid', 'reader_user_id', 'sender_id'])),
      author_name: normalizeText(firstNonEmpty(record, ['user_name', 'author_name', 'nickname', 'nick_name', 'reader_name', 'sender_name'])),
      body: normalizeText(overrides.body || firstNonEmpty(record, [
        'content',
        'comment_content',
        'reply_content',
        'messageContent',
        'message_content',
        'text',
        'desc',
        'description',
        'title',
      ])),
      parent_remote_comment_id: baseParentId
        ? (baseParentId.includes(':') ? baseParentId : `${category}:${baseParentId}`)
        : '',
      created_at: normalizeText(firstNonEmpty(record, ['create_time', 'created_at', 'ctime', 'comment_time', 'messageTime', 'send_time', 'update_time'])),
      like_count: Math.max(0, firstNumber(record, ['digg_count', 'like_count', 'praise_count'])),
      reply_count: Math.max(0, firstNumber(record, ['reply_count', 'comment_count', 'sub_comment_count'])),
      raw_payload: {
        ...(record && typeof record === 'object' ? record : {}),
        forwin_category: category,
      },
    };
  }

  function dedupeComments(comments, limit) {
    const seen = new Set();
    const rows = [];
    for (const item of comments) {
      const remoteId = normalizeText(item?.remote_comment_id);
      const body = normalizeText(item?.body);
      if (!remoteId || !body || seen.has(remoteId)) {
        continue;
      }
      seen.add(remoteId);
      rows.push(item);
      if (rows.length >= limit) {
        break;
      }
    }
    return rows;
  }

  async function fetchFanqieCommentPage(path, params) {
    const url = new URL(path, window.location.origin);
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === '' || value === null || value === undefined) {
        return;
      }
      url.searchParams.set(key, String(value));
    });
    return fetchPlatformJson(url.toString(), {
      method: 'GET',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
  }

  async function collectFanqieComments(payload) {
    const limit = Math.min(Math.max(Number(payload?.limit || 100), 1), 100);
    const pageSize = Math.min(limit, 20);
    const comments = [];
    const workId = normalizeText(payload?.work_id);
    const workName = normalizeText(payload?.work_name);
    const chapterId = normalizeText(payload?.chapter_id);
    const chapterTitle = normalizeText(payload?.chapter_title);
    if (!workId) {
      throw makeUploadError('番茄评论同步缺少作品 ID。', 'comment-sync-invalid-payload');
    }

    const fetchers = [
      {
        category: 'book',
        path: '/api/author/comment/book_comment_list/v0/',
        baseParams: {
          book_id: workId,
          days: 30,
          sort: 0,
          user_filter: 0,
          scope_filter: 0,
        },
      },
      chapterId
        ? {
          category: 'chapter',
          path: '/api/author/comment/chapter_comment_list/v0/',
          baseParams: {
            book_id: workId,
            item_id: chapterId,
            days: 30,
            sort: 0,
          },
        }
        : null,
    ].filter(Boolean);

    for (const fetcher of fetchers) {
      let pageIndex = 1;
      while (comments.length < limit) {
        const page = await fetchFanqieCommentPage(fetcher.path, {
          ...fetcher.baseParams,
          page_index: pageIndex,
          page_count: pageSize,
        });
        const list = extractListPayload(page);
        if (!list.length) {
          break;
        }
        for (const entry of list) {
          const row = buildNormalizedComment(entry, {
            category: fetcher.category,
            work_id: workId,
            work_name: workName,
            chapter_id: fetcher.category === 'chapter' ? chapterId : normalizeText(firstNonEmpty(entry, ['chapter_id', 'item_id'])),
            chapter_title: fetcher.category === 'chapter' ? chapterTitle : normalizeText(firstNonEmpty(entry, ['chapter_title', 'item_title', 'chapter_name'])),
          });
          comments.push(row);
          const rawCommentId = firstNonEmpty(entry, ['comment_id', 'id']);
          const replyCount = Math.min(Math.max(Number(row.reply_count || 0), 0), 10);
          if (rawCommentId && replyCount > 0 && comments.length < limit) {
            const replies = await fetchFanqieCommentPage('/api/author/comment/reply_comment_list/v0/', {
              comment_id: rawCommentId,
              page_index: 1,
              page_count: Math.min(replyCount, 10),
            });
            for (const reply of extractListPayload(replies)) {
              comments.push(buildNormalizedComment(reply, {
                category: 'reply',
                work_id: workId,
                work_name: workName,
                chapter_id: row.chapter_id,
                chapter_title: row.chapter_title,
                parent_remote_comment_id: row.remote_comment_id.split(':').slice(1).join(':'),
              }));
              if (comments.length >= limit) {
                break;
              }
            }
          }
          if (comments.length >= limit) {
            break;
          }
        }
        if (list.length < pageSize) {
          break;
        }
        pageIndex += 1;
        if (pageIndex > 5) {
          break;
        }
      }
    }

    return dedupeComments(comments, limit);
  }

  function qidianMessageLooksLikeComment(record) {
    const haystack = normalizeText([
      firstNonEmpty(record, ['title', 'msgTitle', 'message_title']),
      firstNonEmpty(record, ['content', 'messageContent', 'message_content', 'desc']),
      firstNonEmpty(record, ['book_name', 'bookName']),
      firstNonEmpty(record, ['chapter_title', 'chapterName']),
      firstNonEmpty(record, ['menuTitle', 'menu_name']),
    ].join(' '));
    return /评论|书评|本章说|章评|留言|回复|读者|书友|互动|催更/.test(haystack);
  }

  function normalizeQidianMessage(record, menu = {}) {
    const title = normalizeText(firstNonEmpty(record, ['title', 'msgTitle', 'message_title']));
    const content = normalizeText(firstNonEmpty(record, ['content', 'messageContent', 'message_content', 'desc']));
    return buildNormalizedComment(record, {
      category: `message-${normalizeText(firstNonEmpty(menu, ['type', 'menuType', 'id']) || firstNonEmpty(record, ['type'])) || 'unknown'}`,
      body: normalizeText([title, content].filter(Boolean).join(' | ')),
      work_id: normalizeText(firstNonEmpty(record, ['book_id', 'bookId'])),
      work_name: normalizeText(firstNonEmpty(record, ['book_name', 'bookName'])),
      chapter_id: normalizeText(firstNonEmpty(record, ['chapter_id', 'chapterId', 'item_id'])),
      chapter_title: normalizeText(firstNonEmpty(record, ['chapter_title', 'chapterName', 'item_title'])),
    });
  }

  async function collectQidianComments(payload) {
    const limit = Math.min(Math.max(Number(payload?.limit || 100), 1), 100);
    const menuPayload = await fetchPlatformJson('/ccauthorapp/desk/message/getMenuMessInfo', {
      method: 'GET',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    const menus = extractQidianMenus(menuPayload);
    const candidateMenus = menus.filter((menu) => /评论|书评|本章说|章评|回复|互动/.test(
      normalizeText(firstNonEmpty(menu, ['title', 'menuTitle', 'name', 'menu_name'])),
    ));
    const menusToFetch = candidateMenus.length ? candidateMenus : menus;
    if (!menusToFetch.length) {
      throw makeUploadError('起点消息中心未返回可用的消息分类。', 'comment-sync-platform-unsupported', {
        menu_payload: menuPayload,
      });
    }

    const comments = [];
    for (const menu of menusToFetch) {
      const menuType = normalizeText(firstNonEmpty(menu, ['type', 'menuType', 'id']));
      if (!menuType) {
        continue;
      }
      const listPayload = await fetchPlatformJson(
        `/ccauthorapp/desk/message/getMessageList?type=${encodeURIComponent(menuType)}&IDX=0&pageSize=${Math.min(limit, 20)}`,
        {
          method: 'GET',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        },
      );
      const list = extractListPayload(listPayload);
      for (const entry of list) {
        if (!candidateMenus.length && !qidianMessageLooksLikeComment(entry)) {
          continue;
        }
        comments.push(normalizeQidianMessage(entry, menu));
        if (comments.length >= limit) {
          break;
        }
      }
      if (comments.length >= limit) {
        break;
      }
    }

    const rows = dedupeComments(comments, limit);
    if (!rows.length) {
      throw makeUploadError('起点消息中心未找到可同步的评论消息。', 'comment-sync-no-comment-found', {
        menus: menus.map((menu) => ({
          type: firstNonEmpty(menu, ['type', 'menuType', 'id']),
          title: firstNonEmpty(menu, ['title', 'menuTitle', 'name', 'menu_name']),
        })),
      });
    }
    return {
      comments: rows,
      menus: menus.map((menu) => ({
        type: firstNonEmpty(menu, ['type', 'menuType', 'id']),
        title: firstNonEmpty(menu, ['title', 'menuTitle', 'name', 'menu_name']),
      })),
    };
  }

  async function runCommentSync(payload) {
    setDebugStep('run-comment-sync-start', {
      platform: String(payload?.platform || ''),
      workId: String(payload?.work_id || ''),
      chapterId: String(payload?.chapter_id || ''),
      limit: Number(payload?.limit || 0),
    });
    if (window.location.href.includes('login')) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '平台当前仍在登录页，请先完成扫码登录。',
        errorCode: 'login-required',
      };
    }
    if (window.location.href.includes('fanqienovel.com')) {
      const comments = await collectFanqieComments(payload);
      return {
        ok: true,
        currentUrl: window.location.href,
        message: `番茄评论同步完成，共抓取 ${comments.length} 条评论。`,
        comments,
        resultPayload: { source: 'fanqie-author-api' },
      };
    }
    if (window.location.href.includes('write.qq.com') || window.location.href.includes('pcwrite.yuewen.com')) {
      const result = await collectQidianComments(payload);
      return {
        ok: true,
        currentUrl: window.location.href,
        message: `起点评论同步完成，共抓取 ${result.comments.length} 条评论消息。`,
        comments: result.comments,
        resultPayload: {
          source: 'qidian-message-center',
          menus: result.menus,
        },
      };
    }
    return {
      ok: false,
      currentUrl: window.location.href,
      error: '当前页面不支持评论同步。',
      errorCode: 'comment-sync-unsupported-page',
    };
  }

  function inspectLoginState() {
    const url = window.location.href;
    const text = pageText();

    if (url.includes('write.qq.com') || url.includes('pcwrite.yuewen.com')) {
      const loginVisible = includesAny(text, [
        '扫码登录',
        '微信扫码',
        '手机扫码',
        '账号登录',
        '验证码登录',
      ]);
      const authenticated = (!loginVisible && includesAny(text, [
        '作品管理',
        '作家专区',
        '章节管理',
        '写新章',
        '新建章节',
        '数据概览',
        '消息中心',
        '消息通知',
      ])) || url.includes('/authorh5/');
      return {
        ok: true,
        currentUrl: url,
        platform: 'qidian',
        authenticated,
        loginVisible,
        summary: text.slice(0, 400),
      };
    }

    if (url.includes('fanqienovel.com')) {
      const loginVisible = includesAny(text, [
        '登录',
        '扫码登录',
        '验证码登录',
        '手机验证码',
        '抖音登录',
      ]);
      const authenticated = !loginVisible && includesAny(text, [
        '工作台',
        '作品管理',
        '作家专区',
        '写新章',
        '新增章节',
        '继续创作',
        '数据总览',
        '创作服务',
        '创建新书',
        '去写作',
      ]);
      return {
        ok: true,
        currentUrl: url,
        platform: 'fanqie',
        authenticated,
        loginVisible,
        summary: text.slice(0, 400),
      };
    }

    return {
      ok: true,
      currentUrl: url,
      platform: 'unknown',
      authenticated: false,
      loginVisible: false,
      summary: text.slice(0, 400),
    };
  }

  function selectorsForTitle() {
    return [
      'input[placeholder*="标题"]',
      'textarea[placeholder*="标题"]',
      'input[name*="title"]',
      'input[id*="title"]',
      '[contenteditable="true"][data-placeholder*="标题"]',
    ];
  }

  function selectorsForBody() {
    return [
      'textarea[placeholder*="正文"]',
      'textarea[placeholder*="内容"]',
      'textarea',
      '[contenteditable="true"]',
      'div[role="textbox"]',
    ];
  }

  function buttonMatches(node, keywords) {
    const text = String(node?.innerText || node?.textContent || '').trim();
    return keywords.some((keyword) => text.includes(keyword));
  }

  async function waitForSelector(selectors, timeoutMs = 15000) {
    const startedAt = Date.now();
    while ((Date.now() - startedAt) < timeoutMs) {
      for (const selector of selectors) {
        const node = document.querySelector(selector);
        if (node) {
          return node;
        }
      }
      await sleep(350);
    }
    return null;
  }

  function fillNode(node, value) {
    if (!node) {
      return;
    }
    const tagName = String(node.tagName || '').toLowerCase();
    if (tagName === 'input' || tagName === 'textarea') {
      node.focus();
      const prototype = tagName === 'textarea'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
      if (descriptor?.set) {
        descriptor.set.call(node, value);
      } else {
        node.value = value;
      }
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
      node.dispatchEvent(new Event('blur', { bubbles: true }));
      return;
    }
    node.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function setInputValue(node, value) {
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    const tagName = String(node.tagName || '').toLowerCase();
    const prototype = tagName === 'textarea'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor?.set) {
      descriptor.set.call(node, value);
    } else {
      node.value = value;
    }
    node.dispatchEvent(new InputEvent('beforeinput', {
      bubbles: true,
      cancelable: true,
      data: String(value || ''),
      inputType: 'insertText',
    }));
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function fillInputExact(node, value, options = {}) {
    const normalizedValue = String(value || '');
    const perCharDelayMs = Number(options.perCharDelayMs || 30);
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    node.focus();
    setInputValue(node, normalizedValue);
    node.dispatchEvent(new Event('blur', { bubbles: true }));
    await sleep(200);
    if (String(node.value || '').trim() === normalizedValue.trim()) {
      return true;
    }
    node.focus();
    try {
      if (typeof node.select === 'function') {
        node.select();
      } else {
        document.execCommand('selectAll', false, null);
      }
    } catch (_error) {
      // Ignore when the browser blocks selection commands.
    }
    setInputValue(node, '');
    for (const char of normalizedValue) {
      setInputValue(node, `${String(node.value || '')}${char}`);
      await sleep(perCharDelayMs);
    }
    node.dispatchEvent(new Event('blur', { bubbles: true }));
    await sleep(300);
    return String(node.value || '').trim() === normalizedValue.trim();
  }

  async function fillField(selectors, value, fieldName) {
    const node = await waitForSelector(selectors);
    if (!node) {
      throw new Error(`未找到可填写的${fieldName}输入框。`);
    }
    fillNode(node, value);
  }

  async function clickByKeywords(keywords, timeoutMs = 12000) {
    const startedAt = Date.now();
    while ((Date.now() - startedAt) < timeoutMs) {
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
      const target = nodes.find((node) => buttonMatches(node, keywords));
      if (target) {
        target.click();
        return true;
      }
      await sleep(350);
    }
    return false;
  }

  async function clickExactText(text, timeoutMs = 4000) {
    const startedAt = Date.now();
    while ((Date.now() - startedAt) < timeoutMs) {
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === text)
        .sort((left, right) => left.childElementCount - right.childElementCount);
      const target = nodes[0];
      if (target) {
        target.click();
        return true;
      }
      await sleep(250);
    }
    return false;
  }

  async function clickExactTexts(labels, timeoutMs = 4000) {
    for (const label of labels) {
      if (await clickExactText(label, timeoutMs)) {
        return true;
      }
    }
    return false;
  }

  async function clickActionExactText(text, timeoutMs = 4000, root = document) {
    const startedAt = Date.now();
    while ((Date.now() - startedAt) < timeoutMs) {
      const directTargets = Array.from(root.querySelectorAll('button, [role="button"], a'))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === text)
        .sort((left, right) => left.childElementCount - right.childElementCount);
      const direct = directTargets[0];
      if (direct instanceof HTMLElement) {
        direct.click();
        return true;
      }
      const nestedTargets = Array.from(root.querySelectorAll('div, span'))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === text)
        .map((node) => node.closest?.('button, [role="button"], a') || node)
        .filter((node) => node instanceof HTMLElement)
        .sort((left, right) => left.childElementCount - right.childElementCount);
      const nested = nestedTargets[0];
      if (nested instanceof HTMLElement) {
        nested.click();
        return true;
      }
      await sleep(250);
    }
    return false;
  }

  function hasFanqieEmptyWorkState() {
    const text = pageText();
    return includesAny(text, [
      '助力开书第一笔',
      '书本信息未准备好',
      '先发布章节再补充',
      '创建书本',
    ]);
  }

  function normalizeFanqieBookMeta(rawMeta, bookName) {
    const meta = rawMeta && typeof rawMeta === 'object' ? rawMeta : {};
    const rawPrimaryCategory = String(meta.primary_category || '').trim();
    const normalizedPrimaryCategory = (() => {
      if (!rawPrimaryCategory) {
        return '悬疑灵异';
      }
      const mappings = [
        ['悬疑', '悬疑灵异'],
        ['灵异', '悬疑灵异'],
        ['脑洞', '悬疑脑洞'],
        ['科幻', '科幻末世'],
        ['末世', '科幻末世'],
        ['都市', '都市日常'],
        ['玄幻', '传统玄幻'],
        ['仙侠', '东方仙侠'],
        ['历史', '历史古代'],
        ['游戏', '游戏体育'],
        ['衍生', '男频衍生'],
      ];
      const matched = mappings.find(([needle]) => rawPrimaryCategory.includes(needle));
      return matched ? matched[1] : rawPrimaryCategory;
    })();
    const protagonistNames = Array.isArray(meta.protagonist_names)
      ? meta.protagonist_names.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 2)
      : [];
    const intro = String(meta.intro || '').trim();
    const fallbackIntro = `${String(bookName || '').trim()}讲的是普通人被旧日悬案卷入后，在现实缝隙里一步步逼近真相的故事。`;
    const normalizedIntro = (() => {
      const base = intro || fallbackIntro;
      if (base.length >= 50) {
        return base;
      }
      const suffix = '故事将围绕旧案、港区谜团与人物命运展开，在层层追索中揭开真相。';
      const combined = `${base}${suffix}`;
      return combined.length >= 50 ? combined : `${combined}${fallbackIntro}`;
    })();
    return {
      audience: String(meta.audience || 'male').trim().toLowerCase(),
      primaryCategory: normalizedPrimaryCategory,
      themeTags: Array.isArray(meta.theme_tags)
        ? meta.theme_tags.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 2)
        : [],
      roleTags: Array.isArray(meta.role_tags)
        ? meta.role_tags.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 2)
        : [],
      plotTags: Array.isArray(meta.plot_tags)
        ? meta.plot_tags.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 2)
        : [],
      protagonistNames,
      intro: normalizedIntro,
    };
  }

  function normalizeQidianBookMeta(rawMeta, bookName) {
    const meta = rawMeta && typeof rawMeta === 'object' ? rawMeta : {};
    const intro = String(meta.intro || '').trim();
    const primaryCategory = String(meta.primary_category || '').trim();
    return {
      audience: String(meta.audience || 'male').trim().toLowerCase(),
      primaryCategory,
      intro: intro || `${String(bookName || '').trim()}的故事从一桩旧事重启，在现实与悬念交织中一步步逼近真相。`,
    };
  }

  function resolveQidianCategory(meta) {
    const primary = String(meta?.primaryCategory || '').trim();
    if (!primary) {
      return '悬疑';
    }
    const mapped = [
      ['都市', '都市'],
      ['日常', '都市'],
      ['悬疑', '悬疑'],
      ['科幻', '科幻'],
      ['历史', '历史'],
      ['玄幻', '玄幻'],
      ['奇幻', '奇幻'],
      ['仙侠', '仙侠'],
      ['武侠', '武侠'],
      ['游戏', '游戏'],
      ['轻小说', '轻小说'],
      ['现实', '现实'],
      ['短篇', '短篇'],
      ['诸天', '诸天无限'],
    ].find(([needle]) => primary.includes(needle));
    return mapped ? mapped[1] : '悬疑';
  }

  async function ensureFanqieCreateBookPage() {
    if (window.location.href.includes('/main/writer/create')) {
      return true;
    }
    await dismissFanqieGuideModal();
    window.location.href = 'https://fanqienovel.com/main/writer/create';
    await Promise.race([
      waitForUrlContains('/main/writer/create', 8000),
      waitForPageSignal(['创建作品', '立即创建', '书本名称'], 8000),
    ]);
    if (window.location.href.includes('/main/writer/create') || includesAny(pageText(), ['创建作品', '立即创建', '书本名称'])) {
      return true;
    }
    const openedTrial = (await clickExactText('立即体验', 2500))
      || (await clickByKeywords(['立即体验'], 2500));
    if (openedTrial) {
      await Promise.race([
        waitForUrlContains('/main/writer/create', 5000),
        waitForPageSignal(['去写章节', '创建书本', '立即创建', '书本名称'], 5000),
      ]);
      if (window.location.href.includes('/main/writer/create') || includesAny(pageText(), ['立即创建', '书本名称'])) {
        return true;
      }
    }
    const openedCreateMenu = (await clickExactText('创建新书', 4000))
      || (await clickByKeywords(['创建新书'], 4000));
    if (!openedCreateMenu) {
      return false;
    }
    await waitForPageSignal(['去写章节', '创建书本', '书本信息未准备好'], 5000);
    const openedCreatePage = (await clickExactText('创建书本', 4000))
      || (await clickByKeywords(['创建书本'], 4000));
    if (!openedCreatePage) {
      return false;
    }
    await Promise.race([
      waitForUrlContains('/main/writer/create', 8000),
      waitForPageSignal(['创建作品', '立即创建', '书本名称'], 8000),
    ]);
    return window.location.href.includes('/main/writer/create') || includesAny(pageText(), ['创建作品', '立即创建', '书本名称']);
  }

  function setFanqieAudience(value) {
    const normalized = String(value || '').toLowerCase();
    const wanted = normalized === 'female' || normalized.includes('女') ? '0' : '1';
    const input = document.querySelector(`input[name="pindao"][value="${wanted}"]`);
    if (input instanceof HTMLInputElement) {
      input.checked = true;
      input.click();
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    return false;
  }

  async function selectFanqieTag(tagName) {
    const normalized = String(tagName || '').trim();
    if (!normalized) {
      return true;
    }
    const clicked = (await clickExactText(normalized, 2500))
      || (await clickByKeywords([normalized], 2500));
    await sleep(250);
    return clicked;
  }

  async function openFanqieTagModal() {
    const trigger = await waitForSelector(['.select-view', '.view-inner-wrap .select-view']);
    if (!trigger) {
      return false;
    }
    trigger.click();
    await waitForCondition(
      () => document.querySelector('.arco-modal.category-modal')
        || document.querySelector('.category-choose'),
      4000,
    );
    return Boolean(document.querySelector('.arco-modal.category-modal') || document.querySelector('.category-choose'));
  }

  async function activateFanqieTagTab(tabLabel) {
    const labels = Array.from(document.querySelectorAll('.arco-tabs-nav-tab, .arco-tabs-tab, .arco-tabs-header-title'))
      .filter((node) => String(node?.innerText || node?.textContent || '').trim() === tabLabel)
      .sort((left, right) => left.childElementCount - right.childElementCount);
    const target = labels[0];
    if (!(target instanceof HTMLElement)) {
      return false;
    }
    target.click();
    await sleep(250);
    return true;
  }

  async function selectFanqieTagInModal(tagName) {
    const normalized = String(tagName || '').trim();
    if (!normalized) {
      return true;
    }
    const modal = document.querySelector('.arco-modal.category-modal') || document.querySelector('.category-choose');
    const root = modal instanceof HTMLElement ? modal : document;
    const selectors = [
      '.category-choose-item-container',
      '.category-choose-item',
      '.category-choose-item-title',
      'button',
      '[role="button"]',
      'span',
      'div',
    ];
    for (const selector of selectors) {
      const nodes = Array.from(root.querySelectorAll(selector))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === normalized);
      const target = nodes
        .map((node) => node.closest?.('.category-choose-item-container, .category-choose-item') || node)
        .filter((node) => node instanceof HTMLElement)
        .sort((left, right) => left.childElementCount - right.childElementCount)[0];
      if (target instanceof HTMLElement) {
        target.click();
        await sleep(250);
        return true;
      }
    }
    return false;
  }

  async function confirmFanqieTagModal() {
    const dialog = document.querySelector('.arco-modal.category-modal') || document.querySelector('.category-choose');
    let confirmed = false;
    const waitClosed = () => waitForCondition(
      () => !document.querySelector('.arco-modal.category-modal')
        && !document.querySelector('.category-choose'),
      4000,
    );
    if (dialog instanceof HTMLElement) {
      const button = Array.from(dialog.querySelectorAll('button, [role="button"]'))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === '确认')
        .sort((left, right) => left.childElementCount - right.childElementCount)[0];
      if (button instanceof HTMLElement) {
        button.click();
        confirmed = true;
        await sleep(250);
        if (document.querySelector('.arco-modal.category-modal') || document.querySelector('.category-choose')) {
          button.focus();
          button.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter' }));
          button.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter' }));
          await sleep(250);
        }
      }
    }
    if (!confirmed) {
      confirmed = (await clickExactText('确认', 2500))
        || (await clickByKeywords(['确认'], 2500));
    }
    if (!confirmed) {
      return false;
    }
    let closed = await waitClosed();
    if (closed) {
      return true;
    }
    const closeButton = Array.from((dialog || document).querySelectorAll(
      '.arco-modal-close-icon, .arco-modal-close-btn, .close, [aria-label="Close"], [aria-label="关闭"], button',
    )).find((node) => {
      const text = String(node?.innerText || node?.textContent || '').trim();
      const className = String(node?.className || '');
      return className.includes('close') || text === '关闭' || text === '×';
    });
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
      await sleep(250);
      closed = await waitClosed();
      if (closed) {
        return true;
      }
    }
    document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Escape', code: 'Escape' }));
    document.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Escape', code: 'Escape' }));
    closed = await waitClosed();
    return Boolean(closed);
  }

  async function selectFanqieContestIfPresent() {
    const sectionText = pageText();
    if (!includesAny(sectionText, ['征文活动', '参赛', '活动'])) {
      return true;
    }
    const candidateSelectors = [
      '.essay-activity-item',
      '.essay-activity-item-info',
      '.activity-item',
      '.activity-card',
      '[class*="activity"]',
    ];
    for (const selector of candidateSelectors) {
      const items = Array.from(document.querySelectorAll(selector))
        .filter((node) => {
          const text = String(node?.innerText || node?.textContent || '').trim();
          return text && !['取消', '立即创建'].includes(text);
        });
      const target = items[0];
      if (target instanceof HTMLElement) {
        target.click();
        await sleep(300);
        return true;
      }
    }
    const textTargets = Array.from(document.querySelectorAll('div, span, label'))
      .filter((node) => String(node?.innerText || node?.textContent || '').includes('征文'))
      .sort((left, right) => left.childElementCount - right.childElementCount);
    const textTarget = textTargets[0];
    if (textTarget instanceof HTMLElement) {
      textTarget.click();
      await sleep(300);
    }
    return true;
  }

  async function selectFanqieTags(meta) {
    const opened = await openFanqieTagModal();
    if (!opened) {
      return false;
    }

    const groups = [
      { tab: '主分类', tags: [meta.primaryCategory] },
      { tab: '主题', tags: meta.themeTags || [] },
      { tab: '角色', tags: meta.roleTags || [] },
      { tab: '情节', tags: meta.plotTags || [] },
    ];

    for (const group of groups) {
      const tags = group.tags.filter(Boolean);
      if (!tags.length) {
        continue;
      }
      await activateFanqieTagTab(group.tab);
      for (const tag of tags) {
        await selectFanqieTagInModal(tag);
      }
    }
    const confirmed = await confirmFanqieTagModal();
    if (!confirmed) {
      return false;
    }
    const selectedText = String(document.querySelector('.select-view')?.innerText || document.querySelector('.view-inner-wrap')?.innerText || '').trim();
    return meta.primaryCategory ? selectedText.includes(meta.primaryCategory) : true;
  }

  async function createFanqieBook(bookName, rawMeta) {
    const ready = await ensureFanqieCreateBookPage();
    if (!ready) {
      return false;
    }
    const meta = normalizeFanqieBookMeta(rawMeta, bookName);
    await fillField(['input[placeholder="请输入作品名称"]'], bookName, '作品名称');
    setFanqieAudience(meta.audience);
    await sleep(250);
    const selectedTags = await selectFanqieTags(meta);
    if (!selectedTags) {
      throw makeUploadError('番茄创建书本时未能完成作品标签选择。', 'create-book-validation-failed');
    }

    const protagonist1 = meta.protagonistNames[0] || '';
    const protagonist2 = meta.protagonistNames[1] || '';
    if (protagonist1) {
      await fillField(['input[placeholder="请输入主角名1"]'], protagonist1, '主角名');
    }
    if (protagonist2) {
      await fillField(['input[placeholder="请输入主角名2"]'], protagonist2, '主角名');
    }
    await fillField(
      ['textarea[placeholder*="请输入50-500字以内的作品简介"]', 'textarea.serial-textarea', 'textarea'],
      meta.intro,
      '作品简介',
    );
    await selectFanqieContestIfPresent();
    const submitted = (await clickActionExactText('立即创建', 4000))
      || (await clickExactText('立即创建', 2000))
      || (await clickByKeywords(['立即创建'], 2000));
    if (!submitted) {
      throw makeUploadError('番茄创建书本时未找到立即创建按钮。', 'create-book-validation-failed');
    }
    await sleep(2000);
    const currentText = pageText();
    if (includesAny(currentText, ['已到达当日创建作品上限', '当日创建作品上限'])) {
      throw makeUploadError(
        '番茄当前账号已达到当日创建作品上限。',
        'create-book-rate-limited',
        { platform_reason: 'daily-create-limit' },
      );
    }
    if (includesAny(currentText, ['作品名称已存在', '请完善书本信息', '创建失败'])) {
      throw makeUploadError(
        '番茄创建书本未通过前端校验。',
        'create-book-validation-failed',
      );
    }
    await Promise.race([
      waitForUrlContains('/main/writer/book-info/', 12000),
      waitForPageSignal(['书籍信息待审核', '安全状态', '书号'], 12000),
    ]);
    if (!includesAny(pageText(), [bookName]) && !window.location.href.includes('/main/writer/book-info/')) {
      const text = pageText();
      if (includesAny(text, ['已到达当日创建作品上限', '当日创建作品上限'])) {
        throw makeUploadError(
          '番茄当前账号已达到当日创建作品上限。',
          'create-book-rate-limited',
          { platform_reason: 'daily-create-limit' },
        );
      }
      throw makeUploadError(
        '番茄创建书本未完成，页面没有进入书本详情或写作页。',
        'create-book-validation-failed',
      );
    }
    window.location.href = 'https://fanqienovel.com/main/writer/';
    return true;
  }

  async function ensureQidianCreateBookPage() {
    if (window.location.href.includes('/create-novel')) {
      return true;
    }
    const direct = document.querySelector('a.dashboard-create-book');
    if (direct instanceof HTMLElement) {
      direct.click();
    } else {
      const clicked = (await clickExactText('新建作品', 4000))
        || (await clickByKeywords(['新建作品', '创建作品'], 4000));
      if (!clicked) {
        return false;
      }
    }
    await Promise.race([
      waitForUrlContains('/create-novel', 8000),
      waitForPageSignal(['创建作品', '作品名(选填)', '发布站点'], 8000),
    ]);
    return window.location.href.includes('/create-novel')
      || includesAny(pageText(), ['创建作品', '作品名(选填)', '发布站点']);
  }

  function findQidianSiteDialog() {
    return Array.from(document.querySelectorAll('.ui-dialog'))
      .find((node) => String(node?.innerText || node?.textContent || '').includes('选择平台')) || null;
  }

  async function ensureQidianSiteDialog() {
    const existing = findQidianSiteDialog();
    if (existing) {
      return existing;
    }
    const trigger = document.querySelector('.jsSiteInfo') || document.querySelector('.write-form-li .write-input');
    if (trigger instanceof HTMLElement) {
      trigger.click();
      await sleep(400);
    }
    return waitForCondition(() => findQidianSiteDialog(), 4000);
  }

  function qidianSiteSelectionSummary() {
    const infoText = String(document.querySelector('.jsSiteInfo')?.innerText || '').trim();
    const siteFieldValue = String(document.querySelector('input[name="site"]')?.value || '').trim();
    return {
      infoText,
      siteFieldValue,
    };
  }

  function isQidianSiteSelected(audience) {
    const normalizedAudience = String(audience || 'male').toLowerCase().includes('female') ? '女生' : '男生';
    const { infoText, siteFieldValue } = qidianSiteSelectionSummary();
    if (infoText.includes('起点') && infoText.includes(normalizedAudience)) {
      return true;
    }
    return Boolean(siteFieldValue);
  }

  async function selectQidianPublishSite(audience) {
    const normalizedAudience = String(audience || 'male').toLowerCase().includes('female') ? '女生' : '男生';
    await waitForCondition(
      () => isQidianSiteSelected(audience) || Boolean(findQidianSiteDialog()),
      2000,
    );
    if (isQidianSiteSelected(audience)) {
      return true;
    }
    const dialog = await ensureQidianSiteDialog();
    if (!(dialog instanceof HTMLElement)) {
      return false;
    }
    const siteBlock = Array.from(dialog.querySelectorAll('div'))
      .find((node) => {
        const text = String(node?.innerText || node?.textContent || '').trim();
        return text.includes('起点') && text.includes('成神在起点');
      });
    if (!(siteBlock instanceof HTMLElement)) {
      return false;
    }
    siteBlock.click();
    await sleep(250);
    const genderButton = Array.from(siteBlock.querySelectorAll('.site-button-contain, div, span'))
      .find((node) => String(node?.innerText || node?.textContent || '').trim() === normalizedAudience);
    if (!(genderButton instanceof HTMLElement)) {
      return false;
    }
    genderButton.click();
    await sleep(250);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const confirmButton = dialog.querySelector('.jsSiteSure:not(.disabled), .jsSiteSure, .site-right-button, button');
      if (confirmButton instanceof HTMLElement) {
        confirmButton.click();
        await Promise.race([
          waitForCondition(() => isQidianSiteSelected(audience) || !findQidianSiteDialog(), 2500),
          sleep(500),
        ]);
      }
      if (isQidianSiteSelected(audience)) {
        break;
      }
      const dialogConfirm = await clickExactText('确定', 1200)
        || await clickByKeywords(['确定'], 1200);
      if (dialogConfirm) {
        await Promise.race([
          waitForCondition(() => isQidianSiteSelected(audience) || !findQidianSiteDialog(), 2500),
          sleep(500),
        ]);
        if (isQidianSiteSelected(audience)) {
          break;
        }
      }
    }
    await waitForCondition(() => isQidianSiteSelected(audience), 4000);
    return isQidianSiteSelected(audience);
  }

  async function waitForQidianCategoryReady(timeoutMs = 8000) {
    return waitForCondition(() => {
      const nativeCategory = document.querySelector('select[name="category"]');
      if (nativeCategory instanceof HTMLSelectElement) {
        const options = Array.from(nativeCategory.options)
          .map((option) => String(option.textContent || '').trim())
          .filter(Boolean);
        if (options.length) {
          return nativeCategory;
        }
      }
      const dropdownOptions = Array.from(document.querySelectorAll('.ui-select-datalist-li'))
        .map((node) => String(node?.innerText || node?.textContent || '').trim())
        .filter(Boolean);
      if (dropdownOptions.length) {
        return true;
      }
      return null;
    }, timeoutMs);
  }

  function setQidianSelectValue(selectNode, matcher) {
    if (!(selectNode instanceof HTMLSelectElement)) {
      return false;
    }
    const matchedOption = Array.from(selectNode.options)
      .find((option) => matcher(String(option.textContent || '').trim(), String(option.value || '').trim()));
    if (!matchedOption) {
      return false;
    }
    selectNode.value = matchedOption.value;
    selectNode.dispatchEvent(new Event('input', { bubbles: true }));
    selectNode.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function maybeSelectQidianSubcategory() {
    const subcategory = document.querySelector('select[name="subcategory"]');
    if (!(subcategory instanceof HTMLSelectElement)) {
      return true;
    }
    const realOptions = Array.from(subcategory.options)
      .map((option) => ({
        value: String(option.value || '').trim(),
        text: String(option.textContent || '').trim(),
      }))
      .filter((option) => option.value && option.text);
    if (!realOptions.length || String(subcategory.value || '').trim()) {
      return true;
    }
    subcategory.value = realOptions[0].value;
    subcategory.dispatchEvent(new Event('input', { bubbles: true }));
    subcategory.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(200);
    return true;
  }

  function isVisibleElement(node) {
    return node instanceof HTMLElement
      && Boolean(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
  }

  function findQidianDashboardBookEntry(bookName) {
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const items = Array.from(document.querySelectorAll('.g-prodution-item'));
    const matchedItem = bookName
      ? items.find((item) => textOf(item).includes(bookName))
      : (items[0] || null);
    if (!matchedItem) {
      return null;
    }
    const target = Array.from(matchedItem.querySelectorAll('a[href]'))
      .find((node) => {
        const text = textOf(node);
        const href = String(node.getAttribute('href') || '');
        return text.includes('去写作')
          || href.includes('/addType/')
          || href.includes('/chaptertmp/');
      });
    return {
      itemText: textOf(matchedItem),
      href: target ? String(target.getAttribute('href') || '') : '',
    };
  }

  async function waitForQidianDashboardBookEntry(bookName, timeoutMs = 8000) {
    return waitForCondition(() => {
      if (!window.location.href.includes('/portal/dashboard')) {
        return null;
      }
      const entry = findQidianDashboardBookEntry(bookName);
      if (entry && entry.href) {
        return entry;
      }
      return null;
    }, timeoutMs);
  }

  async function maybeFillQidianIntro(meta) {
    const introValue = String(meta?.intro || '').trim();
    if (!introValue) {
      return true;
    }
    const introField = document.querySelector('textarea[name="intro"]')
      || document.querySelector('textarea');
    if (!(introField instanceof HTMLTextAreaElement)) {
      return true;
    }
    fillNode(introField, introValue);
    await sleep(200);
    return true;
  }

  async function maybeChooseQidianPrecollect(enabled = false) {
    const targetText = enabled ? '是' : '否';
    const choice = Array.from(document.querySelectorAll('label, span, div'))
      .find((node) => isVisibleElement(node) && String(node?.innerText || node?.textContent || '').trim() === targetText);
    if (choice instanceof HTMLElement) {
      choice.click();
      await sleep(150);
    }
    return true;
  }

  async function maybeChooseQidianContestDefault() {
    const notJoin = Array.from(document.querySelectorAll('label, span, div, a'))
      .find((node) => isVisibleElement(node) && String(node?.innerText || node?.textContent || '').trim() === '不参加活动');
    if (notJoin instanceof HTMLElement) {
      notJoin.click();
      await sleep(150);
    }
    return true;
  }

  async function createQidianBook(bookName, rawMeta) {
    const meta = normalizeQidianBookMeta(rawMeta, bookName);
    const titleInput = await waitForSelector(['input[name="title"]', 'input[placeholder*="作品"]'], 8000);
    if (!(titleInput instanceof HTMLInputElement)) {
      throw makeUploadError('起点创建书本页未找到作品名输入框。', 'create-book-validation-failed');
    }
    fillNode(titleInput, String(bookName || '').trim());
    await sleep(250);
    const selectedSite = await selectQidianPublishSite(meta.audience);
    if (!selectedSite) {
      throw makeUploadError('起点创建书本时未能完成发布站点选择。', 'create-book-validation-failed');
    }
    await waitForQidianCategoryReady(8000);
    const categoryLabel = resolveQidianCategory(meta);
    const nativeCategory = document.querySelector('select[name="category"]');
    if (nativeCategory instanceof HTMLSelectElement) {
      const matched = setQidianSelectValue(
        nativeCategory,
        (text, value) => Boolean(value) && text === categoryLabel,
      );
      if (!matched) {
        throw makeUploadError('起点创建书本时未找到作品类型选项。', 'create-book-validation-failed');
      }
    } else {
      const categoryTrigger = document.querySelector('.write-form-li .ui-select-button');
      if (!(categoryTrigger instanceof HTMLElement)) {
        throw makeUploadError('起点创建书本时未找到作品类型选择器。', 'create-book-validation-failed');
      }
      categoryTrigger.click();
      await sleep(250);
      const categorySelected = await clickExactText(categoryLabel, 2500)
        || await clickByKeywords([categoryLabel], 2500);
      if (!categorySelected) {
        throw makeUploadError('起点创建书本时未能选中作品类型。', 'create-book-validation-failed');
      }
    }
    await sleep(250);
    await maybeSelectQidianSubcategory();
    await maybeFillQidianIntro(meta);
    await maybeChooseQidianPrecollect(false);
    await maybeChooseQidianContestDefault();
    const submit = document.querySelector('button.jSubmit, .jSubmit');
    if (!(submit instanceof HTMLElement)) {
      throw makeUploadError('起点创建书本时未找到创建按钮。', 'create-book-validation-failed');
    }
    submit.click();
    await sleep(1500);
    const text = pageText();
    if (includesAny(text, ['创建过于频繁', '今日创建次数过多', '达到创建上限'])) {
      throw makeUploadError(
        '起点当前账号已达到作品创建频率限制。',
        'create-book-rate-limited',
        { platform_reason: 'create-too-frequent' },
      );
    }
    if (includesAny(text, ['作品名不可用', '作品名已存在', '请填写', '请完善'])) {
      throw makeUploadError('起点创建书本未通过页面校验。', 'create-book-validation-failed');
    }
    const startWriting = await waitForCondition(() => {
      const button = Array.from(document.querySelectorAll('a, button, [role="button"]'))
        .find((node) => isVisibleElement(node) && String(node?.innerText || node?.textContent || '').trim() === '开始写作');
      return button || null;
    }, 10000);
    if (!(startWriting instanceof HTMLElement)) {
      const createdText = pageText();
      if (includesAny(createdText, ['创建成功', '新书创建成功', '作品创建成功'])) {
        window.location.href = 'https://write.qq.com/portal/dashboard';
        return false;
      }
      throw makeUploadError('起点创建书本后未出现开始写作入口。', 'chapter-editor-navigation-failed');
    }
    startWriting.click();
    return false;
  }

  async function dismissFanqieGuideModal() {
    const hasGuideArtifacts = () => {
      if (document.querySelector('button.guide-card-footer-btn') || document.querySelector('#___reactour')) {
        return true;
      }
      const guideNodes = Array.from(document.querySelectorAll(
        '#___reactour, .reactour__helper, .reactour__mask, [class*="reactour"], .publish-guide-desc, .publish-guide-mask, .publish-guide-card, [role="dialog"], .byte-modal-wrapper, .byte-modal-content, .arco-modal',
      ));
      return guideNodes.some((node) => {
        const text = String(node?.innerText || node?.textContent || '').trim();
        return text.includes('番茄原创平台全新上线')
          || text.includes('新增读者纠错功能')
          || text.includes('立即体验')
          || text.includes('我知道了')
          || text.includes('继续');
      });
    };

    if (!hasGuideArtifacts()) {
      return;
    }

    if (document.querySelector('button.guide-card-footer-btn') || document.querySelector('#___reactour')) {
      await dismissFanqieGuideSteps();
      if (!hasGuideArtifacts()) {
        return;
      }
    }

    const clickDismissAction = async () => {
      const labels = ['我知道了', '知道了', '关闭', '确定', '立即体验', '继续'];
      for (const label of labels) {
        const clicked = await clickActionExactText(label, 600, document)
          || await clickExactText(label, 600)
          || await clickByKeywords([label], 600);
        if (clicked) {
          await sleep(500);
          return true;
        }
      }
      return false;
    };

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const closeButton = document.querySelector('.byte-modal-close-icon, .arco-modal-close-icon, .byte-modal-close, .arco-modal-close-btn');
      if (closeButton instanceof HTMLElement) {
        closeButton.click();
        await sleep(500);
      } else {
        await clickDismissAction();
      }
      const stillVisible = Array.from(document.querySelectorAll('[role="dialog"], .byte-modal-wrapper, .byte-modal-content, .arco-modal'))
        .some((node) => {
          const text = String(node?.innerText || node?.textContent || '').trim();
          return text.includes('番茄原创平台全新上线')
            || text.includes('新增读者纠错功能')
            || text.includes('立即体验')
            || text.includes('我知道了');
        });
      if (!stillVisible) {
        break;
      }
    }
    const tourNodes = Array.from(document.querySelectorAll(
      '#___reactour, .reactour__helper, .reactour__mask, [class*="reactour"], .publish-guide-desc, .publish-guide-mask, .publish-guide-card',
    ));
    tourNodes.forEach((node) => node.remove());
    Array.from(document.querySelectorAll('.byte-modal-wrapper, .byte-modal-mask, .byte-modal-content, .arco-modal, .arco-modal-mask'))
      .filter((node) => {
        const text = String(node?.innerText || node?.textContent || '').trim();
        return text.includes('番茄原创平台全新上线')
          || text.includes('新增读者纠错功能');
      })
      .forEach((node) => node.remove());
    await dismissFanqieGuideSteps();
  }

  async function dismissFanqieGuideSteps(maxSteps = 8) {
    let steps = 0;
    while (steps < maxSteps) {
      const button = document.querySelector('button.guide-card-footer-btn');
      if (!(button instanceof HTMLElement)) {
        break;
      }
      const text = String(button.innerText || button.textContent || '').trim();
      if (!text) {
        break;
      }
      button.click();
      steps += 1;
      await sleep(350);
    }
  }

  async function fillFanqieTitle(value) {
    const selector = 'input[placeholder="请输入标题"]';
    const node = await waitForSelector([selector], 8000);
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    return fillInputExact(node, value, { perCharDelayMs: 40 });
  }

  async function fillFanqieSequence() {
    const node = document.querySelector('input.serial-input.byte-input.byte-input-size-default:not(.serial-editor-input-hint-area):not([placeholder])');
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    if (String(node.value || '').trim()) {
      return true;
    }
    return fillInputExact(node, '1', { perCharDelayMs: 10 });
  }

  function applyFanqieTrustedBody(value) {
    const text = String(value || '');
    const editor = document.querySelector('.ProseMirror[contenteditable="true"]');
    const target = document.querySelector('.ProseMirror[contenteditable="true"] p') || editor;
    if (!(target instanceof HTMLElement)) {
      return {
        ok: false,
        error: '未找到番茄正文编辑器。',
        editorStatus: readFanqieEditorStatus(),
      };
    }
    target.focus();
    try {
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      document.execCommand('delete', false, null);
      document.execCommand('insertText', false, text);
    } catch (_error) {
      if (editor instanceof HTMLElement) {
        editor.focus();
        editor.textContent = text;
      } else {
        target.textContent = text;
      }
    }
    target.dispatchEvent(new InputEvent('beforeinput', {
      bubbles: true,
      cancelable: true,
      data: text,
      inputType: 'insertText',
    }));
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    return {
      ok: true,
      currentUrl: window.location.href,
      editorStatus: readFanqieEditorStatus(),
    };
  }

  function readFanqieEditorStatus() {
    const text = pageText();
    const countMatch = text.match(/正文字数\s*(\d+)/);
    const nextButton =
      document.querySelector('button.publish-button.auto-editor-next')
      || Array.from(document.querySelectorAll('button')).find((node) => String(node?.innerText || node?.textContent || '').trim() === '下一步');
    const active = document.activeElement;
    const editor = document.querySelector('.ProseMirror[contenteditable="true"]');
    return {
      bodyCharCount: countMatch ? Number(countMatch[1]) : 0,
      nextDisabled: !nextButton
        || nextButton.hasAttribute('disabled')
        || String(nextButton.getAttribute('class') || '').includes('arco-btn-disabled'),
      activeTag: active ? String(active.tagName || '') : '',
      activeClass: active ? String(active.className || '') : '',
      editorTextLength: editor ? String(editor.innerText || '').length : 0,
      guideVisible: Boolean(document.querySelector('button.guide-card-footer-btn')),
    };
  }

  async function openFanqieDirectChapterEditor() {
    const openedCreateMenu = (await clickExactText('创建新书', 4000))
      || (await clickByKeywords(['创建新书'], 4000));
    if (!openedCreateMenu) {
      return false;
    }
    await waitForPageSignal(['去写章节', '去写作', '创建书本'], 5000);
    const openedEditor = (await clickExactText('去写章节', 4000))
      || (await clickByKeywords(['去写章节'], 4000));
    if (!openedEditor) {
      return false;
    }
    await Promise.race([
      waitForUrlContains('/publish/', 6000),
      waitForPageSignal(['存草稿', '下一步'], 6000),
      waitForEditorReady(6000),
    ]);
    return window.location.href.includes('/publish/') || includesAny(pageText(), ['存草稿', '下一步']) || Boolean(document.querySelector('.ProseMirror[contenteditable="true"]'));
  }

  function findFanqieBookEntryNode(bookName) {
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const items = Array.from(document.querySelectorAll('div, section, article, li'))
      .filter((node) => {
        const text = textOf(node);
        if (!text) {
          return false;
        }
        if (bookName && !text.includes(bookName)) {
          return false;
        }
        return Boolean(
          node.querySelector('a[href*="/publish/"]')
          || node.querySelector('a[href*="/chapter-manage/"]')
          || Array.from(node.querySelectorAll('button, [role="button"]')).find((button) => {
            const buttonText = textOf(button);
            return buttonText.includes('创建章节') || buttonText.includes('章节管理');
          }),
        );
      })
      .sort((left, right) => textOf(left).length - textOf(right).length);
    return items[0] || null;
  }

  function findFanqieBookEntry(bookName) {
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const normalize = (value) => {
      try {
        return decodeURIComponent(String(value || '').trim());
      } catch (_error) {
        return String(value || '').trim();
      }
    };
    const targetBookName = String(bookName || '').trim();
    const chapterManageAnchors = Array.from(document.querySelectorAll('a[href*="/chapter-manage/"]'));
    const matchedManageAnchor = targetBookName
      ? chapterManageAnchors.find((node) => {
        const href = normalize(node.getAttribute('href') || '');
        const text = `${textOf(node)} ${(node.parentElement && textOf(node.parentElement)) || ''}`.trim();
        return href.includes(targetBookName) || text.includes(targetBookName);
      })
      : (chapterManageAnchors[0] || null);
    if (matchedManageAnchor instanceof HTMLAnchorElement) {
      const chapterManageHref = String(matchedManageAnchor.getAttribute('href') || '');
      const workIdMatch = chapterManageHref.match(/\/chapter-manage\/(\d+)/);
      const workId = workIdMatch ? String(workIdMatch[1] || '').trim() : '';
      const publishAnchor = workId
        ? Array.from(document.querySelectorAll(`a[href*="/${workId}/publish/"]`)).find((node) => node instanceof HTMLAnchorElement) || null
        : null;
      return {
        bookText: `${textOf(matchedManageAnchor)} ${(matchedManageAnchor.parentElement && textOf(matchedManageAnchor.parentElement)) || ''}`.trim(),
        workId,
        publishHref: publishAnchor ? String(publishAnchor.getAttribute('href') || '') : '',
        publishAnchor,
        chapterManageHref,
      };
    }
    const matchedItem = findFanqieBookEntryNode(bookName);
    if (!matchedItem) {
      return null;
    }
    const anchors = Array.from(matchedItem.querySelectorAll('a[href]'));
    const publishAnchor = anchors.find((node) => String(node.getAttribute('href') || '').includes('/publish/'));
    const chapterManageAnchor = anchors.find((node) => String(node.getAttribute('href') || '').includes('/chapter-manage/'));
    return {
      bookText: textOf(matchedItem),
      workId: '',
      publishHref: publishAnchor ? String(publishAnchor.getAttribute('href') || '') : '',
      publishAnchor: publishAnchor instanceof HTMLAnchorElement ? publishAnchor : null,
      chapterManageHref: chapterManageAnchor ? String(chapterManageAnchor.getAttribute('href') || '') : '',
    };
  }

  async function openFanqieBookEntryEditor(bookName) {
    await dismissFanqieGuideModal();
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const initialUrl = String(window.location.href || '');
    const waitForNavigation = async (timeoutMs = 2200) => {
      const navigated = await waitForCondition(() => {
        const href = String(window.location.href || '');
        if (href !== initialUrl && href.includes('/publish/')) {
          return href;
        }
        if (includesAny(pageText(), ['存草稿', '下一步']) || document.querySelector('.ProseMirror[contenteditable="true"]')) {
          return href || initialUrl;
        }
        return null;
      }, timeoutMs);
      return Boolean(navigated);
    };
    const matchedEntry = findFanqieBookEntry(bookName);
    const matchedItem = findFanqieBookEntryNode(bookName);
    const absolutePublishHref = matchedEntry?.publishHref
      ? new URL(matchedEntry.publishHref, window.location.href).toString()
      : '';

    if (absolutePublishHref) {
      setDebugStep('fanqie-location-assign-direct', {
        workId: matchedEntry?.workId || '',
        targetUrl: absolutePublishHref,
      });
      window.location.assign(absolutePublishHref);
      return {
        opened: true,
        navigated: false,
        method: 'location-assign-direct',
        targetUrl: absolutePublishHref,
        workId: matchedEntry?.workId || '',
      };
    }
    if (matchedItem instanceof HTMLElement) {
      const createButton = Array.from(matchedItem.querySelectorAll('button, [role="button"]')).find((node) => {
        const text = textOf(node);
        return text === '创建章节' || text.includes('创建章节');
      });
      if (createButton instanceof HTMLElement) {
        setDebugStep('fanqie-click-create-button', {
          workId: matchedEntry?.workId || '',
          targetUrl: absolutePublishHref,
        });
        createButton.click();
        if (await waitForNavigation()) {
          setDebugStep('fanqie-create-button-navigated', {
            workId: matchedEntry?.workId || '',
            targetUrl: absolutePublishHref,
          });
          return {
            opened: true,
            navigated: true,
            method: 'create-button-click',
            targetUrl: absolutePublishHref,
            workId: matchedEntry?.workId || '',
          };
        }
        return {
          opened: true,
          navigated: false,
          method: 'create-button-click',
          targetUrl: absolutePublishHref,
          workId: matchedEntry?.workId || '',
        };
      }
      const publishAnchor = Array.from(matchedItem.querySelectorAll('a[href]')).find((node) => {
        const href = String(node.getAttribute('href') || '');
        return href.includes('/publish/');
      });
      if (publishAnchor instanceof HTMLAnchorElement) {
        setDebugStep('fanqie-click-matched-item-publish-anchor', {
          workId: matchedEntry?.workId || '',
          targetUrl: absolutePublishHref || new URL(String(publishAnchor.getAttribute('href') || ''), window.location.href).toString(),
        });
        publishAnchor.click();
        if (await waitForNavigation()) {
          setDebugStep('fanqie-matched-item-publish-anchor-navigated', {
            workId: matchedEntry?.workId || '',
            targetUrl: absolutePublishHref || new URL(String(publishAnchor.getAttribute('href') || ''), window.location.href).toString(),
          });
          return {
            opened: true,
            navigated: true,
            method: 'matched-item-publish-anchor-click',
            targetUrl: absolutePublishHref || new URL(String(publishAnchor.getAttribute('href') || ''), window.location.href).toString(),
            workId: matchedEntry?.workId || '',
          };
        }
      }
    }
    return {
      opened: false,
      navigated: false,
      method: 'not-found',
      targetUrl: '',
      workId: matchedEntry?.workId || '',
    };
  }

  async function waitForFanqieEditorNavigation(timeoutMs = 8000) {
    await Promise.race([
      waitForUrlContains('/publish/', timeoutMs),
      waitForPageSignal(['存草稿', '下一步'], timeoutMs),
      waitForEditorReady(timeoutMs),
    ]);
    return window.location.href.includes('/publish/')
      || includesAny(pageText(), ['存草稿', '下一步'])
      || Boolean(document.querySelector('.ProseMirror[contenteditable="true"]'));
  }

  async function preparePublishPage(bookName, payload = {}) {
    if (window.location.href.includes('write.qq.com')) {
      if (window.location.href.includes('/create-novel')) {
        if (Boolean(payload.create_if_missing) && bookName) {
          return createQidianBook(bookName, payload.book_meta || null);
        }
        window.location.href = 'https://write.qq.com/portal/dashboard';
        return false;
      } else if (window.location.href.includes('/portal/dashboard')) {
        const entry = await waitForQidianDashboardBookEntry(bookName, bookName ? 5000 : 1500);
        if (entry?.href) {
          window.location.href = new URL(entry.href, window.location.href).toString();
          return false;
        }
        if (Boolean(payload.create_if_missing) && bookName) {
          window.location.href = 'https://write.qq.com/portal/dashboard/create-novel?from=S5';
          return {
            pending: true,
            error: '起点正在跳转到创建作品页，请稍后重试。',
            errorCode: 'create-book-page-pending',
          };
        }
        if (bookName) {
          throw makeUploadError('起点当前账号下未找到对应作品。', 'chapter-editor-navigation-failed');
        }
      } else {
        const currentText = pageText();
        if (bookName && !currentText.includes(bookName)) {
          window.location.href = 'https://write.qq.com/portal/dashboard';
          return false;
        }
        if (
          window.location.href.includes('/chaptertmp/')
          || window.location.href.includes('/portal/booknovels/chaptertmp/')
          || window.location.href.includes('/addType/')
        ) {
          return true;
        }
      }
    }
    if (window.location.href.includes('fanqienovel.com')) {
      setDebugStep('fanqie-prepare-publish-start', {
        bookName: String(bookName || ''),
        createIfMissing: Boolean(payload.create_if_missing),
      });
      setDebugStep('fanqie-prepare-before-dismiss-guide', {
        url: String(window.location.href || ''),
      });
      await dismissFanqieGuideModal();
      setDebugStep('fanqie-prepare-after-dismiss-guide', {
        url: String(window.location.href || ''),
      });
      if (
        window.location.href.includes('/publish/')
        || includesAny(pageText(), ['存草稿', '下一步'])
        || Boolean(document.querySelector('.ProseMirror[contenteditable="true"]'))
      ) {
        setDebugStep('fanqie-prepare-editor-already-open', {
          url: String(window.location.href || ''),
        });
        return true;
      }
      const text = pageText();
      const entry = findFanqieBookEntry(bookName);
      const createIfMissing = Boolean(payload.create_if_missing);
      const bookMeta = payload.book_meta || null;
      if (
        window.location.href.includes('/main/writer/')
        && createIfMissing
        && bookName
        && !entry
      ) {
        setDebugStep('fanqie-create-book-missing-entry', { bookName: String(bookName || '') });
        await createFanqieBook(bookName, bookMeta);
        return false;
      }
      if (
        window.location.href.includes('/main/writer/')
        && entry
      ) {
        setDebugStep('fanqie-entry-found', {
          bookName: String(bookName || ''),
          workId: entry.workId || '',
          publishHref: entry.publishHref || '',
        });
        const openedEditor = await openFanqieBookEntryEditor(bookName);
        if (openedEditor?.opened) {
          if (openedEditor.navigated) {
            return true;
          }
          return {
            pending: true,
            currentUrl: window.location.href,
            error: '番茄正在跳转到章节编辑页，请稍后重试。',
            errorCode: 'editor-navigation-pending',
            resultPayload: {
              platform_stage: 'fanqie-entry-opened',
              fanqie_step: openedEditor.method,
              target_url: openedEditor.targetUrl || '',
              work_id: openedEditor.workId || '',
            },
          };
        }
        if (entry.publishHref) {
          const targetUrl = new URL(entry.publishHref, window.location.href).toString();
          window.location.assign(targetUrl);
          return {
            pending: true,
            currentUrl: window.location.href,
            error: '番茄正在跳转到章节编辑页，请稍后重试。',
            errorCode: 'editor-navigation-pending',
            resultPayload: {
              platform_stage: 'fanqie-entry-fallback-assign',
              fanqie_step: 'publish-href-fallback',
              target_url: targetUrl,
              work_id: entry.workId || '',
            },
          };
        }
        throw makeUploadError('番茄当前账号下未找到对应作品的创建章节入口。', 'chapter-editor-navigation-failed');
      }
      if (
        window.location.href.includes('/main/writer/')
        && bookName
        && text.includes(bookName)
        && text.includes('创建章节')
      ) {
        const openedEditor = (await clickExactText('创建章节', 4000))
          || (await clickByKeywords(['创建章节'], 4000));
        if (openedEditor) {
          return false;
        }
      }
      if (
        window.location.href.includes('/main/writer/')
        && text.includes('创建新书')
        && text.includes('去写作')
        && (!bookName || !text.includes(bookName))
      ) {
        if (createIfMissing) {
          await createFanqieBook(bookName, bookMeta);
          return false;
        }
        const canDirectWrite = await openFanqieDirectChapterEditor();
        if (canDirectWrite) {
          await dismissFanqieGuideModal();
          return true;
        }
      }
      if (hasFanqieEmptyWorkState()) {
        if (createIfMissing) {
          await createFanqieBook(bookName, bookMeta);
          return false;
        }
        const canDirectWrite = await openFanqieDirectChapterEditor();
        if (!canDirectWrite) {
          throw new Error('番茄当前账号还没有可上传的作品，请先在平台创建新书。');
        }
        await dismissFanqieGuideModal();
        return true;
      }
    }
    await clickByKeywords(['作品管理', '小说管理', '我的作品'], 3000);
    if (bookName) {
      await clickByKeywords([bookName], 3000);
    }
    const launchedEditor = await clickByKeywords(['写新章', '新建章节', '创建章节', '上传章节', '新增章节', '继续创作', '开始创作'], 5000);
    if (launchedEditor) {
      const navigated = await waitForFanqieEditorNavigation(6000);
      if (!navigated) {
        return false;
      }
    } else if (window.location.href.includes('fanqienovel.com')) {
      return false;
    }
    if (window.location.href.includes('fanqienovel.com') && hasFanqieEmptyWorkState()) {
      const canDirectWrite = await openFanqieDirectChapterEditor();
      if (!canDirectWrite) {
        throw new Error('番茄当前账号还没有可上传的作品，请先在平台创建新书。');
      }
      await dismissFanqieGuideModal();
    }
    return true;
  }

  async function prepareQidianNewChapter() {
    if (!window.location.href.includes('/chaptertmp/')) {
      return { ready: false, pending: false };
    }
    const clicked = (() => {
      const target = document.querySelector('a.ne-st-item[href="#ccid=new"]');
      if (!target) {
        return false;
      }
      target.click();
      return true;
    })();
    if (clicked) {
      await Promise.race([
        waitForEditorReady(1800),
        waitForPageSignal(['确定', '确认章节信息', '确认发布', '发布设置'], 1800),
        waitForUrlContains('#ccid=-1', 1800),
      ]);
      const sawConfirmPopup = includesAny(pageText(), ['确定', '确认章节信息', '确认发布', '发布设置']);
      if (sawConfirmPopup) {
        const confirmed = await clickExactTexts(['确定', '同意'], 2500)
          || await clickByKeywords(['确定', '同意'], 2500);
        if (confirmed) {
          await Promise.race([
            waitForEditorReady(5000),
            waitForPageSignal(['确认章节信息', '确认发布', '发布设置'], 5000),
            sleep(1200),
          ]);
        }
      }
      const editorReady = await waitForEditorReady(1800);
      const href = String(window.location.href || '');
      const titleInput = document.querySelector('#inputTitle');
      const titleValue = String(titleInput?.value || titleInput?.textContent || '').trim();
      if (!editorReady || (!href.includes('#ccid=-1') && titleValue)) {
        return {
          ready: false,
          pending: true,
          error: '起点正在切换到新章节编辑页，请稍后重试。',
          errorCode: 'editor-navigation-pending',
        };
      }
    }
    return { ready: true, pending: false };
  }

  async function confirmQidianPublishIfNeeded() {
    if (!window.location.href.includes('write.qq.com')) {
      return { handled: false, needsTrustedConfirm: false, target: null };
    }
    const findQidianTermsDialog = () => Array.from(document.querySelectorAll('.ui-dialog, [role="dialog"], .ui-popup'))
      .find((node) => String(node?.innerText || node?.textContent || '').includes('阅文集团作家创作须知')) || null;
    const findQidianPublishDialog = () => Array.from(document.querySelectorAll('.ui-dialog, [role="dialog"], .ui-popup'))
      .find((node) => {
        const text = String(node?.innerText || node?.textContent || '').trim();
        return text.includes('确认章节信息') || text.includes('确认发布');
      }) || null;
    const locateQidianPublishConfirmTarget = () => {
      const directButton = document.querySelector('button.btn-send-sure');
      const target = directButton instanceof HTMLElement
        ? directButton
        : Array.from(document.querySelectorAll('button, [role="button"], a, span, div'))
          .find((node) => buttonMatches(node, ['确认发布'])) || null;
      if (!(target instanceof HTMLElement)) {
        return null;
      }
      const rect = target.getBoundingClientRect();
      if (!rect.width && !rect.height) {
        return null;
      }
      return {
        x: Math.round(rect.left + (rect.width / 2)),
        y: Math.round(rect.top + (rect.height / 2)),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        selector: directButton instanceof HTMLElement ? 'button.btn-send-sure' : '',
      };
    };

    const ensureQidianTermsChecked = async () => {
      const dialog = findQidianTermsDialog();
      if (!(dialog instanceof HTMLElement)) {
        return false;
      }
      const checkbox = dialog.querySelector('input[type="checkbox"]');
      if (checkbox instanceof HTMLInputElement) {
        if (!checkbox.checked) {
          checkbox.click();
          checkbox.dispatchEvent(new Event('input', { bubbles: true }));
          checkbox.dispatchEvent(new Event('change', { bubbles: true }));
          await sleep(250);
        }
        return checkbox.checked;
      }
      const label = Array.from(dialog.querySelectorAll('label, span, div'))
        .find((node) => {
          const text = String(node?.innerText || node?.textContent || '').trim();
          return text.includes('我已阅读阅文集团作家创作须知')
            || text.includes('健康创作');
        });
      if (label instanceof HTMLElement) {
        label.click();
        await sleep(250);
        const retriedCheckbox = dialog.querySelector('input[type="checkbox"]');
        if (retriedCheckbox instanceof HTMLInputElement) {
          return retriedCheckbox.checked;
        }
        return true;
      }
      return false;
    };

    await waitForCondition(
      () => includesAny(pageText(), ['确认章节信息', '确认发布', '发布设置', '同意', '阅文集团作家创作须知'])
        || document.querySelector('button.btn-send-sure'),
      6000,
    );

    let handled = false;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const text = pageText();
      const termsDialog = findQidianTermsDialog();
      if (termsDialog instanceof HTMLElement || includesAny(text, ['阅文集团作家创作须知'])) {
        handled = true;
        await ensureQidianTermsChecked();
        const accepted = await clickExactTexts(['同意', '确定'], 2500)
          || await clickByKeywords(['同意', '确定'], 2500);
        if (accepted) {
          await Promise.race([
            waitForCondition(() => !findQidianTermsDialog(), 3000),
            waitForPageSignal(['确认章节信息', '确认发布', '发布设置', '审核中', '待审核'], 3000),
            sleep(1000),
          ]);
          continue;
        }
      }

      const directButton = document.querySelector('button.btn-send-sure');
      if (directButton instanceof HTMLElement) {
        handled = true;
        directButton.click();
        await sleep(1000);
        if (!findQidianPublishDialog()) {
          return { handled, needsTrustedConfirm: false, target: null };
        }
        continue;
      }

      const clicked = await clickExactTexts(['确认发布', '确定', '同意'], 2000)
        || await clickByKeywords(['确认发布', '确定', '同意'], 2000);
      if (clicked) {
        handled = true;
        await sleep(1000);
        if (!findQidianPublishDialog()) {
          return { handled, needsTrustedConfirm: false, target: null };
        }
        continue;
      }

      if (attempt === 0) {
        const republish = await clickExactText('发布', 1500)
          || await clickByKeywords(['发布'], 1500);
        if (republish) {
          handled = true;
          await sleep(1000);
          if (!findQidianPublishDialog()) {
            return { handled, needsTrustedConfirm: false, target: null };
          }
          continue;
        }
      }
      break;
    }
    const trustedTarget = locateQidianPublishConfirmTarget();
    return {
      handled,
      needsTrustedConfirm: Boolean(trustedTarget),
      target: trustedTarget,
    };
  }

  async function waitForQidianRealCcid(timeoutMs = 15000) {
    return waitForCondition(() => {
      const href = String(window.location.href || '');
      if (href.includes('#ccid=') && !href.includes('#ccid=-1')) {
        return href;
      }
      return null;
    }, timeoutMs);
  }

  async function ensureQidianSavedDraftIfNeeded() {
    const href = String(window.location.href || '');
    if (href.includes('#ccid=') && !href.includes('#ccid=-1')) {
      return true;
    }
    const clicked = await clickExactTexts(['保存', '存草稿'], 3000)
      || await clickByKeywords(['保存', '存草稿'], 3000);
    if (!clicked) {
      return false;
    }
    await Promise.race([
      waitForPageSignal(['保存成功', '已保存'], 5000),
      sleep(800),
    ]);
    return Boolean(await waitForQidianRealCcid(15000));
  }

  function fanqieModalTexts() {
    return Array.from(document.querySelectorAll('.arco-modal-wrapper, .byte-modal, .semi-modal, .auxo-modal, [role="dialog"]'))
      .map((node) => String(node?.innerText || node?.textContent || '').trim())
      .filter(Boolean);
  }

  function hasFanqieRiskDetectionModal() {
    return fanqieModalTexts().some((text) => text.includes('是否进行内容风险检测'));
  }

  function hasFanqiePublishSettings() {
    return fanqieModalTexts().some((text) => includesAny(text, ['发布设置', '非AI', 'AI']))
      || includesAny(pageText(), ['发布设置', '非AI', 'AI']);
  }

  function findFanqiePublishSettingsModal() {
    return document.querySelector('.publish-confirm-container-new')
      || Array.from(document.querySelectorAll('.arco-modal, [role="dialog"]'))
        .find((node) => String(node?.innerText || node?.textContent || '').includes('发布设置'))
      || null;
  }

  async function chooseFanqieNonAiIfPresent() {
    const modal = await waitForCondition(() => findFanqiePublishSettingsModal(), 4000);
    if (!(modal instanceof HTMLElement)) {
      return false;
    }
    const noInput = modal.querySelector('input[type="radio"][value="2"]');
    const yesInput = modal.querySelector('input[type="radio"][value="1"]');
    const radioLabels = Array.from(modal.querySelectorAll('label.arco-radio'));
    const noLabel = radioLabels.find((node) => String(node?.innerText || node?.textContent || '').trim() === '否') || null;
    const clickTarget = noLabel instanceof HTMLElement
      ? noLabel
      : (noInput instanceof HTMLElement ? noInput : null);
    if (!(clickTarget instanceof HTMLElement)) {
      return false;
    }
    clickTarget.click();
    if (noInput instanceof HTMLInputElement) {
      noInput.dispatchEvent(new Event('input', { bubbles: true }));
      noInput.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await sleep(400);
    const selected = await waitForCondition(() => {
      if (!(noInput instanceof HTMLInputElement)) {
        return null;
      }
      return noInput.checked === true && (!(yesInput instanceof HTMLInputElement) || yesInput.checked === false);
    }, 2000);
    return Boolean(selected);
  }

  async function submitFanqiePublishSettings() {
    const modal = await waitForCondition(() => findFanqiePublishSettingsModal(), 4000);
    if (!(modal instanceof HTMLElement)) {
      return false;
    }
    const primary = modal.querySelector('.arco-modal-footer button.arco-btn-primary')
      || Array.from(modal.querySelectorAll('button, [role="button"]'))
        .find((node) => String(node?.innerText || node?.textContent || '').trim() === '确认发布')
      || null;
    if (!(primary instanceof HTMLElement)) {
      return false;
    }
    primary.click();
    await sleep(1200);
    const closed = await waitForCondition(() => !findFanqiePublishSettingsModal(), 4000);
    return Boolean(closed);
  }

  async function handleFanqieRiskDetectionModal() {
    if (!hasFanqieRiskDetectionModal()) {
      return false;
    }
    const cancelled = await clickExactTexts(['取消'], 2000)
      || await clickByKeywords(['取消'], 2000);
    if (!cancelled) {
      return false;
    }
    await waitForCondition(() => !hasFanqieRiskDetectionModal(), 4000);
    await sleep(500);
    return true;
  }

  async function waitForFanqiePublishSettings(timeoutMs = 5000) {
    return waitForCondition(() => {
      if (hasFanqiePublishSettings()) {
        return true;
      }
      if (includesAny(pageText(), ['确定', '下一步'])) {
        return 'intermediate-confirm';
      }
      return null;
    }, timeoutMs);
  }

  async function dismissFanqiePublishSettingsIfPresent() {
    if (!hasFanqiePublishSettings()) {
      return false;
    }
    const closed = await clickExactTexts(['取消', '关闭', '返回'], 2000)
      || await clickByKeywords(['取消', '关闭', '返回'], 2000);
    if (closed) {
      await sleep(600);
      return true;
    }
    const closeButton = Array.from(document.querySelectorAll(
      '.arco-modal-close-icon, .arco-modal-close-btn, .byte-modal-close-icon, .byte-modal-close, [aria-label="关闭"], [aria-label="Close"]',
    ))[0];
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
      await sleep(600);
      return true;
    }
    return false;
  }

  async function confirmFanqiePublishIfNeeded(publish) {
    if (!window.location.href.includes('fanqienovel.com')) {
      return false;
    }
    await dismissFanqieGuideModal();
    let signal = await waitForFanqiePublishSettings(4000);
    if (hasFanqieRiskDetectionModal()) {
      await handleFanqieRiskDetectionModal();
      signal = await waitForFanqiePublishSettings(5000);
    }
    let text = pageText();
    if (!hasFanqiePublishSettings() && signal === 'intermediate-confirm') {
      try {
        const focused = document.activeElement;
        if (focused instanceof HTMLElement) {
          focused.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
          focused.dispatchEvent(new KeyboardEvent('keyup', { key: 'Tab', bubbles: true }));
          focused.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
          focused.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
          await sleep(600);
          text = pageText();
        }
      } catch (_error) {
        // Ignore fallback keyboard failures.
      }
      if (!hasFanqiePublishSettings()) {
        await clickExactTexts(['确定', '我知道了', '继续', '下一步'], 2500)
          || await clickByKeywords(['确定', '我知道了', '继续', '下一步'], 2500);
        await sleep(800);
        await waitForFanqiePublishSettings(4000);
        text = pageText();
      }
    }
    if (!hasFanqiePublishSettings()) {
      return false;
    }
    setDebugStep('fanqie-publish-settings-open');
    const nonAiSelected = await chooseFanqieNonAiIfPresent();
    setDebugStep('fanqie-publish-settings-non-ai', { selected: nonAiSelected });
    const confirmed = await submitFanqiePublishSettings();
    setDebugStep('fanqie-publish-settings-confirmed', { confirmed });
    if (!confirmed && !publish) {
      return false;
    }
    return confirmed;
  }

  function resolveOfficialStatus(text) {
    if (includesAny(text, ['已发布'])) {
      return 'published';
    }
    if (includesAny(text, ['审核中', '待审核', '提交审核'])) {
      return 'review-pending';
    }
    return '';
  }

  async function verifyPublishOutcome(platform, chapterTitle, publish) {
    if (!publish) {
      if (platform === 'fanqie') {
        return buildFanqieDraftVerifyRequest(chapterTitle);
      }
      return {
        ok: true,
        currentUrl: window.location.href,
        message: '章节草稿保存动作已提交。',
        resultPayload: { mode: 'draft' },
      };
    }
    await sleep(1200);
    const text = pageText();
    const officialStatus = resolveOfficialStatus(text);
    if (officialStatus) {
      return {
        ok: true,
        currentUrl: window.location.href,
        message: officialStatus === 'published' ? '章节已发布。' : '章节已进入平台审核。',
        resultPayload: {
          mode: 'publish',
          official_status: officialStatus,
        },
      };
    }
    if (
      platform === 'qidian'
      && String(window.location.href || '').includes('#ccid=')
      && !String(window.location.href || '').includes('#ccid=-1')
      && !includesAny(text, ['确认章节信息', '确认发布', '保存失败', '请输入1~20000字以内的章节内容'])
    ) {
      const titleNode = document.querySelector('#inputTitle');
      const titleValue = String(titleNode?.value || titleNode?.textContent || '').trim();
      const titleMatched = titleValue ? titleValue === String(chapterTitle || '').trim() : text.includes(String(chapterTitle || '').trim());
      const wordCount = readQidianWordCount();
      if (titleMatched && wordCount > 0) {
        return {
          ok: true,
          currentUrl: window.location.href,
          message: '章节已提交至起点，等待平台审核。',
          resultPayload: {
            mode: 'publish',
            official_status: 'review-pending',
            verified_via: 'chapter-page',
            word_count: wordCount,
          },
        };
      }
    }
    return {
      ok: false,
      currentUrl: window.location.href,
      error: `${platform === 'qidian' ? '起点' : '番茄'}页面未确认章节发布结果。`,
      errorCode: 'publish-not-confirmed',
      resultPayload: {
        mode: 'publish',
        chapter_title: chapterTitle,
      },
    };
  }

  function readQidianWordCount() {
    const text = pageText();
    const matched = text.match(/本章字数[：:\s]*(\d+)/);
    if (!matched) {
      return 0;
    }
    const value = Number.parseInt(matched[1], 10);
    return Number.isFinite(value) ? value : 0;
  }

  async function runPageScript(scriptFactory, arg, timeoutMs = 3000) {
    return new Promise((resolve) => {
      const eventName = `forwin-page-script-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      let settled = false;
      const finish = (value) => {
        if (settled) {
          return;
        }
        settled = true;
        window.removeEventListener(eventName, onMessage);
        resolve(value);
      };
      const onMessage = (event) => finish(event.detail || null);
      window.addEventListener(eventName, onMessage, { once: true });
      const script = document.createElement('script');
      const payload = JSON.stringify(arg ?? null);
      script.textContent = `(() => {
        const eventName = ${JSON.stringify(eventName)};
        const arg = ${payload};
        let detail = null;
        try {
          detail = (${scriptFactory.toString()})(arg);
        } catch (error) {
          detail = { ok: false, reason: String(error && error.message || error) };
        }
        window.dispatchEvent(new CustomEvent(eventName, { detail }));
      })();`;
      (document.documentElement || document.head || document.body).appendChild(script);
      script.remove();
      window.setTimeout(() => finish(null), timeoutMs);
    });
  }

  async function fillQidianBody(value) {
    const frame = document.querySelector('iframe#mce_0_ifr');
    const textarea = document.querySelector('textarea#mce_0');
    const paragraphs = String(value || '')
      .split(/\n+/)
      .map((line) => line.trim())
      .filter(Boolean);
    const html = paragraphs
      .map((line) => `<p>${line.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</p>`)
      .join('');

    const pageWriteResult = await runPageScript((payload) => {
      const editor = window.tinymce?.activeEditor || window.tinyMCE?.activeEditor || null;
      if (!editor) {
        return { ok: false, reason: 'no-active-editor' };
      }
      const bodyHtml = Array.isArray(payload?.paragraphs) ? payload.paragraphs.join('') : '';
      editor.focus();
      editor.setContent(bodyHtml, { format: 'raw' });
      editor.fire('input');
      editor.fire('change');
      editor.fire('keyup');
      editor.nodeChanged();
      editor.save();
      const textareaNode = document.querySelector('textarea#mce_0');
      const frameNode = document.querySelector('iframe#mce_0_ifr');
      const frameDoc = frameNode?.contentDocument || frameNode?.contentWindow?.document || null;
      const frameBody = frameDoc?.body || null;
      if (frameBody) {
        frameBody.dispatchEvent(new Event('input', { bubbles: true }));
        frameBody.dispatchEvent(new Event('change', { bubbles: true }));
        frameBody.dispatchEvent(new KeyboardEvent('keyup', { key: 'a', bubbles: true }));
      }
      return {
        ok: true,
        textareaLength: textareaNode ? String(textareaNode.value || '').length : 0,
        frameTextLength: frameBody ? String(frameBody.innerText || frameBody.textContent || '').length : 0,
      };
    }, { paragraphs: html ? [html] : [] }, 3000);

    if (!frame) {
      return waitForCondition(() => readQidianWordCount() > 0, 4000);
    }
    const frameDoc = frame.contentDocument || frame.contentWindow?.document;
    const frameBody = frameDoc?.body;
    if (!frameBody) {
      return waitForCondition(() => readQidianWordCount() > 0, 4000);
    }
    frameBody.focus();
    if (!pageWriteResult?.ok) {
      frameBody.innerHTML = html || '<p><br data-mce-bogus="1"></p>';
    }
    frameBody.dispatchEvent(new Event('input', { bubbles: true }));
    frameBody.dispatchEvent(new Event('change', { bubbles: true }));
    frameBody.dispatchEvent(new KeyboardEvent('keyup', { key: 'a', bubbles: true }));
    if (textarea) {
      textarea.value = value;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      textarea.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const ready = await waitForCondition(() => readQidianWordCount() > 0, 5000);
    return Boolean(ready);
  }

  function getFanqieWordCount() {
    return readFanqieEditorStatus().bodyCharCount;
  }

  async function waitForFanqieSaved(timeoutMs = 15000) {
    return waitForCondition(() => {
      const text = pageText();
      if (text.includes('保存中')) {
        return null;
      }
      if (includesAny(text, ['已保存', '保存成功'])) {
        return true;
      }
      return null;
    }, timeoutMs);
  }

  function extractFanqieWorkId() {
    const href = String(window.location.href || '');
    const matched = href.match(/\/main\/writer\/(\d+)\/publish(?:\/|[?#]|$)/);
    return matched ? String(matched[1] || '').trim() : '';
  }

  async function ensureFanqieDraftTabVisible() {
    await dismissFanqieGuideModal();
    const hasDraftListSignal = () => {
      const text = pageText();
      const href = String(window.location.href || '');
      return href.includes('type=2')
        || text.includes('共') && text.includes('篇草稿')
        || text.includes('新建草稿');
    };
    if (hasDraftListSignal()) {
      return true;
    }
    const draftTab = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'))
      .find((node) => {
        const text = String(node?.innerText || node?.textContent || '').trim();
        if (text !== '草稿箱') {
          return false;
        }
        const clickable = node instanceof HTMLElement
          ? (node.closest?.('button, [role="button"], a, [tabindex]') || node)
          : null;
        return isVisibleElement(clickable);
      });
    const clickableDraftTab = draftTab instanceof HTMLElement
      ? (draftTab.closest?.('button, [role="button"], a, [tabindex]') || draftTab)
      : null;
    if (clickableDraftTab instanceof HTMLElement) {
      clickableDraftTab.click();
      await Promise.race([
        waitForCondition(() => hasDraftListSignal(), 5000),
        sleep(1200),
      ]);
      await dismissFanqieGuideModal();
    }
    return hasDraftListSignal();
  }

  async function verifyFanqieDraftSavedOnCurrentPage(chapterTitle) {
    await ensureFanqieDraftTabVisible();
    const text = pageText();
    const title = String(chapterTitle || '').trim();
    if (title && text.includes(title)) {
      return {
        ok: true,
        currentUrl: window.location.href,
        message: '章节草稿已进入番茄章节管理。',
        resultPayload: {
          mode: 'draft',
          official_status: 'drafted',
          verified_via: 'chapter-manage',
        },
      };
    }
    return {
      ok: false,
      currentUrl: window.location.href,
      error: '番茄章节管理页未找到新草稿。',
      errorCode: 'publish-not-confirmed',
      resultPayload: {
        mode: 'draft',
        chapter_title: chapterTitle,
        verify_phase: 'chapter-manage',
      },
    };
  }

  function buildFanqieDraftVerifyRequest(chapterTitle) {
    const workId = extractFanqieWorkId();
    if (!workId) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '番茄当前页面缺少作品 ID，无法核验草稿是否落盘。',
        errorCode: 'publish-not-confirmed',
        resultPayload: {
          mode: 'draft',
          chapter_title: chapterTitle,
          verify_phase: 'missing-work-id',
        },
      };
    }
    return {
      ok: false,
      currentUrl: window.location.href,
      error: '番茄需要跳转章节管理页核验草稿。',
      errorCode: 'fanqie-draft-verify-required',
        resultPayload: {
          mode: 'draft',
          chapter_title: chapterTitle,
          verify_phase: 'pending-chapter-manage',
          verify_url: `https://fanqienovel.com/main/writer/chapter-manage/${workId}?type=2`,
        },
      };
    }

  function locateFanqieTrustedBodyTarget() {
    const paragraph = document.querySelector('.ProseMirror[contenteditable="true"] p');
    const editor = document.querySelector('.ProseMirror[contenteditable="true"]');
    const target = paragraph || editor;
    if (!(target instanceof HTMLElement)) {
      return null;
    }
    const rect = target.getBoundingClientRect();
    return {
      x: Math.round(rect.left + Math.min(40, Math.max(12, rect.width / 2))),
      y: Math.round(rect.top + Math.min(36, Math.max(12, rect.height / 2))),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      selector: paragraph ? '.ProseMirror p' : '.ProseMirror',
      wordCount: getFanqieWordCount(),
    };
  }

  async function clickAction(publish) {
    if (window.location.href.includes('fanqienovel.com')) {
      const primaryNextButton = document.querySelector('button.publish-button.auto-editor-next');
      let clicked = false;
      if (primaryNextButton instanceof HTMLElement) {
        primaryNextButton.click();
        clicked = true;
      }
      if (!clicked) {
        clicked = await clickActionExactText('下一步', 5000)
          || await clickExactText('下一步', 2000)
          || await clickByKeywords(['下一步'], 2000);
      }
      if (!clicked) {
        throw new Error('未找到番茄下一步按钮。');
      }
      return;
    }
    const keywords = publish
      ? ['发布', '提交', '保存并发布', '发布章节', '提交审核']
      : ['保存', '存草稿', '保存草稿'];
    const clicked = await clickByKeywords(keywords, 12000);
    if (!clicked) {
      throw new Error(`未找到${publish ? '发布' : '保存草稿'}按钮。`);
    }
  }

  async function runUpload(payload) {
    setDebugStep('run-upload-start', {
      platform: String(payload?.platform || ''),
      bookName: String(payload?.book_name || ''),
      chapterTitle: String(payload?.chapter_title || ''),
      publish: Boolean(payload?.publish),
    });
    if (window.location.href.includes('login')) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '平台当前仍在登录页，请先完成扫码登录。',
      };
    }

    const ready = await preparePublishPage(payload.book_name, payload);
    if (ready && typeof ready === 'object' && ready.pending) {
      return {
        ok: false,
        currentUrl: ready.currentUrl || window.location.href,
        error: ready.error || '平台正在跳转，请稍后重试。',
        errorCode: ready.errorCode || 'editor-navigation-pending',
        resultPayload: ready.resultPayload || {},
      };
    }
    if (!ready) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '平台正在跳转到章节编辑页，请稍后重试。',
        errorCode: 'editor-navigation-pending',
      };
    }
    setDebugStep('run-upload-editor-ready-wait');
    await waitForEditorReady(10000);
    setDebugStep('run-upload-editor-ready');
    if (window.location.href.includes('write.qq.com')) {
      const qidianChapterReady = await prepareQidianNewChapter();
      if (qidianChapterReady?.pending) {
        return {
          ok: false,
          currentUrl: window.location.href,
          error: qidianChapterReady.error || '起点正在切换到新章节编辑页，请稍后重试。',
          errorCode: qidianChapterReady.errorCode || 'editor-navigation-pending',
        };
      }
      await fillField(['#inputTitle', ...selectorsForTitle()], payload.chapter_title, '章节标题');
      const filled = await fillQidianBody(payload.body);
      if (!filled) {
        await fillField(selectorsForBody(), payload.body, '正文内容');
      }
      if (payload.publish) {
        const saved = await ensureQidianSavedDraftIfNeeded();
        if (!saved) {
          return {
            ok: false,
            currentUrl: window.location.href,
            error: '起点新建章节未能先保存为真实草稿，无法继续发布。',
            errorCode: 'publish-not-confirmed',
            resultPayload: {
              mode: 'publish',
              chapter_title: payload.chapter_title,
              save_required: true,
            },
          };
        }
      }
    } else {
      setDebugStep('run-upload-fill-title-start');
      await fillField(selectorsForTitle(), payload.chapter_title, '章节标题');
      setDebugStep('run-upload-fill-title-done');
      if (window.location.href.includes('fanqienovel.com')) {
        if (!payload.trustedBodyDone) {
          setDebugStep('run-upload-fanqie-sequence-start');
          await fillFanqieSequence();
          setDebugStep('run-upload-fanqie-sequence-done');
          setDebugStep('run-upload-fanqie-title-exact-start');
          await fillFanqieTitle(payload.chapter_title);
          setDebugStep('run-upload-fanqie-title-exact-done');
          await dismissFanqieGuideModal();
          setDebugStep('run-upload-fanqie-trusted-body-required');
          return {
            ok: false,
            currentUrl: window.location.href,
            error: '番茄正文需要可信输入。',
            errorCode: 'trusted-body-input-required',
            trustedBodyTarget: locateFanqieTrustedBodyTarget(),
            editorStatus: readFanqieEditorStatus(),
          };
        }
        await dismissFanqieGuideModal();
        setDebugStep('run-upload-fanqie-editor-status-read');
        const status = readFanqieEditorStatus();
        const wordCount = status.bodyCharCount;
        if (wordCount <= 0) {
          setDebugStep('run-upload-fanqie-editor-status-empty', status);
          return {
            ok: false,
            currentUrl: window.location.href,
            error: '番茄正文尚未真正写入编辑器状态。',
            errorCode: 'trusted-body-input-missing',
            trustedBodyTarget: locateFanqieTrustedBodyTarget(),
            editorStatus: status,
          };
        }
        setDebugStep('run-upload-fanqie-sequence-start-2');
        await fillFanqieSequence();
        setDebugStep('run-upload-fanqie-sequence-done-2');
        setDebugStep('run-upload-fanqie-title-exact-start-2');
        await fillFanqieTitle(payload.chapter_title);
        setDebugStep('run-upload-fanqie-title-exact-done-2');
        setDebugStep('run-upload-fanqie-save-wait-start');
        await waitForFanqieSaved(20000);
        setDebugStep('run-upload-fanqie-save-wait-done');
      }
    }
    if (!(window.location.href.includes('write.qq.com') && payload.publish && payload.trustedPublishDone)) {
      setDebugStep('run-upload-click-action-start', { publish: Boolean(payload.publish) });
      await clickAction(Boolean(payload.publish));
      setDebugStep('run-upload-click-action-done', { publish: Boolean(payload.publish) });
      if (window.location.href.includes('fanqienovel.com')) {
        setDebugStep('run-upload-fanqie-confirm-start', { publish: Boolean(payload.publish) });
        await confirmFanqiePublishIfNeeded(Boolean(payload.publish));
        setDebugStep('run-upload-fanqie-confirm-done', { publish: Boolean(payload.publish) });
      }
      if (window.location.href.includes('write.qq.com') && payload.publish) {
        const qidianConfirm = await confirmQidianPublishIfNeeded();
        if (qidianConfirm?.needsTrustedConfirm) {
          return {
            ok: false,
            currentUrl: window.location.href,
            error: '起点最终确认发布需要可信点击。',
            errorCode: 'trusted-qidian-confirm-required',
            trustedConfirmTarget: qidianConfirm.target,
            resultPayload: {
              mode: 'publish',
              chapter_title: payload.chapter_title,
            },
          };
        }
      }
    }
    setDebugStep('run-upload-post-action-wait-start');
    await Promise.race([
      waitForPageSignal(['保存成功', '已保存', '发布成功', '提交成功', '审核中'], 4000),
      waitForCondition(() => !window.location.href.includes('login'), 4000),
      sleep(800),
    ]);
    setDebugStep('run-upload-verify-start');
    return verifyPublishOutcome(
      window.location.href.includes('write.qq.com') ? 'qidian' : 'fanqie',
      payload.chapter_title,
      Boolean(payload.publish),
    );
  }

  runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.channel !== CHANNEL) {
      return;
    }
    if (message.action === 'inspect-login-state') {
      sendResponse(inspectLoginState());
      return false;
    }
    if (message.action === 'inspect-fanqie-editor-state') {
      sendResponse({
        ok: true,
        currentUrl: window.location.href,
        wordCount: getFanqieWordCount(),
        trustedBodyTarget: locateFanqieTrustedBodyTarget(),
        editorStatus: readFanqieEditorStatus(),
      });
      return false;
    }
    if (message.action === 'inspect-platform-agent-debug') {
      sendResponse({
        ok: true,
        currentUrl: window.location.href,
        debug: globalThis.__FORWIN_PLATFORM_AGENT_DEBUG__ || null,
      });
      return false;
    }
    if (message.action === 'verify-fanqie-draft') {
      verifyFanqieDraftSavedOnCurrentPage(message.payload?.chapterTitle || '')
        .then(sendResponse)
        .catch((error) => {
          sendResponse({
            ok: false,
            currentUrl: window.location.href,
            error: error instanceof Error ? error.message : String(error),
            errorCode: error instanceof Error ? String(error.code || '') : '',
            resultPayload: error instanceof Error && error.resultPayload && typeof error.resultPayload === 'object'
              ? error.resultPayload
              : {},
          });
        });
      return true;
    }
    if (message.action === 'apply-fanqie-trusted-body') {
      sendResponse(applyFanqieTrustedBody(message.payload?.body || ''));
      return false;
    }
    if (message.action === 'run-comment-sync') {
      runCommentSync(message.payload || {})
        .then(sendResponse)
        .catch((error) => {
          sendResponse({
            ok: false,
            currentUrl: window.location.href,
            error: error instanceof Error ? error.message : String(error),
            errorCode: error instanceof Error ? String(error.code || '') : '',
            resultPayload: error instanceof Error && error.resultPayload && typeof error.resultPayload === 'object'
              ? error.resultPayload
              : {},
          });
        });
      return true;
    }
    if (message.action === 'run-upload') {
      runUpload(message.payload || {})
        .then(sendResponse)
        .catch((error) => {
          sendResponse({
            ok: false,
            currentUrl: window.location.href,
            error: error instanceof Error ? error.message : String(error),
            errorCode: error instanceof Error ? String(error.code || '') : '',
            resultPayload: error instanceof Error && error.resultPayload && typeof error.resultPayload === 'object'
              ? error.resultPayload
              : {},
          });
        });
      return true;
    }
    return true;
  });

  window.addEventListener('pageshow', announceReady);
  window.addEventListener('popstate', announceReady);
  window.addEventListener('hashchange', announceReady);
  markPlatformAgentState('data-forwin-platform-agent-ready', '1');
  announceReady();
})();
