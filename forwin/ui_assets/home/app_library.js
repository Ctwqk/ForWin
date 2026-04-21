    function updateTaskModalSelects() {
      const modelSelect = document.getElementById('task_generation_model_profile_id');
      const genesisModelSelect = document.getElementById('genesis_model_profile_id');
      const platformSelect = document.getElementById('task_upload_platform');
      clearNode(platformSelect);
      populateModelProfileSelect(modelSelect, currentTaskPrefill?.model_profile_id || modelSelect?.value || '');
      currentGenesisModelProfileId = populateModelProfileSelect(
        genesisModelSelect,
        currentGenesisModelProfileId || genesisModelSelect?.value || '',
      );
      platformsState.forEach((platform) => {
        const option = document.createElement('option');
        option.value = platform.platform_id;
        option.textContent = platform.display_name;
        platformSelect.appendChild(option);
      });
    }

    function changeGenesisModelProfile(value) {
      currentGenesisModelProfileId = String(value || '').trim();
    }

    function renderProfiles() {
      const list = document.getElementById('profile_list');
      clearNode(list);
      const profiles = Array.isArray(settingsState?.profiles) ? settingsState.profiles : [];
      if (!profiles.length) {
        list.appendChild(createNode('div', '还没有模型配置。先添加一条，让生成任务只需要下拉选择。', 'empty'));
        return;
      }
      profiles.forEach((profile) => {
        const item = createNode('article', '', 'list-item');
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', profile.name || '未命名模型'));
        titleWrap.appendChild(createNode('div', `${profile.model} | ${profile.base_url}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        if (profile.id === settingsState.default_profile_id) badges.appendChild(createNode('span', '默认', 'badge ok'));
        badges.appendChild(createNode('span', profile.has_api_key ? 'Key 已保存' : 'Key 未保存', `badge ${profile.has_api_key ? 'ok' : 'warn'}`));
        top.appendChild(badges);
        item.appendChild(top);
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('设置', () => openModelModal(profile.id), 'secondary'));
        actions.appendChild(createButton('设为默认', () => setDefaultProfile(profile.id), 'ghost'));
        actions.appendChild(createButton('删除', () => deleteProfile(profile.id), 'danger'));
        item.appendChild(actions);
        list.appendChild(item);
      });
    }

    function modelPresetById(presetId) {
      return MODEL_PROVIDER_PRESETS.find((preset) => preset.id === presetId) || null;
    }

    function modelPresetSites(preset) {
      const sites = Array.isArray(preset?.sites) ? preset.sites : [];
      if (sites.length) return sites;
      if (preset?.base_url) {
        return [{ label: '默认站点', base_url: preset.base_url }];
      }
      return [];
    }

    function normalizedBaseUrl(value) {
      return String(value || '').trim().replace(/\/+$/, '').toLowerCase();
    }

    function detectModelPresetId(baseUrl, model) {
      const normalizedCurrentBaseUrl = normalizedBaseUrl(baseUrl);
      const normalizedModel = (model || '').trim();
      const matched = MODEL_PROVIDER_PRESETS.find((preset) => {
        const siteMatched = modelPresetSites(preset).some((site) => (
          normalizedCurrentBaseUrl && normalizedCurrentBaseUrl === normalizedBaseUrl(site.base_url)
        ));
        const recommended = Array.isArray(preset.recommended_models) ? preset.recommended_models.map((item) => String(item || '').trim()) : [];
        return (
          siteMatched
          || (normalizedModel && recommended.includes(normalizedModel))
        );
      });
      return matched ? matched.id : '';
    }

    function syncModelPresetControls(preferredPresetId = null) {
      const providerSelect = document.getElementById('model_form_provider_preset');
      const baseUrlSelect = document.getElementById('model_form_base_url_select');
      const modelSelect = document.getElementById('model_form_recommended_model');
      const hint = document.getElementById('model_form_provider_hint');
      const currentModel = document.getElementById('model_form_model').value.trim();
      const currentBaseUrl = document.getElementById('model_form_base_url').value.trim();
      const selectedPresetId = preferredPresetId !== null
        ? preferredPresetId
        : detectModelPresetId(currentBaseUrl, currentModel);

      clearNode(providerSelect);
      const customOption = document.createElement('option');
      customOption.value = '';
      customOption.textContent = '自定义';
      providerSelect.appendChild(customOption);
      MODEL_PROVIDER_PRESETS.forEach((preset) => {
        const option = document.createElement('option');
        option.value = preset.id;
        option.textContent = `${preset.label} · ${preset.default_model}`;
        providerSelect.appendChild(option);
      });
      providerSelect.value = selectedPresetId;

      const selectedPreset = modelPresetById(providerSelect.value);
      const sites = modelPresetSites(selectedPreset);
      clearNode(baseUrlSelect);
      if (!sites.length) {
        const option = document.createElement('option');
        option.value = currentBaseUrl;
        option.textContent = currentBaseUrl || '自定义当前值';
        baseUrlSelect.appendChild(option);
      } else {
        sites.forEach((site) => {
          const option = document.createElement('option');
          option.value = site.base_url;
          option.textContent = `${site.label} · ${site.base_url}`;
          baseUrlSelect.appendChild(option);
        });
        if (currentBaseUrl && !sites.some((site) => normalizedBaseUrl(site.base_url) === normalizedBaseUrl(currentBaseUrl))) {
          const option = document.createElement('option');
          option.value = currentBaseUrl;
          option.textContent = `${currentBaseUrl} · 当前自定义`;
          baseUrlSelect.appendChild(option);
        }
      }
      baseUrlSelect.value = currentBaseUrl || (sites[0]?.base_url || '');

      clearNode(modelSelect);
      const recommendedModels = selectedPreset && Array.isArray(selectedPreset.recommended_models)
        ? selectedPreset.recommended_models.map((item) => String(item || '').trim()).filter(Boolean)
        : [];
      if (!recommendedModels.length) {
        const option = document.createElement('option');
        option.value = currentModel;
        option.textContent = currentModel || '自定义当前值';
        modelSelect.appendChild(option);
      } else {
        recommendedModels.forEach((modelName) => {
          const option = document.createElement('option');
          option.value = modelName;
          option.textContent = modelName === selectedPreset.default_model ? `${modelName} · 推荐` : modelName;
          modelSelect.appendChild(option);
        });
        if (currentModel && !recommendedModels.includes(currentModel)) {
          const option = document.createElement('option');
          option.value = currentModel;
          option.textContent = `${currentModel} · 当前值`;
          modelSelect.appendChild(option);
        }
      }
      modelSelect.value = currentModel || (selectedPreset?.default_model || '');
      hint.textContent = selectedPreset
        ? `${selectedPreset.hint} 默认站点：${sites[0]?.base_url || selectedPreset.base_url || ''}`
        : '保留手填 base URL / model，用于任意 OpenAI 兼容服务。';
    }

    function applyModelPresetById(presetId) {
      const preset = modelPresetById(presetId);
      if (!preset) {
        syncModelPresetControls();
        return;
      }
      const nameInput = document.getElementById('model_form_name');
      const sites = modelPresetSites(preset);
      document.getElementById('model_form_base_url').value = sites[0]?.base_url || preset.base_url || '';
      document.getElementById('model_form_model').value = preset.default_model || '';
      if (!nameInput.value.trim()) {
        nameInput.value = preset.default_name || preset.label || '';
      }
      syncModelPresetControls(preset.id);
    }

    function applySelectedModelPreset() {
      applyModelPresetById(document.getElementById('model_form_provider_preset').value);
    }

    function normalizeMinChapterChars(value) {
      const normalized = Number(value || @@MIN_CHAPTER_CHARS_JSON@@);
      if (!Number.isFinite(normalized)) return @@MIN_CHAPTER_CHARS_JSON@@;
      return Math.max(500, Math.min(50000, Math.round(normalized)));
    }

    function normalizeReviewInterval(value) {
      const normalized = Number(value || 0);
      if (!Number.isFinite(normalized)) return 0;
      return Math.max(0, Math.min(200, Math.round(normalized)));
    }

    function normalizeProgressionMode(value) {
      const normalized = String(value || '').trim();
      if (['legacy_relaxed', 'serial_canon', 'serial_canon_band_guard'].includes(normalized)) {
        return normalized;
      }
      return '';
    }

    function strictGovernanceDefaults() {
      return {
        progression_mode: 'serial_canon_band_guard',
        auto_band_checkpoint: true,
        band_warn_action: 'pause',
        manual_checkpoints_enabled: true,
        future_constraints_enabled: true,
      };
    }

    function applyGenerationPreferenceFields() {
      const minChars = normalizeMinChapterChars(settingsState?.min_chapter_chars || @@MIN_CHAPTER_CHARS_JSON@@);
      document.getElementById('config_generation_min_chapter_chars').value = minChars;
      document.getElementById('config_generation_review_interval_chapters').value = normalizeReviewInterval(settingsState?.review_interval_chapters ?? @@REVIEW_INTERVAL_CHAPTERS_JSON@@);
      document.getElementById('config_generation_operation_mode').value = settingsState?.operation_mode || @@OPERATION_MODE_JSON@@;
      document.getElementById('config_generation_freeze_failed_candidates').checked = settingsState?.freeze_failed_candidates ?? @@FREEZE_FAILED_JSON@@;
    }

    async function loadSettings() {
      try {
        settingsState = await requestJson('/api/settings/llm');
        document.getElementById('saved_key_badge').textContent = `API Key：${settingsState.has_api_key ? '已保存' : '未保存'}`;
        applyGenerationPreferenceFields();
        updateTaskModalSelects();
        renderProfiles();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置读取失败');
      }
    }

    function openModelModal(profileId) {
      currentProfileId = profileId || '';
      const profile = (settingsState?.profiles || []).find((item) => item.id === currentProfileId);
      document.getElementById('model_modal_title').textContent = profile ? '模型设置' : '添加模型';
      document.getElementById('model_form_name').value = profile?.name || '';
      document.getElementById('model_form_model').value = profile?.model || @@MODEL_JSON@@;
      document.getElementById('model_form_base_url').value = profile?.base_url || @@BASE_URL_JSON@@;
      document.getElementById('model_form_api_key').value = '';
      document.getElementById('model_form_set_default').checked = Boolean(profile && settingsState?.default_profile_id === profile.id);
      syncModelPresetControls();
      document.getElementById('model_modal_shell').classList.add('open');
    }

    function closeModelModal() {
      document.getElementById('model_modal_shell').classList.remove('open');
    }

    async function saveModelProfile() {
      const payload = {
        profile_id: currentProfileId || null,
        name: document.getElementById('model_form_name').value.trim(),
        api_key: document.getElementById('model_form_api_key').value.trim(),
        base_url: document.getElementById('model_form_base_url').value.trim(),
        model: document.getElementById('model_form_model').value.trim(),
        set_as_default: document.getElementById('model_form_set_default').checked,
      };
      if (!payload.name) {
        setGlobalStatus('请先填写模型配置名称。', '模型配置');
        return;
      }
      try {
        settingsState = await requestJson('/api/settings/llm/profiles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeModelModal();
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '模型配置已保存。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置保存失败');
      }
    }

    async function saveGenerationPreferences() {
      const payload = {
        operation_mode: document.getElementById('config_generation_operation_mode').value,
        freeze_failed_candidates: document.getElementById('config_generation_freeze_failed_candidates').checked,
        min_chapter_chars: normalizeMinChapterChars(document.getElementById('config_generation_min_chapter_chars').value),
        review_interval_chapters: normalizeReviewInterval(document.getElementById('config_generation_review_interval_chapters').value),
      };
      try {
        settingsState = await requestJson('/api/settings/llm/preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        applyGenerationPreferenceFields();
        setGlobalStatus(settingsState.message || '生成设置已保存。', '生成设置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '生成设置保存失败');
      }
    }

    async function setDefaultProfile(profileId) {
      try {
        settingsState = await requestJson('/api/settings/llm/default-profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_id: profileId }),
        });
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '默认模型已切换。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '默认模型切换失败');
      }
    }

    async function deleteProfile(profileId) {
      if (!window.confirm('确定删除这条模型配置吗？')) return;
      try {
        settingsState = await requestJson(`/api/settings/llm/profiles/${profileId}`, { method: 'DELETE' });
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '模型配置已删除。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置删除失败');
      }
    }

    document.getElementById('model_form_provider_preset').addEventListener('change', () => {
      const presetId = document.getElementById('model_form_provider_preset').value;
      if (presetId) {
        applyModelPresetById(presetId);
      } else {
        syncModelPresetControls('');
      }
    });

    document.getElementById('model_form_base_url_select').addEventListener('change', (event) => {
      const value = event.target?.value || '';
      if (value) {
        document.getElementById('model_form_base_url').value = value;
      }
      syncModelPresetControls(document.getElementById('model_form_provider_preset').value);
    });

    document.getElementById('model_form_recommended_model').addEventListener('change', (event) => {
      const value = event.target?.value || '';
      if (value) {
        document.getElementById('model_form_model').value = value;
      }
    });

    function renderPlatforms() {
      const list = document.getElementById('platform_list');
      clearNode(list);
      if (!platformsState.length) {
        list.appendChild(createNode('div', '还没有收到平台状态。请先让浏览器扩展连上后端。', 'empty'));
        return;
      }
      platformsState.forEach((item) => {
        const card = createNode('article', '', 'list-item');
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', item.display_name));
        titleWrap.appendChild(createNode('div', `登录状态：${item.connected ? '已登录' : '未登录'} | 扩展：${item.extension_online ? '在线' : '离线'}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        badges.appendChild(createNode('span', item.connected ? '已登录' : '待登录', `badge ${item.connected ? 'ok' : 'warn'}`));
        badges.appendChild(createNode('span', item.extension_online ? '扩展在线' : '扩展离线', `badge ${item.extension_online ? 'ok' : 'warn'}`));
        top.appendChild(badges);
        card.appendChild(top);
        card.appendChild(createNode('div', `Client ID：${item.extension_client_id || '未绑定'}`, 'meta-line'));
        card.appendChild(createNode('div', `最近心跳：${item.last_heartbeat_at || '无'}`, 'meta-line'));
        if (item.last_error) card.appendChild(createNode('div', `最近错误：${item.last_error}`, 'meta-line'));
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton(item.connected ? '重新登录' : '登录', () => connectPlatform(item.platform_id), 'secondary'));
        actions.appendChild(createButton('打开官网', () => window.open(item.login_url, '_blank', 'noopener,noreferrer'), 'ghost'));
        card.appendChild(actions);
        list.appendChild(card);
      });
      updateTaskModalSelects();
    }

    async function loadPlatforms() {
      try {
        platformsState = await requestJson('/api/publishers/platforms');
        renderPlatforms();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '平台状态读取失败');
      }
    }

    async function connectPlatform(platform) {
      if (!BACKEND_EXTENSION_KEY_READY) {
        setGlobalStatus('后端尚未配置 FORWIN_PUBLISHER_EXTENSION_API_KEY。扩展可以打开登录页，但状态无法稳定回写。', '平台登录');
      }
      try {
        const payload = await bridgeRequest('open-login', { platform }, 2500);
        setGlobalStatus(payload.message || '登录弹窗已打开，请在浏览器扩展弹窗中完成登录。', '平台登录');
      } catch (error) {
        setGlobalStatus(`${error.message || String(error)}\n若扩展尚未加载，请用开发者模式加载：${EXTENSION_INSTALL_PATH}`, '平台登录');
      }
    }

    function fillPlatformSelect(selectId, selectedValue = '', includeEmpty = true) {
      const select = document.getElementById(selectId);
      if (!select) return;
      clearNode(select);
      if (includeEmpty) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = '暂不绑定平台';
        select.appendChild(emptyOption);
      }
      (platformsState || []).forEach((item) => {
        const option = document.createElement('option');
        option.value = item.platform_id;
        option.textContent = item.display_name || item.platform_id;
        select.appendChild(option);
      });
      select.value = selectedValue || '';
    }

    function normalizedPublishBindings(automation = {}) {
      const rawBindings = Array.isArray(automation?.publish_bindings) ? automation.publish_bindings : [];
      const candidates = [
        ...(automation?.publish?.platform ? [automation.publish] : []),
        ...rawBindings,
      ];
      const bindings = [];
      const seen = new Set();
      candidates.forEach((item) => {
        const platform = String(item?.platform || '').trim();
        if (!platform || seen.has(platform) || bindings.length >= 2) return;
        bindings.push({
          platform,
          book_name: String(item?.book_name || '').trim(),
          upload_url: String(item?.upload_url || '').trim(),
          create_if_missing: Boolean(item?.create_if_missing),
        });
        seen.add(platform);
      });
      return bindings;
    }

    function formatPublishBindingsSummary(automation = {}) {
      return normalizedPublishBindings(automation)
        .map((item, index) => `${index === 0 ? '默认 ' : ''}${item.platform}${item.create_if_missing ? '（先建书）' : '（只传章节）'}`)
        .join(' / ');
    }

    function resolveBookPrefillBindings(prefill = {}) {
      if (Array.isArray(prefill.publish_bindings) && prefill.publish_bindings.length) {
        return prefill.publish_bindings.slice(0, 2);
      }
      if (prefill.publish_platform) {
        return [
          {
            platform: prefill.publish_platform,
            book_name: prefill.publish_book_name || prefill.title || '',
            upload_url: prefill.publish_upload_url || '',
            create_if_missing: prefill.platform_has_existing_book === false,
          },
        ];
      }
      return [];
    }

    function readBookBinding(index) {
      return {
        platform: document.getElementById(`book_form_publish_platform_${index}`).value,
        book_name: document.getElementById(`book_form_publish_book_name_${index}`).value.trim(),
        upload_url: document.getElementById(`book_form_publish_upload_url_${index}`).value.trim(),
        create_if_missing: document.getElementById(`book_form_publish_mode_${index}`).value === 'create_book',
      };
    }

    function openBookModal(prefill = {}) {
      const bindings = resolveBookPrefillBindings(prefill);
      const primaryBinding = bindings[0] || {};
      const secondaryBinding = bindings[1] || {};
      fillPlatformSelect('book_form_publish_platform_1', primaryBinding.platform || '', true);
      fillPlatformSelect('book_form_publish_platform_2', secondaryBinding.platform || '', true);
      document.getElementById('book_form_title').value = prefill.title || '';
      document.getElementById('book_form_genre').value = prefill.genre || @@DEFAULT_GENRE_JSON@@;
      document.getElementById('book_form_target_total_chapters').value = prefill.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@;
      document.getElementById('book_form_audience_hint').value = prefill.audience_hint || '';
      document.getElementById('book_form_core_emotion').value = prefill.core_emotion || '';
      document.getElementById('book_form_core_delight').value = prefill.core_delight || '';
      document.getElementById('book_form_premise').value = prefill.premise || '';
      document.getElementById('book_form_setting_summary').value = prefill.setting_summary || '';
      document.getElementById('book_form_inspiration_notes').value = prefill.inspiration_notes || '';
      document.getElementById('book_form_content_guardrails').value = Array.isArray(prefill.content_guardrails)
        ? prefill.content_guardrails.join('\\n')
        : (prefill.content_guardrails || '');
      document.getElementById('book_form_publish_mode_1').value = primaryBinding.create_if_missing ? 'create_book' : 'chapter_only';
      document.getElementById('book_form_publish_mode_2').value = secondaryBinding.create_if_missing ? 'create_book' : 'chapter_only';
      document.getElementById('book_form_publish_book_name_1').value = primaryBinding.book_name || prefill.title || '';
      document.getElementById('book_form_publish_book_name_2').value = secondaryBinding.book_name || '';
      document.getElementById('book_form_publish_upload_url_1').value = primaryBinding.upload_url || '';
      document.getElementById('book_form_publish_upload_url_2').value = secondaryBinding.upload_url || '';
      document.getElementById('book_modal_shell').classList.add('open');
    }

    function closeBookModal() {
      document.getElementById('book_modal_shell').classList.remove('open');
    }

    async function submitBookModal() {
      try {
        const rawBindings = [readBookBinding(1), readBookBinding(2)];
        const publishBindings = [];
        const seenPlatforms = new Set();
        for (const item of rawBindings) {
          if (!item.platform) continue;
          if (seenPlatforms.has(item.platform)) {
            setGlobalStatus('两个绑定平台不能重复，请调整后再创建。', '书本管理');
            return;
          }
          publishBindings.push(item);
          seenPlatforms.add(item.platform);
        }
        const primaryBinding = publishBindings[0] || null;
        const payload = {
          title: document.getElementById('book_form_title').value.trim(),
          premise: document.getElementById('book_form_premise').value.trim(),
          genre: document.getElementById('book_form_genre').value.trim() || @@DEFAULT_GENRE_JSON@@,
          target_total_chapters: Number(document.getElementById('book_form_target_total_chapters').value || @@DEFAULT_CHAPTERS_JSON@@),
          audience_hint: document.getElementById('book_form_audience_hint').value.trim(),
          core_emotion: document.getElementById('book_form_core_emotion').value.trim(),
          core_delight: document.getElementById('book_form_core_delight').value.trim(),
          setting_summary: document.getElementById('book_form_setting_summary').value.trim(),
          inspiration_notes: document.getElementById('book_form_inspiration_notes').value.trim(),
          content_guardrails: parseTextareaLines(document.getElementById('book_form_content_guardrails').value),
          publish_bindings: publishBindings,
          publish_platform: primaryBinding?.platform || '',
          publish_book_name: primaryBinding?.book_name || '',
          publish_upload_url: primaryBinding?.upload_url || '',
          platform_has_existing_book: primaryBinding ? !primaryBinding.create_if_missing : true,
        };
        if (!payload.title) {
          setGlobalStatus('新建书本必须填写书名。', '书本管理');
          return;
        }
        if (!payload.premise) {
          setGlobalStatus('新建书本必须填写 premise / prompt。', '书本管理');
          return;
        }
        if (!Number.isFinite(payload.target_total_chapters) || payload.target_total_chapters < 1 || payload.target_total_chapters > 200) {
          setGlobalStatus('总章节数必须是 1 到 200 之间的整数。', '书本管理');
          return;
        }
        const created = await requestJson('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeBookModal();
        switchTab('book');
        await loadBooks();
        setGlobalStatus(created.message || `书本《${payload.title}》已创建。`, '书本管理');
        if (created.project_id) {
          await openGenesisWorkspace(created.project_id, 'brief');
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '新建书本失败');
      }
    }

    function toggleSelectAllBooks() {
      if (selectedBookIds.size === booksState.length) {
        selectedBookIds = new Set();
      } else {
        selectedBookIds = new Set(booksState.map((book) => book.id).filter(Boolean));
      }
      renderBooks();
    }

    async function bulkDeleteBooks() {
      const projectIds = Array.from(selectedBookIds);
      if (!projectIds.length) return;
      if (!window.confirm(`确定批量删除这 ${projectIds.length} 本书吗？相关章节、review 和关联数据都会一起删除。`)) return;
      try {
        const result = await requestJson('/api/projects/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_ids: projectIds }),
        });
        const deletedProjectIds = new Set(Array.isArray(result.deleted_ids) ? result.deleted_ids : []);
        selectedBookIds = new Set(
          Array.from(selectedBookIds).filter((projectId) => !deletedProjectIds.has(projectId))
        );
        if (currentDrawerTask?.project_id && deletedProjectIds.has(currentDrawerTask.project_id)) {
          closeTaskDrawer();
        }
        setGlobalStatus(result.message || '批量删除完成。', '书本管理');
        await loadBooks();
        await loadTaskCenter();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '批量删除书本失败');
      }
    }

    async function deleteBook(book) {
      if (!window.confirm(`删除书本《${book.title || '未命名'}》后，章节、review 和关联数据都会删除，确定继续吗？`)) return;
      try {
        const result = await requestJson(`/api/projects/${book.id}`, { method: 'DELETE' });
        selectedBookIds.delete(book.id);
        setGlobalStatus(result.message || '书本已删除。', '书本管理');
        await loadBooks();
        await loadTaskCenter();
        if (currentDrawerTask?.project_id === book.id) {
          closeTaskDrawer();
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '删除书本失败');
      }
    }

    function renderBooks() {
      const list = document.getElementById('book_list');
      clearNode(list);
      if (!booksState.length) {
        list.appendChild(createNode('div', '还没有书本。先新建一本，进入 Genesis 创世，再决定什么时候启动写作。', 'empty'));
        syncBookBulkActions();
        return;
      }
      booksState.forEach((book) => {
        const node = createNode('article', '', 'task-item');
        const selectionLine = createNode('div', '', 'selection-line');
        selectionLine.appendChild(createNode('div', `书本 · ${book.id}`, 'task-id'));
        const selectWrap = document.createElement('label');
        selectWrap.className = 'select-toggle';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = selectedBookIds.has(book.id);
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedBookIds.add(book.id);
          else selectedBookIds.delete(book.id);
          syncBookBulkActions();
        });
        selectWrap.appendChild(checkbox);
        selectWrap.appendChild(document.createTextNode('选择'));
        selectionLine.appendChild(selectWrap);
        node.appendChild(selectionLine);
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', book.title || '未命名书本'));
        titleWrap.appendChild(createNode('div', `${book.genre || ''}${book.created_at ? ` | 创建于 ${book.created_at}` : ''}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        if (book.creation_status && book.creation_status !== 'legacy') {
          badges.appendChild(createNode('span', `Genesis · ${book.creation_status}`, `badge ${book.creation_status === 'genesis_ready' ? 'ok' : 'warn'}`));
        }
        badges.appendChild(createNode('span', `目标 ${book.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@} 章`, 'badge'));
        badges.appendChild(createNode('span', `已规划 ${book.chapter_count || 0}`, 'badge'));
        badges.appendChild(createNode('span', `已生成 ${book.generated_chapter_count || 0}`, 'badge ok'));
        badges.appendChild(createNode('span', `已上传 ${book.uploaded_chapter_count || 0}`, 'badge'));
        if (book.needs_review_chapter_count) {
          badges.appendChild(createNode('span', `待处理 ${book.needs_review_chapter_count}`, 'badge warn'));
        }
        top.appendChild(badges);
        node.appendChild(top);
        const publishBindingsSummary = formatPublishBindingsSummary(book.automation || {});
        const meta = [
          book.premise ? `Premise：${book.premise}` : '',
          book.target_total_chapters ? `计划总章数：${book.target_total_chapters}` : '',
          book.latest_stage ? `最近阶段：${book.latest_stage}` : '',
          book.pacing_summary ? `节奏：${book.pacing_summary}` : '',
          book.upload_task_count ? `上传任务：${book.upload_task_count}` : '',
          book.automation?.publish?.platform
            ? `发布默认：${book.automation.publish.platform} · ${book.automation.publish.create_if_missing ? '平台未建书，首次上传先建书' : '平台已有书目，只传章节'}`
            : '',
          publishBindingsSummary
            ? `已绑定平台：${publishBindingsSummary}`
            : '',
          genesisProgressSummary(book),
          book.automation?.enabled
            ? `自动化：${book.automation.daily_start_time || '09:00'} 开始，每日 ${book.automation.daily_chapter_quota || 1} 章${book.automation.auto_publish ? '，完成后自动发布' : ''}`
            : '自动化：关闭',
        ].filter(Boolean).join('\\n');
        if (meta) {
          node.appendChild(createNode('div', meta, 'meta-line'));
        }
        if (Array.isArray(book.chapters) && book.chapters.length) {
          const preview = book.chapters
            .slice(-3)
            .map((chapter) => `第${chapter.chapter_number}章 ${chapterStatusLabel(chapter.status)} · ${chapter.title}`)
            .join('\\n');
          node.appendChild(createNode('div', preview, 'meta-line'));
        }
        const actions = createNode('div', '', 'action-row');
        const viewButtonLabel = ['creating', 'genesis_ready'].includes(book.creation_status) ? '创世工作台' : '查看书本';
        actions.appendChild(createButton(
          viewButtonLabel,
          () => ['creating', 'genesis_ready'].includes(book.creation_status)
            ? openGenesisWorkspace(book.id)
            : openTaskDrawer('generation', `project-${book.id}`),
          'secondary',
        ));
        const publishButton = createButton('发布到平台', () => openBookPublishModal(book), 'secondary');
        if (!pickLatestPublishableChapter(book)) {
          publishButton.disabled = true;
        }
        actions.appendChild(publishButton);
        const control = book.generation_control || {};
        const hasReviewBlocker = Boolean(book.needs_review_chapter_count || (Array.isArray(control.pending_review_chapters) && control.pending_review_chapters.length));
        let generateLabel = '生成首批章节';
        let generateClass = 'primary';
        let generateAction = () => openTaskModal('generation', {
          project_id: book.id,
          book_title: book.title,
          premise: book.premise || '',
          genre: book.genre || @@DEFAULT_GENRE_JSON@@,
          num_chapters: book.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@,
          operation_mode: book.governance?.default_operation_mode || '',
          progression_mode: book.governance?.progression_mode || '',
          auto_band_checkpoint: Boolean(book.governance?.auto_band_checkpoint),
          manual_checkpoints_enabled: Boolean(book.governance?.manual_checkpoints_enabled),
          future_constraints_enabled: Boolean(book.governance?.future_constraints_enabled),
        });
        if (book.creation_status === 'creating') {
          generateLabel = '继续创世';
          generateClass = 'primary';
          generateAction = () => openGenesisWorkspace(book.id);
        } else if (book.creation_status === 'genesis_ready') {
          generateLabel = '启动写作';
          generateClass = 'primary';
          generateAction = () => startWritingFromList(book.id);
        } else if (hasReviewBlocker) {
          generateLabel = '处理 Review';
          generateClass = 'primary';
          generateAction = () => openTaskDrawer('generation', `project-${book.id}`);
        } else if (control.can_resume) {
          generateLabel = '继续生成剩余章节';
          generateClass = 'primary';
          generateAction = () => continueProjectGeneration(book.id);
        } else if (book.chapter_count) {
          generateLabel = control.plan_state === 'completed' ? '写作完成' : '查看进度';
          generateClass = 'ghost';
          generateAction = () => openTaskDrawer('generation', `project-${book.id}`);
        }
        const generateButton = createButton(
          generateLabel,
          generateAction,
          generateClass,
        );
        actions.appendChild(generateButton);
        actions.appendChild(createButton('删除书本', () => deleteBook(book), 'danger'));
        node.appendChild(actions);
        list.appendChild(node);
      });
      syncBookBulkActions();
    }

    async function loadBooks(showStatus = false) {
      try {
        const nextBooksState = await requestJson('/api/projects');
        const nextSignature = dataSignature(nextBooksState);
        booksState = nextBooksState;
        selectedBookIds = new Set(
          Array.from(selectedBookIds).filter((projectId) => booksState.some((book) => book.id === projectId))
        );
        if (nextSignature !== booksStateSignature) {
          booksStateSignature = nextSignature;
          renderBooks();
        } else if (!document.getElementById('book_list')?.childNodes.length) {
          booksStateSignature = nextSignature;
          renderBooks();
        } else {
          syncBookBulkActions();
        }
        if (showStatus) setGlobalStatus(`已刷新 ${booksState.length} 本书。`, '书本管理');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '书本列表读取失败');
      }
    }

    function toggleSelectAllTasks() {
      const deletableItems = taskCenterState.filter((item) => item.deletable);
      if (selectedTaskKeys.size === deletableItems.length) {
        selectedTaskKeys = new Set();
      } else {
        selectedTaskKeys = new Set(deletableItems.map((item) => taskSelectionKey(item)));
      }
      renderTaskList();
    }

    async function bulkDeleteTasks() {
      const items = taskCenterState
        .filter((item) => selectedTaskKeys.has(taskSelectionKey(item)) && item.deletable)
        .map((item) => ({ task_kind: item.task_kind, task_id: item.task_id }));
      if (!items.length) return;
      if (!window.confirm(`确定批量删除这 ${items.length} 条任务吗？`)) return;
      try {
        const result = await requestJson('/api/tasks/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items }),
        });
        const deletedIds = new Set(Array.isArray(result.deleted_ids) ? result.deleted_ids : []);
        selectedTaskKeys = new Set(
          Array.from(selectedTaskKeys).filter((key) => !deletedIds.has(key))
        );
        if (currentDrawerTask && deletedIds.has(`${currentDrawerTask.task_kind}:${currentDrawerTask.task_id}`)) {
          closeTaskDrawer();
        }
        setGlobalStatus(result.message || '批量删除完成。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '批量删除任务失败');
      }
    }

    function renderTaskList() {
      const list = document.getElementById('task_list');
      clearNode(list);
      if (!taskCenterState.length) {
        list.appendChild(createNode('div', '还没有任务。点击右上角“新建任务”开始。', 'empty'));
        syncTaskBulkActions();
        return;
      }
      taskCenterState.forEach((item) => {
        const node = createNode('article', '', 'task-item');
        const selectionLine = createNode('div', '', 'selection-line');
        selectionLine.appendChild(createNode('div', `${serializeTaskType(item.task_kind)} · ${item.task_id}`, 'task-id'));
        const selectWrap = document.createElement('label');
        selectWrap.className = 'select-toggle';
        const checkbox = document.createElement('input');
        const selectionKey = taskSelectionKey(item);
        checkbox.type = 'checkbox';
        checkbox.checked = selectedTaskKeys.has(selectionKey);
        checkbox.disabled = !item.deletable;
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedTaskKeys.add(selectionKey);
          else selectedTaskKeys.delete(selectionKey);
          syncTaskBulkActions();
        });
        selectWrap.appendChild(checkbox);
        selectWrap.appendChild(document.createTextNode(item.deletable ? '选择' : '不可删'));
        selectionLine.appendChild(selectWrap);
        node.appendChild(selectionLine);
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', item.title || '未命名任务'));
        titleWrap.appendChild(createNode('div', `${item.status}${item.subtitle ? ` | ${item.subtitle}` : ''}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        badges.appendChild(createNode('span', serializeTaskType(item.task_kind), 'badge'));
        badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
        top.appendChild(badges);
        node.appendChild(top);
        const meta = [
          item.updated_at ? `更新时间：${item.updated_at}` : '',
          item.project_id ? `书本：${item.project_id}` : '',
          item.extension_client_id ? `执行端：${item.extension_client_id}` : '',
          item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
          item.message ? `消息：${item.message}` : '',
          item.error ? `错误：${item.error}` : '',
        ].filter(Boolean).join('\\n');
        node.appendChild(createNode('div', meta, 'meta-line'));
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('查看详情', () => openTaskDrawer(item.task_kind, item.task_id), 'secondary'));
        actions.appendChild(createButton('暂停', () => pauseTask(item), 'ghost'));
        actions.appendChild(createButton('强制终止', () => terminateTask(item), 'ghost'));
        if (item.task_kind === 'generation' && item.generation_control?.can_resume && item.project_id) {
          actions.appendChild(createButton('继续', () => continueProjectGeneration(item.project_id), 'primary'));
        }
        actions.appendChild(createButton('删除', () => deleteTask(item), 'danger'));
        actions.querySelectorAll('button')[1].disabled = !item.pausable;
        actions.querySelectorAll('button')[2].disabled = !item.terminable;
        actions.querySelectorAll('button')[actions.querySelectorAll('button').length - 1].disabled = !item.deletable;
        node.appendChild(actions);
        list.appendChild(node);
      });
      syncTaskBulkActions();
    }

    async function loadTaskCenter(showStatus = false) {
      try {
        const nextTaskCenterState = await requestJson('/api/task-center/items?limit=80');
        const nextSignature = dataSignature(nextTaskCenterState);
        taskCenterState = nextTaskCenterState;
        const validSelectionKeys = new Set(
          taskCenterState
            .filter((item) => item.deletable)
            .map((item) => taskSelectionKey(item))
        );
        selectedTaskKeys = new Set(
          Array.from(selectedTaskKeys).filter((key) => validSelectionKeys.has(key))
        );
        taskPollHasActive = taskCenterState.some((item) => ACTIVE_TASK_STATUSES.has(item.status));
        if (nextSignature !== taskCenterStateSignature) {
          taskCenterStateSignature = nextSignature;
          renderTaskList();
        } else if (!document.getElementById('task_list')?.childNodes.length) {
          taskCenterStateSignature = nextSignature;
          renderTaskList();
        } else {
          syncTaskBulkActions();
        }
        if (showStatus) setGlobalStatus(`已刷新 ${taskCenterState.length} 条任务。`, '任务中心');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务中心读取失败');
      }
    }

    function dismissModal(event, id) {
      if (event.target === event.currentTarget) {
        if (id === 'genesis_modal_shell') {
          closeGenesisWorkspace();
          return;
        }
        document.getElementById(id).classList.remove('open');
      }
    }

    function closeGovernanceActionModal() {
      document.getElementById('governance_action_modal_shell').classList.remove('open');
      currentGovernanceAction = null;
      clearNode(document.getElementById('governance_action_modal_fields'));
      document.getElementById('governance_action_modal_reason').value = '';
    }

    function openGovernanceActionModal(config = {}) {
      currentGovernanceAction = config || {};
      document.getElementById('governance_action_modal_title').textContent = config.title || '治理动作';
      document.getElementById('governance_action_modal_description').textContent = config.description || '所有治理动作都要求填写原因，便于进入决策链与审计时间线。';
      document.getElementById('governance_action_modal_submit').textContent = config.confirmLabel || '提交';
      document.getElementById('governance_action_modal_reason').value = config.reason || '';
      const fields = document.getElementById('governance_action_modal_fields');
      clearNode(fields);
      (Array.isArray(config.fields) ? config.fields : []).forEach((field) => {
        let control = null;
        if (field.type === 'select') {
          control = document.createElement('select');
          (Array.isArray(field.options) ? field.options : []).forEach((optionValue) => {
            const option = document.createElement('option');
            option.value = String(optionValue || '');
            option.textContent = String(optionValue || '');
            control.appendChild(option);
          });
          control.value = String(field.value ?? '');
        } else {
          control = field.type === 'textarea' ? document.createElement('textarea') : document.createElement('input');
          if (field.type !== 'textarea') {
            control.type = field.type === 'number' ? 'number' : 'text';
          } else {
            control.rows = Number(field.rows || 8);
            control.spellcheck = false;
          }
          if (field.type === 'number') {
            control.min = String(field.min ?? 0);
            control.step = String(field.step ?? 1);
          }
          control.value = String(field.value ?? '');
        }
        control.id = `governance_action_field_${field.name}`;
        fields.appendChild(createLabeledField(field.label || field.name, control));
      });
      document.getElementById('governance_action_modal_shell').classList.add('open');
    }

    async function submitGovernanceActionModal() {
      if (!currentGovernanceAction?.onSubmit) return;
      const reason = String(document.getElementById('governance_action_modal_reason').value || '').trim();
      if (!reason) {
        setGlobalStatus('治理动作必须填写 reason。', '治理动作');
        return;
      }
      const values = {};
      (Array.isArray(currentGovernanceAction.fields) ? currentGovernanceAction.fields : []).forEach((field) => {
        const control = document.getElementById(`governance_action_field_${field.name}`);
        if (!control) return;
        if (field.type === 'number') {
          values[field.name] = Number(control.value || 0);
        } else {
          values[field.name] = String(control.value || '').trim();
        }
      });
      try {
        await currentGovernanceAction.onSubmit({ reason, ...values });
        closeGovernanceActionModal();
      } catch (error) {
        setGlobalStatus(error.message || String(error), currentGovernanceAction.errorTitle || '治理动作失败');
      }
    }

    function setTaskModalKind(kind) {
      currentTaskModalKind = kind;
      document.getElementById('new_task_kind_generation').classList.toggle('active', kind === 'generation');
      document.getElementById('new_task_kind_upload').classList.toggle('active', kind === 'upload');
      document.getElementById('task_form_generation').style.display = kind === 'generation' ? 'grid' : 'none';
      document.getElementById('task_form_upload').style.display = kind === 'upload' ? 'grid' : 'none';
    }

    function updateTaskModalHeader() {
      const title = document.getElementById('task_modal_title');
      const description = document.getElementById('task_modal_description');
      if (!title || !description) return;
      if (currentTaskModalKind === 'upload') {
        title.textContent = '新建上传任务';
        description.textContent = '统一入口。先选任务类型，再填写最少必要字段。';
        return;
      }
      if (currentTaskPrefill?.continue_generation) {
        title.textContent = '继续生成';
        description.textContent = '沿用现有生成 modal，但本次提交会走 continue-generation，并允许覆盖本次治理策略。';
        return;
      }
      title.textContent = '新建任务';
      description.textContent = '统一入口。先选任务类型，再填写最少必要字段。';
    }

    function applyTaskPrefill() {
      const projectHint = document.getElementById('task_generation_project_hint');
      if (currentTaskPrefill.project_id) {
        projectHint.style.display = 'block';
        projectHint.textContent = currentTaskPrefill.continue_generation
          ? `继续生成目标：${currentTaskPrefill.book_title || '未命名书本'} · ${currentTaskPrefill.project_id}`
          : `当前生成目标：${currentTaskPrefill.book_title || '未命名书本'} · ${currentTaskPrefill.project_id}`;
      } else {
        projectHint.style.display = 'none';
        projectHint.textContent = '';
      }
      document.getElementById('task_generation_genre').value = currentTaskPrefill.genre || @@DEFAULT_GENRE_JSON@@;
      document.getElementById('task_generation_num_chapters').value = currentTaskPrefill.num_chapters || @@DEFAULT_CHAPTERS_JSON@@;
      document.getElementById('task_generation_min_chapter_chars').value = normalizeMinChapterChars(
        currentTaskPrefill.min_chapter_chars || settingsState?.min_chapter_chars || @@MIN_CHAPTER_CHARS_JSON@@
      );
      document.getElementById('task_generation_premise').value = currentTaskPrefill.premise || '';
      document.getElementById('task_generation_operation_mode').value = currentTaskPrefill.operation_mode || settingsState?.operation_mode || @@OPERATION_MODE_JSON@@;
      document.getElementById('task_generation_freeze_failed_candidates').checked = currentTaskPrefill.freeze_failed_candidates ?? settingsState?.freeze_failed_candidates ?? @@FREEZE_FAILED_JSON@@;
      const strictDefaults = strictGovernanceDefaults();
      const defaultProgressionMode = currentTaskPrefill.progression_mode ?? settingsState?.progression_mode ?? strictDefaults.progression_mode;
      document.getElementById('task_generation_progression_mode').value = normalizeProgressionMode(defaultProgressionMode) || strictDefaults.progression_mode;
      document.getElementById('task_generation_auto_band_checkpoint').checked = currentTaskPrefill.auto_band_checkpoint ?? settingsState?.auto_band_checkpoint ?? strictDefaults.auto_band_checkpoint;
      document.getElementById('task_generation_manual_checkpoints_enabled').checked = currentTaskPrefill.manual_checkpoints_enabled ?? settingsState?.manual_checkpoints_enabled ?? strictDefaults.manual_checkpoints_enabled;
      document.getElementById('task_generation_future_constraints_enabled').checked = currentTaskPrefill.future_constraints_enabled ?? settingsState?.future_constraints_enabled ?? strictDefaults.future_constraints_enabled;
      if (currentTaskPrefill.model_profile_id) {
        document.getElementById('task_generation_model_profile_id').value = currentTaskPrefill.model_profile_id;
      }

      document.getElementById('task_upload_platform').value = currentTaskPrefill.platform || (platformsState[0]?.platform_id || '');
      document.getElementById('task_upload_book_name').value = currentTaskPrefill.book_name || '';
      document.getElementById('task_upload_chapter_title').value = currentTaskPrefill.chapter_title || '';
      document.getElementById('task_upload_upload_url').value = currentTaskPrefill.upload_url || '';
      document.getElementById('task_upload_body').value = currentTaskPrefill.body || '';
      document.getElementById('task_upload_publish').checked = currentTaskPrefill.publish ?? true;
      document.getElementById('task_upload_create_if_missing').checked = currentTaskPrefill.create_if_missing ?? false;
      document.getElementById('task_upload_audience').value = currentTaskPrefill.audience || '';
      document.getElementById('task_upload_primary_category').value = currentTaskPrefill.primary_category || '';
      document.getElementById('task_upload_protagonist_names').value = currentTaskPrefill.protagonist_names || '';
      document.getElementById('task_upload_intro').value = currentTaskPrefill.intro || '';
    }

    function openTaskModal(kind = 'generation', prefill = {}) {
      currentTaskPrefill = prefill || {};
      setTaskModalKind(kind);
      updateTaskModalHeader();
      applyTaskPrefill();
      document.getElementById('task_modal_shell').classList.add('open');
    }

    function closeTaskModal() {
      document.getElementById('task_modal_shell').classList.remove('open');
      currentTaskPrefill = {};
    }

    function preferredPublishBinding(book) {
      const automation = book?.automation || {};
      const bindings = Array.isArray(automation.publish_bindings)
        ? automation.publish_bindings.filter((item) => item?.platform)
        : [];
      if (bindings.length) return bindings[0];
      if (automation.publish?.platform) return automation.publish;
      return {};
    }

    function pickLatestPublishableChapter(book) {
      const chapters = Array.isArray(book?.chapters) ? book.chapters : [];
      return chapters
        .filter((chapter) => Number(chapter?.char_count || 0) > 0 || ['drafted', 'accepted'].includes(String(chapter?.status || '')))
        .sort((left, right) => Number(right?.chapter_number || 0) - Number(left?.chapter_number || 0))[0] || null;
    }

    async function openBookPublishModal(book) {
      const chapter = pickLatestPublishableChapter(book);
      if (!chapter) {
        setGlobalStatus('当前书本还没有可发布的已生成章节。', '书本发布');
        return;
      }
      try {
        const chapterDetail = await requestJson(`/api/projects/${book.id}/chapters/${chapter.chapter_number}`);
        const binding = preferredPublishBinding(book);
        const bookMeta = binding?.book_meta || {};
        openTaskModal('upload', {
          project_id: book.id,
          platform: binding?.platform || '',
          book_name: binding?.book_name || book.title || '',
          chapter_title: chapterDetail.title || chapter.title || `第${chapter.chapter_number}章`,
          body: chapterDetail.body || '',
          upload_url: binding?.upload_url || '',
          create_if_missing: Boolean(binding?.create_if_missing),
          audience: bookMeta?.audience || '',
          primary_category: bookMeta?.primary_category || '',
          protagonist_names: Array.isArray(bookMeta?.protagonist_names) ? bookMeta.protagonist_names.join(', ') : '',
          intro: bookMeta?.intro || '',
          publish: true,
        });
      } catch (error) {
        setGlobalStatus(error.message || String(error), '章节读取失败');
      }
    }

    async function submitTaskModal() {
      try {
        if (currentTaskModalKind === 'generation') {
          const payload = {
            project_id: currentTaskPrefill.project_id || null,
            premise: document.getElementById('task_generation_premise').value.trim(),
            genre: document.getElementById('task_generation_genre').value.trim() || @@DEFAULT_GENRE_JSON@@,
            num_chapters: Number(document.getElementById('task_generation_num_chapters').value || @@DEFAULT_CHAPTERS_JSON@@),
            min_chapter_chars: normalizeMinChapterChars(document.getElementById('task_generation_min_chapter_chars').value),
            review_interval_chapters: normalizeReviewInterval(settingsState?.review_interval_chapters ?? @@REVIEW_INTERVAL_CHAPTERS_JSON@@),
            model_profile_id: document.getElementById('task_generation_model_profile_id').value || null,
            operation_mode: document.getElementById('task_generation_operation_mode').value,
            freeze_failed_candidates: document.getElementById('task_generation_freeze_failed_candidates').checked,
            progression_mode: normalizeProgressionMode(document.getElementById('task_generation_progression_mode').value),
            auto_band_checkpoint: document.getElementById('task_generation_auto_band_checkpoint').checked,
            manual_checkpoints_enabled: document.getElementById('task_generation_manual_checkpoints_enabled').checked,
            future_constraints_enabled: document.getElementById('task_generation_future_constraints_enabled').checked,
          };
          if (!currentTaskPrefill.continue_generation && !payload.premise) {
            setGlobalStatus('生成任务必须填写 premise / prompt。', '新建任务');
            return;
          }
          const requestUrl = currentTaskPrefill.continue_generation && currentTaskPrefill.project_id
            ? `/api/projects/${currentTaskPrefill.project_id}/continue-generation`
            : '/api/generate';
          const requestPayload = currentTaskPrefill.continue_generation
            ? {
                max_chapters: Number(document.getElementById('task_generation_num_chapters').value || 0) || null,
                operation_mode: payload.operation_mode,
                review_interval_chapters: payload.review_interval_chapters,
                progression_mode: payload.progression_mode || null,
                auto_band_checkpoint: payload.auto_band_checkpoint,
                manual_checkpoints_enabled: payload.manual_checkpoints_enabled,
                future_constraints_enabled: payload.future_constraints_enabled,
              }
            : payload;
          const created = await requestJson(requestUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestPayload),
          });
          closeTaskModal();
          switchTab('task');
          await loadTaskCenter();
          await loadBooks();
          setGlobalStatus(
            currentTaskPrefill.continue_generation
              ? `已创建继续生成任务 ${created.task_id}。`
              : `已创建生成任务 ${created.task_id}。`,
            currentTaskPrefill.continue_generation ? '继续生成' : '新建任务'
          );
          await openTaskDrawer('generation', created.task_id);
          return;
        }

        const protagonistNames = document.getElementById('task_upload_protagonist_names').value
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean);
        const payload = {
          project_id: currentTaskPrefill.project_id || null,
          platform: document.getElementById('task_upload_platform').value,
          book_name: document.getElementById('task_upload_book_name').value.trim(),
          chapter_title: document.getElementById('task_upload_chapter_title').value.trim(),
          body: document.getElementById('task_upload_body').value,
          upload_url: document.getElementById('task_upload_upload_url').value.trim() || null,
          publish: document.getElementById('task_upload_publish').checked,
          create_if_missing: document.getElementById('task_upload_create_if_missing').checked,
          book_meta: {
            audience: document.getElementById('task_upload_audience').value,
            primary_category: document.getElementById('task_upload_primary_category').value.trim(),
            protagonist_names: protagonistNames,
            intro: document.getElementById('task_upload_intro').value.trim(),
          },
        };
        if (!payload.platform || !payload.book_name || !payload.chapter_title || !payload.body.trim()) {
          setGlobalStatus('上传任务至少需要平台、作品名、章节名和正文。', '新建任务');
          return;
        }
        const created = await requestJson('/api/publishers/upload-jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeTaskModal();
        switchTab('task');
        await loadTaskCenter();
        await loadBooks();
        setGlobalStatus(`已创建上传任务 ${created.job_id}。`, '新建任务');
        await openTaskDrawer('upload', created.job_id);
      } catch (error) {
        setGlobalStatus(error.message || String(error), '新建任务失败');
      }
    }

    async function terminateTask(item) {
      const url = item.task_kind === 'upload'
        ? `/api/publishers/upload-jobs/${item.task_id}/terminate`
        : `/api/tasks/${item.task_id}/terminate`;
      try {
        const result = await requestJson(url, { method: 'POST' });
        setGlobalStatus(result.message || '已发送终止请求。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          await openTaskDrawer(item.task_kind, item.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务终止失败');
      }
    }

    async function pauseTask(item) {
      if (item.task_kind !== 'generation') {
        setGlobalStatus('发布任务暂不支持安全暂停，请使用终止。', '任务操作');
        return;
      }
      try {
        const result = await requestJson(`/api/tasks/${item.task_id}/pause`, { method: 'POST' });
        setGlobalStatus(result.message || '已发送安全暂停请求。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          await openTaskDrawer(item.task_kind, item.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务暂停失败');
      }
    }

    async function continueProjectGeneration(projectId) {
      const project = booksState.find((book) => book.id === projectId);
      openTaskModal('generation', {
        continue_generation: true,
        project_id: projectId,
        book_title: project?.title || '',
        premise: project?.premise || '',
        genre: project?.genre || @@DEFAULT_GENRE_JSON@@,
        num_chapters: project?.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@,
        operation_mode: project?.governance?.default_operation_mode || '',
        progression_mode: project?.governance?.progression_mode || '',
        auto_band_checkpoint: Boolean(project?.governance?.auto_band_checkpoint),
        manual_checkpoints_enabled: Boolean(project?.governance?.manual_checkpoints_enabled),
        future_constraints_enabled: Boolean(project?.governance?.future_constraints_enabled),
      });
    }

    async function deleteTask(item) {
      if (!window.confirm('删除后任务会从任务中心消失，确定继续吗？')) return;
      const url = item.task_kind === 'upload'
        ? `/api/publishers/upload-jobs/${item.task_id}`
        : `/api/tasks/${item.task_id}`;
      try {
        await requestJson(url, { method: 'DELETE' });
        selectedTaskKeys.delete(taskSelectionKey(item));
        setGlobalStatus(`任务 ${item.task_id} 已删除。`, '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          closeTaskDrawer();
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务删除失败');
      }
    }
