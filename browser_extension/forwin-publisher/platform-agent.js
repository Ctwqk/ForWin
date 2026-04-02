(function () {
  const CHANNEL = globalThis.__FORWIN_CHANNELS__?.PLATFORM_AGENT_CHANNEL || 'forwin-publisher-platform-agent';
  const runtime = (globalThis.browser && globalThis.browser.runtime) || (globalThis.chrome && globalThis.chrome.runtime);
  if (!runtime) {
    return;
  }

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

  function includesAny(text, keywords) {
    return keywords.some((keyword) => text.includes(keyword));
  }

  function inspectLoginState() {
    const url = window.location.href;
    const text = pageText();

    if (url.includes('write.qq.com')) {
      const loginVisible = includesAny(text, [
        '扫码登录',
        '微信扫码',
        '手机扫码',
        '账号登录',
        '验证码登录',
      ]);
      const authenticated = !loginVisible && includesAny(text, [
        '作品管理',
        '作家专区',
        '章节管理',
        '写新章',
        '新建章节',
        '数据概览',
      ]);
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
      node.value = value;
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }
    node.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
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

  function hasFanqieEmptyWorkState() {
    const text = pageText();
    return includesAny(text, [
      '助力开书第一笔',
      '书本信息未准备好',
      '先发布章节再补充',
      '创建书本',
    ]);
  }

  async function dismissFanqieGuideModal() {
    const closeButton = document.querySelector('.byte-modal-close-icon');
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
      await waitForCondition(
        () => !document.querySelector('.byte-modal-close-icon'),
        2500,
      );
    }
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

  function findFanqieBookEntry(bookName) {
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
    const matchedItem = items.find((item) => {
      const text = textOf(item);
      return bookName ? text.includes(bookName) : true;
    }) || items[0];
    if (!matchedItem) {
      return null;
    }
    const anchors = Array.from(matchedItem.querySelectorAll('a[href]'));
    const publishAnchor = anchors.find((node) => String(node.getAttribute('href') || '').includes('/publish/'));
    const chapterManageAnchor = anchors.find((node) => String(node.getAttribute('href') || '').includes('/chapter-manage/'));
    return {
      bookText: textOf(matchedItem),
      publishHref: publishAnchor ? String(publishAnchor.getAttribute('href') || '') : '',
      chapterManageHref: chapterManageAnchor ? String(chapterManageAnchor.getAttribute('href') || '') : '',
    };
  }

  async function preparePublishPage(bookName) {
    if (window.location.href.includes('write.qq.com')) {
      const items = Array.from(document.querySelectorAll('.g-prodution-item'));
      const matchedItem = items.find((item) => {
        const text = String(item?.innerText || item?.textContent || '');
        return bookName ? text.includes(bookName) : true;
      }) || items[0];
      const targetHref = matchedItem
        ? Array.from(matchedItem.querySelectorAll('a')).find((node) => {
          const text = String(node?.innerText || node?.textContent || '').trim();
          const href = String(node?.getAttribute('href') || '');
          return text.includes('去写作') || href.includes('/addType/');
        })?.getAttribute('href')
        : '';
      if (targetHref) {
        window.location.href = new URL(targetHref, window.location.href).toString();
        return false;
      }
    }
    if (window.location.href.includes('fanqienovel.com')) {
      await dismissFanqieGuideModal();
      const text = pageText();
      const entry = findFanqieBookEntry(bookName);
      if (
        window.location.href.includes('/main/writer/')
        && entry?.publishHref
      ) {
        window.location.href = new URL(entry.publishHref, window.location.href).toString();
        return false;
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
          await Promise.race([
            waitForUrlContains('/publish/', 6000),
            waitForPageSignal(['存草稿', '下一步'], 6000),
            waitForEditorReady(6000),
          ]);
          await dismissFanqieGuideModal();
          if (window.location.href.includes('/publish/') || includesAny(pageText(), ['存草稿', '下一步'])) {
            return true;
          }
        }
      }
      if (
        window.location.href.includes('/main/writer/')
        && text.includes('创建新书')
        && text.includes('去写作')
        && (!bookName || !text.includes(bookName))
      ) {
        const canDirectWrite = await openFanqieDirectChapterEditor();
        if (canDirectWrite) {
          await dismissFanqieGuideModal();
          return true;
        }
      }
      if (hasFanqieEmptyWorkState()) {
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
    await clickByKeywords(['写新章', '新建章节', '创建章节', '上传章节', '新增章节', '继续创作', '开始创作'], 5000);
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
      return false;
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
      await waitForEditorReady(5000);
    }
    return clicked;
  }

  async function fillQidianBody(value) {
    const frame = document.querySelector('iframe#mce_0_ifr');
    const textarea = document.querySelector('textarea#mce_0');
    if (!frame) {
      return false;
    }
    const frameDoc = frame.contentDocument || frame.contentWindow?.document;
    const frameBody = frameDoc?.body;
    if (!frameBody) {
      return false;
    }
    frameBody.focus();
    frameBody.replaceChildren();
    frameBody.textContent = value;
    frameBody.dispatchEvent(new Event('input', { bubbles: true }));
    frameBody.dispatchEvent(new Event('change', { bubbles: true }));
    if (textarea) {
      textarea.value = value;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      textarea.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
  }

  async function fillFanqieBody(value) {
    const editor = document.querySelector('.ProseMirror[contenteditable="true"]');
    if (!(editor instanceof HTMLElement)) {
      return false;
    }
    const lines = String(value || '').split(/\n/);
    const normalizedLines = lines.length ? lines : [''];
    const fragment = document.createDocumentFragment();
    normalizedLines.forEach((line) => {
      const paragraph = document.createElement('p');
      if (line) {
        paragraph.textContent = line;
      } else {
        paragraph.appendChild(document.createElement('br'));
      }
      fragment.appendChild(paragraph);
    });
    editor.focus();
    editor.replaceChildren(fragment);
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function clickAction(publish) {
    const keywords = publish
      ? ['发布', '提交', '保存并发布', '发布章节', '提交审核']
      : ['保存', '存草稿', '保存草稿'];
    const clicked = await clickByKeywords(keywords, 12000);
    if (!clicked) {
      throw new Error(`未找到${publish ? '发布' : '保存草稿'}按钮。`);
    }
  }

  async function runUpload(payload) {
    if (window.location.href.includes('login')) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '平台当前仍在登录页，请先完成扫码登录。',
      };
    }

    const ready = await preparePublishPage(payload.book_name);
    if (!ready) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: '平台正在跳转到章节编辑页，请稍后重试。',
        errorCode: 'editor-navigation-pending',
      };
    }
    await waitForEditorReady(10000);
    if (window.location.href.includes('write.qq.com')) {
      await prepareQidianNewChapter();
      await fillField(['#inputTitle', ...selectorsForTitle()], payload.chapter_title, '章节标题');
      const filled = await fillQidianBody(payload.body);
      if (!filled) {
        await fillField(selectorsForBody(), payload.body, '正文内容');
      }
    } else {
      await fillField(selectorsForTitle(), payload.chapter_title, '章节标题');
      const filled = window.location.href.includes('fanqienovel.com')
        ? await fillFanqieBody(payload.body)
        : false;
      if (!filled) {
        await fillField(selectorsForBody(), payload.body, '正文内容');
      }
    }
    await clickAction(Boolean(payload.publish));
    await Promise.race([
      waitForPageSignal(['保存成功', '已保存', '发布成功', '提交成功', '审核中'], 4000),
      waitForCondition(() => !window.location.href.includes('login'), 4000),
      sleep(800),
    ]);

    return {
      ok: true,
      currentUrl: window.location.href,
      message: payload.publish ? '章节发布动作已提交。' : '章节草稿保存动作已提交。',
      resultPayload: {
        mode: payload.publish ? 'publish' : 'draft',
      },
    };
  }

  runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.channel !== CHANNEL) {
      return;
    }
    if (message.action === 'inspect-login-state') {
      sendResponse(inspectLoginState());
      return false;
    }
    if (message.action === 'run-upload') {
      runUpload(message.payload || {})
        .then(sendResponse)
        .catch((error) => {
          sendResponse({
            ok: false,
            currentUrl: window.location.href,
            error: error instanceof Error ? error.message : String(error),
          });
        });
      return true;
    }
    return true;
  });

  window.addEventListener('pageshow', announceReady);
  window.addEventListener('popstate', announceReady);
  window.addEventListener('hashchange', announceReady);
  announceReady();
})();
