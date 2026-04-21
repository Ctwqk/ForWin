    function genesisStageLabel(stageKey) {
      const map = {
        brief: '创意简报',
        world: '世界观与背景',
        map: '地图与空间拓扑',
        story_engine: '角色势力与叙事引擎',
        book_blueprint: '整本书多 Arc 路线图',
        bootstrap: '执行契约与启动交接',
      };
      return map[stageKey] || stageKey || 'Genesis Stage';
    }

    function genesisFieldForStage(stageKey) {
      return GENESIS_STAGE_FIELD_MAP[stageKey] || 'book_brief';
    }

    function genesisStageState(detail, stageKey) {
      return detail?.pack?.stage_states?.[stageKey] || {
        stage_key: stageKey,
        status: 'todo',
        locked: false,
        updated_at: '',
        last_trace_id: '',
      };
    }

    function genesisProgressSummary(book) {
      const stages = Array.isArray(book?.genesis_stage_overview) ? book.genesis_stage_overview : [];
      if (!stages.length) return '';
      const lockedCount = stages.filter((stage) => stage.locked).length;
      const activeStage = stages.find((stage) => !stage.locked) || stages[stages.length - 1];
      return `Genesis：${lockedCount}/${stages.length} 已锁定${activeStage ? ` · 当前 ${genesisStageLabel(activeStage.stage_key)}` : ''}`;
    }

    function chooseGenesisStage(detail) {
      const stages = Array.isArray(detail?.pack?.stage_states)
        ? detail.pack.stage_states
        : Object.values(detail?.pack?.stage_states || {});
      const firstUnlocked = stages.find((stage) => !stage.locked);
      return firstUnlocked?.stage_key || GENESIS_STAGE_ORDER[0];
    }

    function closeGenesisWorkspace() {
      document.getElementById('genesis_modal_shell').classList.remove('open');
      currentGenesisProjectId = '';
      currentGenesisDetail = null;
      currentGenesisStage = GENESIS_STAGE_ORDER[0];
      currentGenesisItemCollection = '';
      currentGenesisItemIndex = -1;
      clearAllGenesisDrafts();
      currentGenesisModelProfileId = currentGenesisModelProfileId || settingsState?.default_profile_id || '';
    }

    async function openGenesisWorkspace(projectId, stageKey = '') {
      currentGenesisProjectId = projectId;
      currentGenesisStage = stageKey && GENESIS_STAGE_ORDER.includes(stageKey)
        ? stageKey
        : GENESIS_STAGE_ORDER[0];
      document.getElementById('genesis_modal_shell').classList.add('open');
      await refreshGenesisWorkspace(projectId);
    }

    async function refreshGenesisWorkspace(projectId = currentGenesisProjectId) {
      if (!projectId) return;
      try {
        const detail = await requestJson(`/api/projects/${projectId}/genesis`);
        currentGenesisDetail = detail;
        clearAllGenesisDrafts();
        if (!GENESIS_STAGE_ORDER.includes(currentGenesisStage)) {
          currentGenesisStage = chooseGenesisStage(detail);
        }
        renderGenesisWorkspace();
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 工作台读取失败');
      }
    }

    function currentGenesisServerPayload(detail = currentGenesisDetail, stageKey = currentGenesisStage) {
      const fieldKey = genesisFieldForStage(stageKey);
      return detail?.pack?.[fieldKey] || {};
    }

    function currentGenesisPayload(detail = currentGenesisDetail, stageKey = currentGenesisStage) {
      if (Object.prototype.hasOwnProperty.call(currentGenesisDrafts, stageKey)) {
        return currentGenesisDrafts[stageKey];
      }
      return currentGenesisServerPayload(detail, stageKey);
    }

    function rememberGenesisDraft(stageKey, payload) {
      currentGenesisDrafts[stageKey] = deepCloneJson(payload || {});
    }

    function clearGenesisDraft(stageKey = currentGenesisStage) {
      delete currentGenesisDrafts[stageKey];
    }

    function clearAllGenesisDrafts() {
      currentGenesisDrafts = {};
    }

    function genesisStageItemTargets(stageKey = currentGenesisStage) {
      return GENESIS_STAGE_ITEM_TARGETS[stageKey] || [];
    }

    function genesisItemKey(target) {
      return target?.collection || target?.path || '';
    }

    function defaultGenesisItemCollection(stageKey = currentGenesisStage) {
      return genesisItemKey(genesisStageItemTargets(stageKey)[0]) || '';
    }

    function genesisItemDefinition(stageKey = currentGenesisStage, collection = currentGenesisItemCollection) {
      return genesisStageItemTargets(stageKey).find((item) => genesisItemKey(item) === collection) || null;
    }

    function currentGenesisItemList(detail = currentGenesisDetail, collection = currentGenesisItemCollection) {
      const definition = genesisItemDefinition(currentGenesisStage, collection);
      const payload = currentGenesisPayload(detail);
      if (!definition) return [];
      if (definition.collection) {
        return Array.isArray(payload?.[definition.collection]) ? payload[definition.collection] : [];
      }
      if (!definition.path) return [];
      if (payload && typeof payload === 'object' && Object.prototype.hasOwnProperty.call(payload, definition.path)) {
        return [payload[definition.path]];
      }
      return [deepCloneJson(definition.template ?? null)];
    }

    function currentGenesisItemPath() {
      const definition = genesisItemDefinition();
      if (!definition) return '';
      if (definition.path && !definition.collection) return definition.path;
      if (!currentGenesisItemCollection || currentGenesisItemIndex < 0) return '';
      return `${definition.collection}[${currentGenesisItemIndex}]`;
    }

    function currentGenesisItemPayload(detail = currentGenesisDetail) {
      const items = currentGenesisItemList(detail);
      const definition = genesisItemDefinition();
      if (definition?.path && !definition.collection) {
        return items.length ? items[0] : deepCloneJson(definition.template ?? null);
      }
      return currentGenesisItemIndex >= 0 && currentGenesisItemIndex < items.length ? items[currentGenesisItemIndex] : null;
    }

    function genesisPathSegments(path = '') {
      return String(path || '')
        .split('.')
        .map((part) => part.trim())
        .filter(Boolean);
    }

    function genesisGetValueAtPath(source, path = '') {
      if (!path) return source;
      let cursor = source;
      for (const segment of genesisPathSegments(path)) {
        if (cursor == null || typeof cursor !== 'object') return undefined;
        cursor = cursor[segment];
      }
      return cursor;
    }

    function genesisSetValueAtPath(source, path, value) {
      if (!path) return value;
      const segments = genesisPathSegments(path);
      if (!segments.length) return value;
      let cursor = source;
      for (let index = 0; index < segments.length - 1; index += 1) {
        const segment = segments[index];
        if (!cursor[segment] || typeof cursor[segment] !== 'object' || Array.isArray(cursor[segment])) {
          cursor[segment] = {};
        }
        cursor = cursor[segment];
      }
      cursor[segments[segments.length - 1]] = value;
      return source;
    }

    function genesisStringList(value) {
      if (Array.isArray(value)) return value.filter((item) => String(item || '').trim()).map((item) => String(item).trim());
      if (value == null || value === '') return [];
      return String(value)
        .split('\\n')
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function genesisObjectList(value, schema = []) {
      if (Array.isArray(value)) {
        return value
          .filter((item) => item && typeof item === 'object')
          .map((item) => {
            const normalized = {};
            schema.forEach((field) => {
              const raw = item[field.path];
              if (field.kind === 'checkbox') {
                normalized[field.path] = Boolean(raw);
              } else if (field.kind === 'list') {
                normalized[field.path] = genesisStringList(raw);
              } else {
                normalized[field.path] = raw == null ? '' : raw;
              }
            });
            return normalized;
          });
      }
      return [];
    }

    function genesisParseObjectList(value, schema = []) {
      const text = String(value ?? '').trim();
      if (!text) return [];
      try {
        const parsed = JSON.parse(text);
        return genesisObjectList(parsed, schema);
      } catch (error) {
        return [];
      }
    }

    function genesisDisplayObjectList(value, schema = []) {
      return JSON.stringify(genesisObjectList(value, schema), null, 2);
    }

    function genesisReadFieldValue(field, control) {
      const kind = field?.kind || 'text';
      if (kind === 'checkbox') return Boolean(control?.checked);
      if (kind === 'number') {
        const parsed = Number.parseInt(String(control?.value ?? '').trim(), 10);
        return Number.isFinite(parsed) ? parsed : 0;
      }
      if (kind === 'list') return genesisStringList(control?.value);
      if (kind === 'object_list') return genesisParseObjectList(control?.value, field.schema || []);
      return String(control?.value ?? '');
    }

    function genesisFieldDisplayValue(field, value) {
      const kind = field?.kind || 'text';
      if (kind === 'checkbox') return Boolean(value);
      if (kind === 'list') return Array.isArray(value) ? value.join('\\n') : '';
      if (kind === 'object_list') return genesisDisplayObjectList(value, field.schema || []);
      if (kind === 'number') return value == null || value === '' ? '' : String(value);
      return value == null ? '' : String(value);
    }

    function genesisReferenceOptions(source) {
      if (!source) return [];
      let payload = {};
      if (source === 'culture_profiles') {
        payload = currentGenesisStage === 'world'
          ? currentGenesisPayload(currentGenesisDetail, 'world')
          : currentGenesisServerPayload(currentGenesisDetail, 'world');
      } else if (source === 'submaps' || source === 'regions' || source === 'nodes') {
        payload = currentGenesisStage === 'map'
          ? currentGenesisPayload(currentGenesisDetail, 'map')
          : currentGenesisServerPayload(currentGenesisDetail, 'map');
      } else if (source === 'factions' || source === 'core_cast' || source === 'opposition') {
        payload = currentGenesisStage === 'story_engine'
          ? currentGenesisPayload(currentGenesisDetail, 'story_engine')
          : currentGenesisServerPayload(currentGenesisDetail, 'story_engine');
      }
      const items = Array.isArray(payload?.[source]) ? payload[source] : [];
      return items
        .map((item) => {
          if (item && typeof item === 'object') {
            const fallbackValue = String(item.name || item.title || item.id || '').trim();
            if (!fallbackValue) return null;
            if (source === 'culture_profiles') {
              return {
                value: String(item.id || fallbackValue).trim(),
                label: `${String(item.name || fallbackValue).trim()}${item.inspiration ? ` · ${item.inspiration}` : ''}`,
              };
            }
            if (source === 'regions') {
              return {
                value: String(item.id || fallbackValue).trim(),
                label: `${String(item.name || fallbackValue).trim()}${item.subworld_name ? ` · ${item.subworld_name}` : ''}`,
              };
            }
            return { value: fallbackValue, label: fallbackValue };
          }
          const value = String(item || '').trim();
          return value ? { value, label: value } : null;
        })
        .filter(Boolean);
    }

    function updateGenesisLivePreview(payload) {
      document.getElementById('genesis_stage_preview').textContent = JSON.stringify(payload || {}, null, 2);
      const definition = genesisItemDefinition();
      const preview = document.getElementById('genesis_item_preview');
      if (!preview || !definition) return;
      if (definition.path && !definition.collection) {
        preview.textContent = JSON.stringify(genesisGetValueAtPath(payload, definition.path), null, 2);
        return;
      }
      if (!definition.collection) return;
      const items = Array.isArray(payload?.[definition.collection]) ? payload[definition.collection] : [];
      const currentItem = currentGenesisItemIndex >= 0 && currentGenesisItemIndex < items.length ? items[currentGenesisItemIndex] : null;
      preview.textContent = JSON.stringify(currentItem || definition.template || {}, null, 2);
    }

    async function generateGenesisFieldValue(field, applyFn, context = {}) {
      if (!currentGenesisProjectId) return;
      try {
        const response = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis/generate-name`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            stage_key: currentGenesisStage,
            target_path: context.targetPath || '',
            field_path: field.path || '',
            kind: field.name_generation_kind || '',
            count: Number(field.name_generation_count || 1),
            nonce: String(Date.now()),
            stage_payload_override: currentGenesisPayload(currentGenesisDetail, currentGenesisStage),
          }),
        });
        applyFn(field, response.applied_value, true);
        setGlobalStatus(response.message || `${field.label} 已自动生成。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), `${field.label} 自动生成失败`);
      }
    }

    function renderGenesisStructuredFields(container, fields, source, applyFn, context = {}) {
      clearNode(container);
      if (!fields.length) {
        container.appendChild(createNode('div', '当前阶段暂未拆出结构化字段，仍可直接编辑下方 JSON。', 'meta-line'));
        return;
      }
      fields.forEach((field) => {
        const wrapper = createNode('div', '', 'form-field');
        const labelRow = createNode('div', '', 'row wrap');
        const label = document.createElement('label');
        label.textContent = field.label;
        labelRow.appendChild(label);
        if (field.name_generation_kind) {
          labelRow.appendChild(createButton('自动生成', () => generateGenesisFieldValue(field, applyFn, context), 'ghost'));
        }
        wrapper.appendChild(labelRow);
        const currentValue = genesisGetValueAtPath(source, field.path);
        const kind = field.kind || 'text';
        let control;
        if (kind === 'textarea' || kind === 'list' || kind === 'object_list') {
          control = document.createElement('textarea');
          control.value = genesisFieldDisplayValue(field, currentValue);
          if (field.placeholder) control.placeholder = field.placeholder;
        } else if (kind === 'select' || kind === 'reference') {
          control = document.createElement('select');
          const options = kind === 'reference' ? genesisReferenceOptions(field.source) : (field.options || []);
          const blank = document.createElement('option');
          blank.value = '';
          blank.textContent = field.empty_label || '未设置';
          control.appendChild(blank);
          options.forEach((optionDef) => {
            const option = document.createElement('option');
            option.value = String(optionDef.value ?? '');
            option.textContent = String(optionDef.label ?? optionDef.value ?? '');
            control.appendChild(option);
          });
          const normalizedValue = genesisFieldDisplayValue(field, currentValue);
          if (normalizedValue && !Array.from(control.options).some((option) => option.value === normalizedValue)) {
            const option = document.createElement('option');
            option.value = normalizedValue;
            option.textContent = `${normalizedValue} · 当前值`;
            control.appendChild(option);
          }
          control.value = normalizedValue;
        } else {
          control = document.createElement('input');
          control.type = kind === 'number' ? 'number' : 'text';
          control.value = genesisFieldDisplayValue(field, currentValue);
          if (field.placeholder) control.placeholder = field.placeholder;
        }
        if (kind === 'checkbox') {
          control = document.createElement('input');
          control.type = 'checkbox';
          control.checked = Boolean(currentValue);
        }
        const commitField = (refresh = false) => applyFn(field, genesisReadFieldValue(field, control), refresh);
        control.addEventListener(kind === 'checkbox' ? 'change' : 'input', () => commitField(false));
        control.addEventListener('change', () => commitField(true));
        wrapper.appendChild(control);
        if (field.help) {
          wrapper.appendChild(createNode('div', field.help, 'meta-line'));
        }
        if (field.row_label) {
          wrapper.appendChild(createNode('div', `行内标签：${field.row_label}`, 'meta-line'));
        }
        container.appendChild(wrapper);
      });
    }

    function handleGenesisEditorInput() {
      const editor = document.getElementById('genesis_stage_editor');
      if (!editor) return;
      try {
        const parsed = JSON.parse(editor.value || '{}');
        rememberGenesisDraft(currentGenesisStage, parsed);
        updateGenesisLivePreview(parsed);
      } catch (error) {
        document.getElementById('genesis_stage_preview').textContent = '当前 JSON 不是有效对象，保存或 AI 改写前需要先修正。';
      }
    }

    function applyGenesisStageField(field, value, refresh = false) {
      const payload = deepCloneJson(currentGenesisPayload(currentGenesisDetail, currentGenesisStage));
      genesisSetValueAtPath(payload, field.path, value);
      rememberGenesisDraft(currentGenesisStage, payload);
      document.getElementById('genesis_stage_editor').value = JSON.stringify(payload || {}, null, 2);
      updateGenesisLivePreview(payload);
      if (refresh) {
        renderGenesisItemWorkbench(currentGenesisDetail);
      }
    }

    function applyGenesisItemField(field, value, refresh = false) {
      const definition = genesisItemDefinition();
      if (!definition) return;
      const payload = deepCloneJson(currentGenesisPayload(currentGenesisDetail, currentGenesisStage));
      if (definition.path && !definition.collection) {
        const rootValue = genesisGetValueAtPath(payload, definition.path);
        const nextValue = deepCloneJson(rootValue);
        const updatedValue = field.path ? genesisSetValueAtPath(nextValue, field.path, value) : value;
        genesisSetValueAtPath(payload, definition.path, updatedValue);
      } else if (definition.collection) {
        if (!Array.isArray(payload[definition.collection])) payload[definition.collection] = [];
        while (payload[definition.collection].length <= currentGenesisItemIndex) {
          payload[definition.collection].push(deepCloneJson(definition.template || {}));
        }
        const nextItem = deepCloneJson(payload[definition.collection][currentGenesisItemIndex] || {});
        genesisSetValueAtPath(nextItem, field.path, value);
        if (currentGenesisStage === 'map' && definition.collection === 'regions') {
          const regions = payload[definition.collection];
          const currentId = String(nextItem.id || '').trim();
          const currentSubworld = String(nextItem.subworld_name || '').trim();
          let nextLevel = Number.parseInt(String(nextItem.level ?? 1), 10);
          if (nextLevel !== 2) nextLevel = 1;
          nextItem.level = nextLevel;
          if (nextLevel === 1) {
            nextItem.parent_region_id = '';
          } else {
            const candidates = Array.isArray(regions)
              ? regions.filter((region, index) => {
                if (index === currentGenesisItemIndex) return false;
                if (!region || typeof region !== 'object') return false;
                if (String(region.subworld_name || '').trim() !== currentSubworld) return false;
                if (Number.parseInt(String(region.level ?? 1), 10) !== 1) return false;
                const regionId = String(region.id || '').trim();
                return regionId && regionId !== currentId;
              })
              : [];
            const parentId = String(nextItem.parent_region_id || '').trim();
            if (!candidates.some((region) => String(region.id || '').trim() === parentId)) {
              if (candidates.length) {
                nextItem.parent_region_id = String(candidates[0].id || '').trim();
              } else {
                nextItem.level = 1;
                nextItem.parent_region_id = '';
              }
            }
          }
        }
        payload[definition.collection][currentGenesisItemIndex] = nextItem;
      }
      rememberGenesisDraft(currentGenesisStage, payload);
      document.getElementById('genesis_stage_editor').value = JSON.stringify(payload || {}, null, 2);
      updateGenesisLivePreview(payload);
      if (refresh) {
        renderGenesisItemWorkbench(currentGenesisDetail);
      }
    }

    function renderGenesisStageForm(detail = currentGenesisDetail) {
      const container = document.getElementById('genesis_stage_form');
      renderGenesisStructuredFields(
        container,
        GENESIS_STAGE_FORM_FIELDS[currentGenesisStage] || [],
        currentGenesisPayload(detail, currentGenesisStage),
        applyGenesisStageField,
        { mode: 'stage', targetPath: '' },
      );
    }

    function ensureGenesisItemSelection(detail = currentGenesisDetail) {
      const targets = genesisStageItemTargets();
      if (!targets.length) {
        currentGenesisItemCollection = '';
        currentGenesisItemIndex = -1;
        return;
      }
      if (!targets.some((item) => genesisItemKey(item) === currentGenesisItemCollection)) {
        currentGenesisItemCollection = defaultGenesisItemCollection();
      }
      const definition = genesisItemDefinition(currentGenesisStage, currentGenesisItemCollection);
      if (!definition) {
        currentGenesisItemIndex = -1;
        return;
      }
      if (definition.path && !definition.collection) {
        currentGenesisItemIndex = 0;
        return;
      }
      const items = currentGenesisItemList(detail, currentGenesisItemCollection);
      if (!items.length) {
        currentGenesisItemIndex = -1;
        return;
      }
      if (currentGenesisItemIndex < 0 || currentGenesisItemIndex >= items.length) {
        currentGenesisItemIndex = 0;
      }
    }

    function genesisItemLabel(item, definition, index) {
      if (definition?.path && !definition?.collection) {
        return definition.singletonLabel || definition.label || '子项';
      }
      if (item && typeof item === 'object') {
        return item.name || item.title || item.id || `${definition?.label || '子项'} ${index + 1}`;
      }
      return `${definition?.label || '子项'} ${index + 1}`;
    }

    function parseGenesisStageEditor() {
      return JSON.parse(document.getElementById('genesis_stage_editor').value || '{}');
    }

    async function patchGenesisStagePayload(nextPayload, reason = '') {
      return runGenesisAction(async () => {
        const fieldKey = genesisFieldForStage(currentGenesisStage);
        currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [fieldKey]: nextPayload, reason }),
        });
        clearAllGenesisDrafts();
        renderGenesisWorkspace();
        await loadBooks();
        return true;
      });
    }

    async function persistGenesisEditorIfDirty(reasonPrefix = 'ui_sync') {
      const parsed = parseGenesisStageEditor();
      if (dataSignature(parsed) === dataSignature(currentGenesisServerPayload(currentGenesisDetail))) return;
      const saved = await patchGenesisStagePayload(parsed, `${reasonPrefix}_${currentGenesisStage}`);
      if (saved === null) return;
    }

    function currentGenesisStageSummary(detail = currentGenesisDetail) {
      const stageState = genesisStageState(detail, currentGenesisStage);
      const payload = currentGenesisPayload(detail);
      const lines = [
        `阶段：${genesisStageLabel(currentGenesisStage)}`,
        `状态：${stageState.status || 'todo'}`,
        `锁定：${stageState.locked ? '是' : '否'}`,
        stageState.updated_at ? `更新时间：${stageState.updated_at}` : '',
      ];
      if (payload && typeof payload === 'object') {
        if (payload.summary) lines.push(`Summary：${payload.summary}`);
        if (payload.overview) lines.push(`Overview：${payload.overview}`);
        if (Array.isArray(payload.arcs)) {
          lines.push(`Arc 数量：${payload.arcs.length}`);
          lines.push(...payload.arcs.slice(0, 6).map((arc) => `Arc ${arc.arc_number || '?'} · ${arc.title || '未命名'} · ${arc.chapter_count || 0} 章`));
        }
        if (Array.isArray(payload.axioms)) lines.push(`规则数量：${payload.axioms.length}`);
        if (payload.history_slice) lines.push(`历史切片：${String(payload.history_slice).slice(0, 40)}`);
        if (payload.naming_style) lines.push(`命名风格：${String(payload.naming_style)}`);
        if (Array.isArray(payload.forbidden_zones)) lines.push(`禁区数量：${payload.forbidden_zones.length}`);
        if (Array.isArray(payload.culture_profiles)) lines.push(`文化背景：${payload.culture_profiles.length}`);
        if (Array.isArray(payload.submaps)) lines.push(`小地图数量：${payload.submaps.length}`);
        if (Array.isArray(payload.regions)) lines.push(`地区数量：${payload.regions.length}`);
        if (Array.isArray(payload.nodes)) lines.push(`地点节点：${payload.nodes.length}`);
        if (Array.isArray(payload.core_cast)) lines.push(`角色数量：${payload.core_cast.length}`);
        if (Array.isArray(payload.factions)) lines.push(`势力数量：${payload.factions.length}`);
        if (Array.isArray(payload.long_arcs)) lines.push(`长期引擎：${payload.long_arcs.length}`);
      }
      return lines.filter(Boolean).join('\\n');
    }

    function renderGenesisTraceList(detail = currentGenesisDetail) {
      const traceList = document.getElementById('genesis_trace_list');
      clearNode(traceList);
      const traces = (Array.isArray(detail?.prompt_traces) ? detail.prompt_traces : [])
        .filter((trace) => trace.stage_key === currentGenesisStage)
        .slice(0, 6);
      if (!traces.length) {
        traceList.appendChild(createNode('div', '当前阶段还没有 Prompt Trace。可以先点“生成”或“重生”。', 'meta-line'));
        return;
      }
      traces.forEach((trace) => {
        const item = createNode('article', '', 'trace-item');
        item.appendChild(createNode('strong', `${trace.template_id || currentGenesisStage} · ${trace.created_at || '未记录时间'}`));
        item.appendChild(createNode('div', [
          trace.template_version ? `template=${trace.template_version}` : '',
          trace.model_profile?.profile_name ? `profile=${trace.model_profile.profile_name}` : '',
          trace.model_profile?.profile_id ? `profile_id=${trace.model_profile.profile_id}` : '',
          trace.model_profile?.model ? `model=${trace.model_profile.model}` : '',
          trace.decision_event_id ? `decision=${trace.decision_event_id}` : '',
        ].filter(Boolean).join(' | '), 'meta-line'));
        if (trace.input_snapshot?.instruction) {
          item.appendChild(createNode('div', `指令：${trace.input_snapshot.instruction}`, 'meta-line'));
        }
        if (trace.input_snapshot?.target_path) {
          item.appendChild(createNode('div', `目标：${trace.input_snapshot.target_path}`, 'meta-line'));
        }
        const summary = createNode('div', '', 'trace-summary');
        if (trace.output_summary && Object.keys(trace.output_summary).length) {
          summary.appendChild(createNode('div', JSON.stringify(trace.output_summary, null, 2), 'code'));
        }
        item.appendChild(summary);
        const promptPre = document.createElement('pre');
        promptPre.textContent = trace.effective_system_prompt || '无 system prompt 快照。';
        item.appendChild(promptPre);
        traceList.appendChild(item);
      });
    }

    function renderGenesisItemWorkbench(detail = currentGenesisDetail) {
      const card = document.getElementById('genesis_item_card');
      const collectionSelect = document.getElementById('genesis_item_collection_select');
      const entrySelect = document.getElementById('genesis_item_entry_select');
      const meta = document.getElementById('genesis_item_meta');
      const form = document.getElementById('genesis_item_form');
      const preview = document.getElementById('genesis_item_preview');
      const refineItemBtn = document.getElementById('genesis_refine_item_btn');
      const refineMeta = document.getElementById('genesis_refine_meta');
      const createBtn = document.getElementById('genesis_create_item_btn');
      const deleteBtn = document.getElementById('genesis_delete_item_btn');
      const targets = genesisStageItemTargets();
      clearNode(collectionSelect);
      clearNode(entrySelect);
      if (!targets.length) {
        card.hidden = true;
        preview.textContent = '';
        clearNode(form);
        meta.textContent = '当前阶段没有子项目标。';
        refineItemBtn.disabled = true;
        if (createBtn) createBtn.disabled = true;
        if (deleteBtn) deleteBtn.disabled = true;
        refineMeta.textContent = '默认会改写整个阶段；世界观、地图与空间、角色与势力、Arc 蓝图阶段支持子项级对话改写。';
        return;
      }
      card.hidden = false;
      ensureGenesisItemSelection(detail);
      targets.forEach((target) => {
        const option = document.createElement('option');
        option.value = genesisItemKey(target);
        option.textContent = target.label;
        option.selected = genesisItemKey(target) === currentGenesisItemCollection;
        collectionSelect.appendChild(option);
      });
      const definition = genesisItemDefinition();
      const items = currentGenesisItemList(detail);
      const supportsListItems = Boolean(definition?.collection);
      if (createBtn) createBtn.disabled = !supportsListItems;
      if (deleteBtn) deleteBtn.disabled = !supportsListItems || !currentGenesisItemPath();
      if (definition?.path && !definition.collection) {
        const option = document.createElement('option');
        option.value = '0';
        option.textContent = definition.singletonLabel || `当前${definition.label || '目标'}`;
        option.selected = true;
        entrySelect.appendChild(option);
        entrySelect.disabled = true;
        const currentItem = currentGenesisItemPayload(detail);
        meta.textContent = `当前目标：${definition.label || '子项'} · ${definition.path}`;
        renderGenesisStructuredFields(form, definition.fields || [], currentItem, applyGenesisItemField, {
          mode: 'item',
          targetPath: currentGenesisItemPath(),
        });
        preview.textContent = JSON.stringify(currentItem ?? definition.template ?? null, null, 2);
        refineItemBtn.disabled = !currentGenesisItemPath();
        refineMeta.textContent = currentGenesisItemPath()
          ? `当前可定向改写：${currentGenesisItemPath()}`
          : '默认会改写整个阶段；如果先选择子项，就可以只改那个字段。';
        return;
      }
      if (!items.length) {
        const option = document.createElement('option');
        option.value = '-1';
        option.textContent = `还没有${definition?.label || '子项'}`;
        option.selected = true;
        entrySelect.appendChild(option);
        entrySelect.disabled = true;
        meta.textContent = `当前还没有${definition?.label || '子项'}。可以先新增一个，再用 AI 只改这个对象。`;
        clearNode(form);
        preview.textContent = JSON.stringify(definition?.template || {}, null, 2);
        refineItemBtn.disabled = true;
        if (deleteBtn) deleteBtn.disabled = true;
        refineMeta.textContent = '默认会改写整个阶段；如果先创建并选择子项，就可以只改那个对象。';
        return;
      }
      entrySelect.disabled = false;
      items.forEach((item, index) => {
        const option = document.createElement('option');
        option.value = String(index);
        option.textContent = genesisItemLabel(item, definition, index);
        option.selected = index === currentGenesisItemIndex;
        entrySelect.appendChild(option);
      });
      const currentItem = currentGenesisItemPayload(detail);
      meta.textContent = `当前目标：${definition?.label || '子项'} · ${genesisItemLabel(currentItem, definition, currentGenesisItemIndex)}`;
      renderGenesisStructuredFields(form, definition.fields || [], currentItem || definition.template || {}, applyGenesisItemField, {
        mode: 'item',
        targetPath: currentGenesisItemPath(),
      });
      preview.textContent = JSON.stringify(currentItem || {}, null, 2);
      refineItemBtn.disabled = !currentGenesisItemPath();
      refineMeta.textContent = currentGenesisStage === 'book_blueprint'
        ? 'Arc 蓝图默认仍沿用自动生成；你也可以选中某个 Arc 做局部编辑或 AI 微调。'
        : (
          currentGenesisItemPath()
            ? `当前可定向改写：${currentGenesisItemPath()}`
            : '默认会改写整个阶段；如果先创建并选择子项，就可以只改那个对象。'
        );
    }

    function renderGenesisWorkspace() {
      const detail = currentGenesisDetail;
      if (!detail) return;
      const projectTitle = booksState.find((book) => book.id === detail.project_id)?.title || detail.project_id;
      document.getElementById('genesis_modal_title').textContent = `Book Genesis · ${projectTitle}`;
      document.getElementById('genesis_modal_subtitle').textContent = [
        `项目 ${detail.project_id}`,
        `状态：${detail.creation_status || 'creating'}`,
        `Revision：${detail.revision || 1}`,
      ].join(' | ');
      const board = document.getElementById('genesis_stage_board');
      clearNode(board);
      GENESIS_STAGE_ORDER.forEach((stageKey) => {
        const stageState = genesisStageState(detail, stageKey);
        const button = createButton(
          `${genesisStageLabel(stageKey)} · ${stageState.locked ? '已锁定' : stageState.status || 'todo'}`,
          () => {
            currentGenesisStage = stageKey;
            currentGenesisItemCollection = '';
            currentGenesisItemIndex = -1;
            renderGenesisWorkspace();
          },
          `genesis-stage-chip${currentGenesisStage === stageKey ? ' active' : ''}${stageState.locked ? ' locked' : ''}`,
        );
        board.appendChild(button);
      });

      const stageState = genesisStageState(detail, currentGenesisStage);
      const payload = currentGenesisPayload(detail);
      document.getElementById('genesis_stage_title').textContent = genesisStageLabel(currentGenesisStage);
      document.getElementById('genesis_stage_meta').textContent = [
        `状态：${stageState.status || 'todo'}`,
        `锁定：${stageState.locked ? '是' : '否'}`,
        stageState.updated_at ? `更新：${stageState.updated_at}` : '',
      ].filter(Boolean).join(' | ');
      document.getElementById('genesis_stage_editor').value = JSON.stringify(payload || {}, null, 2);
      document.getElementById('genesis_stage_summary').textContent = currentGenesisStageSummary(detail);
      document.getElementById('genesis_stage_preview').textContent = JSON.stringify(payload || {}, null, 2);
      renderGenesisStageForm(detail);
      currentGenesisModelProfileId = populateModelProfileSelect(
        document.getElementById('genesis_model_profile_id'),
        currentGenesisModelProfileId || settingsState?.default_profile_id || '',
      );

      const blueprint = currentGenesisPayload(detail, 'book_blueprint') || {};
      const blueprintLines = [
        blueprint.summary ? `Summary：${blueprint.summary}` : '',
        Array.isArray(blueprint.arcs) ? `Arc 数量：${blueprint.arcs.length}` : '',
      ];
      if (Array.isArray(blueprint.arcs)) {
        blueprint.arcs.slice(0, 8).forEach((arc) => {
          blueprintLines.push(`Arc ${arc.arc_number || '?'} · ${arc.title || '未命名'} · ${arc.chapter_start || '?'}-${arc.chapter_end || '?'} · target=${arc.target_size || arc.chapter_count || 0}`);
        });
      }
      document.getElementById('genesis_blueprint_summary').textContent = blueprintLines.filter(Boolean).join('\\n') || '还没有生成整本书蓝图。';
      document.getElementById('genesis_start_writing_btn').disabled = !detail.can_start_writing;
      renderGenesisItemWorkbench(detail);
      renderGenesisTraceList(detail);
    }

    async function saveGenesisStage() {
      if (!currentGenesisProjectId || !currentGenesisDetail) return;
      try {
        const parsed = parseGenesisStageEditor();
        const saved = await patchGenesisStagePayload(parsed, `ui_edit_${currentGenesisStage}`);
        if (saved === null) return;
        setGlobalStatus(`${genesisStageLabel(currentGenesisStage)} 已保存。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 保存失败');
      }
    }

    async function generateGenesisStage(action = 'generate') {
      if (!currentGenesisProjectId) return;
      const normalized = action === 'rerun' ? 'rerun' : 'generate';
      try {
        const generated = await runGenesisAction(async () => {
          currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis/stages/${currentGenesisStage}/${normalized}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              model_profile_id: currentGenesisModelProfileId || null,
            }),
          });
          clearAllGenesisDrafts();
          renderGenesisWorkspace();
          await loadBooks();
          return true;
        });
        if (generated === null) return;
        setGlobalStatus(`${genesisStageLabel(currentGenesisStage)} 已${normalized === 'rerun' ? '重生' : '生成'}。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 阶段执行失败');
      }
    }

    async function lockGenesisStage() {
      if (!currentGenesisProjectId) return;
      try {
        const locked = await runGenesisAction(async () => {
          currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis/stages/${currentGenesisStage}/lock`, {
            method: 'POST',
          });
          clearAllGenesisDrafts();
          renderGenesisWorkspace();
          await loadBooks();
          return true;
        });
        if (locked === null) return;
        setGlobalStatus(`${genesisStageLabel(currentGenesisStage)} 已锁定。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 锁定失败');
      }
    }

    function selectGenesisItemCollection(value) {
      currentGenesisItemCollection = value;
      currentGenesisItemIndex = 0;
      renderGenesisWorkspace();
    }

    function selectGenesisItemIndex(value) {
      const parsed = Number.parseInt(value, 10);
      currentGenesisItemIndex = Number.isFinite(parsed) ? parsed : -1;
      renderGenesisWorkspace();
    }

    async function createGenesisItem() {
      if (!currentGenesisProjectId) return;
      const definition = genesisItemDefinition(currentGenesisStage, currentGenesisItemCollection || defaultGenesisItemCollection());
      if (!definition || !definition.collection) {
        setGlobalStatus('当前阶段不支持子项创建。', 'Genesis 工作台');
        return;
      }
      try {
        const created = await runGenesisAction(async () => {
          const workingPayload = deepCloneJson(currentGenesisPayload(currentGenesisDetail, currentGenesisStage));
          const nextPayload = deepCloneJson(workingPayload);
          if (!Array.isArray(nextPayload[definition.collection])) {
            nextPayload[definition.collection] = [];
          }
          const nextItem = deepCloneJson(definition.template);
          if (nextItem && typeof nextItem === 'object' && typeof nextItem.name === 'string') {
            nextItem.name = `${nextItem.name}${nextPayload[definition.collection].length + 1}`;
          }
          if (nextItem && typeof nextItem === 'object' && typeof nextItem.arc_number === 'number') {
            const nextArcNumber = nextPayload[definition.collection].length + 1;
            nextItem.arc_number = nextArcNumber;
            if (typeof nextItem.title === 'string' && nextItem.title.startsWith('新 Arc')) {
              nextItem.title = `Arc ${nextArcNumber}`;
            }
          }
          if (nextItem && typeof nextItem === 'object' && definition.collection === 'regions') {
            const nextRegionNumber = nextPayload[definition.collection].length + 1;
            nextItem.id = `region-${nextRegionNumber}`;
            nextItem.level = 1;
            nextItem.parent_region_id = '';
          }
          if (nextItem && typeof nextItem === 'object' && definition.collection === 'culture_profiles') {
            const nextCultureNumber = nextPayload[definition.collection].length + 1;
            nextItem.id = `culture-${nextCultureNumber}`;
          }
          nextPayload[definition.collection].push(nextItem);
          currentGenesisItemCollection = definition.collection;
          currentGenesisItemIndex = nextPayload[definition.collection].length - 1;
          const fieldKey = genesisFieldForStage(currentGenesisStage);
          currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [fieldKey]: nextPayload, reason: `ui_create_${currentGenesisItemCollection}` }),
          });
          clearAllGenesisDrafts();
          renderGenesisWorkspace();
          await loadBooks();
          return true;
        });
        if (created === null) return;
        setGlobalStatus(`${definition.label} 已创建。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 子项创建失败');
      }
    }

    async function deleteGenesisItem() {
      if (!currentGenesisProjectId) return;
      const definition = genesisItemDefinition();
      const path = currentGenesisItemPath();
      if (!definition || !definition.collection || !path) {
        setGlobalStatus('先选择一个可删除的子项。', 'Genesis 工作台');
        return;
      }
      if (!window.confirm(`确认删除当前${definition.label}吗？`)) return;
      try {
        const deleted = await runGenesisAction(async () => {
          const nextPayload = deepCloneJson(currentGenesisPayload(currentGenesisDetail, currentGenesisStage));
          const items = Array.isArray(nextPayload[definition.collection]) ? nextPayload[definition.collection] : [];
          items.splice(currentGenesisItemIndex, 1);
          nextPayload[definition.collection] = items;
          currentGenesisItemIndex = items.length ? Math.min(currentGenesisItemIndex, items.length - 1) : -1;
          const fieldKey = genesisFieldForStage(currentGenesisStage);
          currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [fieldKey]: nextPayload, reason: `ui_delete_${definition.collection}` }),
          });
          clearAllGenesisDrafts();
          renderGenesisWorkspace();
          await loadBooks();
          return true;
        });
        if (deleted === null) return;
        setGlobalStatus(`${definition.label} 已删除。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis 子项删除失败');
      }
    }

    async function refineGenesisStage(targetPath = '') {
      if (!currentGenesisProjectId) return;
      const instruction = document.getElementById('genesis_refine_instruction').value.trim();
      if (!instruction) {
        setGlobalStatus('先输入你想让 AI 修改什么。', 'Genesis 工作台');
        return;
      }
      try {
        const refined = await runGenesisAction(async () => {
          const parsed = parseGenesisStageEditor();
          const definition = genesisItemDefinition();
          if (
            targetPath
            && definition?.path
            && !definition?.collection
            && targetPath === definition.path
            && (!parsed || typeof parsed !== 'object' || !Object.prototype.hasOwnProperty.call(parsed, definition.path))
          ) {
            parsed[definition.path] = deepCloneJson(definition.template ?? null);
            document.getElementById('genesis_stage_editor').value = JSON.stringify(parsed, null, 2);
          }
          if (dataSignature(parsed) !== dataSignature(currentGenesisServerPayload(currentGenesisDetail))) {
            const fieldKey = genesisFieldForStage(currentGenesisStage);
            currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ [fieldKey]: parsed, reason: `ui_refine_sync_${currentGenesisStage}` }),
            });
            clearAllGenesisDrafts();
            renderGenesisWorkspace();
          }
          currentGenesisDetail = await requestJson(`/api/projects/${currentGenesisProjectId}/genesis/stages/${currentGenesisStage}/refine`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              instruction,
              target_path: targetPath,
              reason: targetPath ? `ui_refine_item_${currentGenesisStage}` : `ui_refine_stage_${currentGenesisStage}`,
              model_profile_id: currentGenesisModelProfileId || null,
            }),
          });
          clearAllGenesisDrafts();
          renderGenesisWorkspace();
          await loadBooks();
          return true;
        });
        if (refined === null) return;
        setGlobalStatus(targetPath ? `${genesisStageLabel(currentGenesisStage)} 的选中子项已按指令改写。` : `${genesisStageLabel(currentGenesisStage)} 已按指令改写。`, 'Genesis 工作台');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Genesis AI 改写失败');
      }
    }

    async function refineGenesisCurrentStage() {
      await refineGenesisStage('');
    }

    async function refineGenesisSelectedItem() {
      const path = currentGenesisItemPath();
      if (!path) {
        setGlobalStatus('先创建或选择一个子项，再让 AI 只改这个对象。', 'Genesis 工作台');
        return;
      }
      await refineGenesisStage(path);
    }

    async function startWriting(projectId, sourceLabel = 'Genesis 工作台') {
      if (!projectId) return;
      if (!window.confirm('启动写作后，系统会从 Genesis 根蓝图物化 Arc 骨架与当前 Arc 的章节计划，并立即创建写作任务。继续吗？')) return;
      try {
        const result = await requestJson(`/api/projects/${projectId}/start-writing`, {
          method: 'POST',
        });
        await loadBooks();
        await loadTaskCenter();
        setGlobalStatus(result.message || '已启动写作。', sourceLabel);
        if (result.task_id) {
          closeGenesisWorkspace();
          switchTab('task');
          await openTaskDrawer('generation', result.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '启动写作失败');
      }
    }

    async function startWritingFromGenesis() {
      await startWriting(currentGenesisProjectId, 'Genesis 工作台');
    }

    async function startWritingFromList(projectId) {
      await startWriting(projectId, '书本管理');
    }
