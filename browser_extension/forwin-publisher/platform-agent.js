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
    const protagonistNames = Array.isArray(meta.protagonist_names)
      ? meta.protagonist_names.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 2)
      : [];
    const intro = String(meta.intro || '').trim();
    return {
      audience: String(meta.audience || 'male').trim().toLowerCase(),
      primaryCategory: String(meta.primary_category || '').trim(),
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
      intro: intro || `${String(bookName || '').trim()}讲的是普通人被旧日悬案卷入后，在现实缝隙里一步步逼近真相的故事。`,
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
    const selectors = [
      '.category-choose-item-title',
      '.category-choose-item-container',
      '.category-choose-item',
      'button',
      '[role="button"]',
      'span',
      'div',
    ];
    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector))
        .filter((node) => String(node?.innerText || node?.textContent || '').trim() === normalized)
        .sort((left, right) => left.childElementCount - right.childElementCount);
      const target = nodes[0];
      if (target instanceof HTMLElement) {
        target.click();
        await sleep(250);
        return true;
      }
    }
    return false;
  }

  async function confirmFanqieTagModal() {
    const confirmed = (await clickExactText('确认', 2500))
      || (await clickByKeywords(['确认'], 2500));
    if (!confirmed) {
      return false;
    }
    await waitForCondition(
      () => !document.querySelector('.arco-modal.category-modal'),
      4000,
    );
    return true;
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
    const submitted = (await clickExactText('立即创建', 4000))
      || (await clickByKeywords(['立即创建'], 4000));
    if (!submitted) {
      throw makeUploadError('番茄创建书本时未找到立即创建按钮。', 'create-book-validation-failed');
    }
    await sleep(2000);
    const currentText = pageText();
    if (includesAny(currentText, ['已到达当日创建作品上限', '当日创建作品上限', '无法继续发布'])) {
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
      if (includesAny(text, ['已到达当日创建作品上限', '当日创建作品上限', '无法继续发布'])) {
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

  async function selectQidianPublishSite(audience) {
    const normalizedAudience = String(audience || 'male').toLowerCase().includes('female') ? '女生' : '男生';
    await waitForCondition(() => {
      const siteValue = String(document.querySelector('input[name="site"]')?.value || '').trim();
      const text = pageText();
      return Boolean(siteValue) || text.includes('起点');
    }, 2000);
    const infoText = String(document.querySelector('.jsSiteInfo')?.innerText || '').trim();
    const siteFieldValue = String(document.querySelector('input[name="site"]')?.value || '').trim();
    const visibleText = pageText();
    if (
      (infoText.includes('起点') && infoText.includes(normalizedAudience))
      || Boolean(siteFieldValue)
      || (visibleText.includes('起点') && visibleText.includes(normalizedAudience))
    ) {
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
    let confirmed = false;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const confirmButton = dialog.querySelector('.jsSiteSure:not(.disabled), .jsSiteSure, .site-right-button, button');
      if (!(confirmButton instanceof HTMLElement)) {
        break;
      }
      confirmButton.click();
      await Promise.race([
        waitForCondition(() => !findQidianSiteDialog(), 2500),
        sleep(500),
      ]);
      if (!findQidianSiteDialog()) {
        confirmed = true;
        break;
      }
      const dialogConfirm = await clickExactText('确定', 1200)
        || await clickByKeywords(['确定'], 1200);
      if (dialogConfirm) {
        await Promise.race([
          waitForCondition(() => !findQidianSiteDialog(), 2500),
          sleep(500),
        ]);
        if (!findQidianSiteDialog()) {
          confirmed = true;
          break;
        }
      }
    }
    if (!confirmed) {
      return false;
    }
    const updated = String(document.querySelector('.jsSiteInfo')?.innerText || '').trim();
    return (updated.includes('起点') && updated.includes(normalizedAudience))
      || (visibleText.includes('起点') && visibleText.includes(normalizedAudience));
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
    const closeButton = document.querySelector('.byte-modal-close-icon');
    if (closeButton instanceof HTMLElement) {
      closeButton.click();
      await waitForCondition(
        () => !document.querySelector('.byte-modal-close-icon'),
        2500,
      );
    }
    const tourNodes = Array.from(document.querySelectorAll(
      '#___reactour, .reactour__helper, .reactour__mask, [class*="reactour"], .publish-guide-desc, .publish-guide-mask, .publish-guide-card',
    ));
    tourNodes.forEach((node) => node.remove());
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
    const normalizedValue = String(value || '');
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    node.click();
    node.value = normalizedValue;
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(250);
    if (String(node.value || '').trim() === normalizedValue.trim()) {
      return true;
    }
    node.click();
    try {
      document.execCommand('selectAll', false, null);
    } catch (_error) {
      // Ignore when the browser blocks execCommand.
    }
    node.value = '';
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
    node.focus();
    node.value = normalizedValue;
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(250);
    return String(node.value || '').trim() === normalizedValue.trim();
  }

  async function fillFanqieSequence() {
    const node = document.querySelector('input.serial-input.byte-input.byte-input-size-default');
    if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement)) {
      return false;
    }
    if (String(node.value || '').trim()) {
      return true;
    }
    node.click();
    node.value = '1';
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(150);
    return Boolean(String(node.value || '').trim());
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

  function findFanqieBookEntry(bookName) {
    const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
    const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
    const matchedItem = bookName
      ? items.find((item) => textOf(item).includes(bookName))
      : items[0];
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

  async function openFanqieBookEntryEditor(bookName) {
    return (() => {
      const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
      const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
      const matchedItem = bookName
        ? items.find((item) => textOf(item).includes(bookName))
        : (items[0] || null);
      if (!matchedItem) {
        return false;
      }
      const publishAnchor = Array.from(matchedItem.querySelectorAll('a[href]')).find((node) => {
        const href = String(node.getAttribute('href') || '');
        return href.includes('/publish/');
      });
      if (publishAnchor instanceof HTMLAnchorElement) {
        publishAnchor.click();
        return true;
      }
      const createButton = Array.from(matchedItem.querySelectorAll('button, [role="button"]')).find((node) => {
        const text = textOf(node);
        return text === '创建章节' || text.includes('创建章节');
      });
      if (createButton instanceof HTMLElement) {
        createButton.click();
        return true;
      }
      return false;
    })();
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
      await dismissFanqieGuideModal();
      const text = pageText();
      const entry = findFanqieBookEntry(bookName);
      const createIfMissing = Boolean(payload.create_if_missing);
      const bookMeta = payload.book_meta || null;
      if (
        window.location.href.includes('/main/writer/')
        && entry?.publishHref
      ) {
        const openedEditor = await openFanqieBookEntryEditor(bookName);
        if (openedEditor) {
          return false;
        }
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
    if (!href.includes('#ccid=-1')) {
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

  async function confirmFanqiePublishIfNeeded() {
    if (!window.location.href.includes('fanqienovel.com')) {
      return false;
    }
    await dismissFanqieGuideModal();
    await Promise.race([
      waitForPageSignal(['发布设置', 'AI', '非AI', '审核中', '发布成功'], 5000),
      sleep(600),
    ]);
    let text = pageText();
    if (!includesAny(text, ['发布设置', 'AI', '非AI']) && includesAny(text, ['确定', '下一步'])) {
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
      if (!includesAny(text, ['发布设置', 'AI', '非AI'])) {
        await clickExactTexts(['确定', '我知道了', '继续', '下一步'], 2500)
          || await clickByKeywords(['确定', '我知道了', '继续', '下一步'], 2500);
        await sleep(600);
        text = pageText();
      }
    }
    if (!includesAny(text, ['发布设置', 'AI', '非AI'])) {
      return false;
    }
    await clickExactTexts(['非AI', '非AI生成', '否'], 2500)
      || await clickByKeywords(['非AI', '非AI生成', '否'], 2500);
    await sleep(400);
    const confirmed = await clickExactTexts(['确认发布', '发布', '提交审核', '确定'], 3000)
      || await clickByKeywords(['确认发布', '发布', '提交审核', '确定'], 3000);
    if (confirmed) {
      await sleep(1200);
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

    const ready = await preparePublishPage(payload.book_name, payload);
    if (ready && typeof ready === 'object' && ready.pending) {
      return {
        ok: false,
        currentUrl: window.location.href,
        error: ready.error || '平台正在跳转，请稍后重试。',
        errorCode: ready.errorCode || 'editor-navigation-pending',
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
    await waitForEditorReady(10000);
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
      await fillField(selectorsForTitle(), payload.chapter_title, '章节标题');
      if (window.location.href.includes('fanqienovel.com')) {
        if (!payload.trustedBodyDone) {
          await fillFanqieSequence();
          await fillFanqieTitle(payload.chapter_title);
          await dismissFanqieGuideModal();
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
        const status = readFanqieEditorStatus();
        const wordCount = status.bodyCharCount;
        if (wordCount <= 0) {
          return {
            ok: false,
            currentUrl: window.location.href,
            error: '番茄正文尚未真正写入编辑器状态。',
            errorCode: 'trusted-body-input-missing',
            trustedBodyTarget: locateFanqieTrustedBodyTarget(),
            editorStatus: status,
          };
        }
      }
    }
    if (!(window.location.href.includes('write.qq.com') && payload.publish && payload.trustedPublishDone)) {
      await clickAction(Boolean(payload.publish));
      if (window.location.href.includes('fanqienovel.com') && payload.publish) {
        await confirmFanqiePublishIfNeeded();
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
    await Promise.race([
      waitForPageSignal(['保存成功', '已保存', '发布成功', '提交成功', '审核中'], 4000),
      waitForCondition(() => !window.location.href.includes('login'), 4000),
      sleep(800),
    ]);
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
    if (message.action === 'apply-fanqie-trusted-body') {
      sendResponse(applyFanqieTrustedBody(message.payload?.body || ''));
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
  announceReady();
})();
