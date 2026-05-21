    const PROJECT_CHAPTER_RENDER_BATCH_SIZE = 60;

    function governanceLabel(governance = {}) {
      const parts = [
        governance.progression_mode || 'serial_canon_band_guard',
        governance.auto_band_checkpoint ? 'auto-band-checkpoint' : 'manual-band-checkpoint',
        governance.future_constraints_enabled ? 'future-constraints:on' : 'future-constraints:off',
      ];
      return parts.join(' | ');
    }

    function renderDecisionTimeline(project = {}) {
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '决策时间线', 'task-id'));
      const items = Array.isArray(project.decision_timeline) ? project.decision_timeline : [];
      if (!items.length) {
        card.appendChild(createNode('div', '当前还没有可展示的治理决策记录。', 'meta-line'));
        return card;
      }
      const toolbar = createNode('div', '', 'badge-row');
      const filterSelect = document.createElement('select');
      [
        ['all', '全部'],
        ['arc', 'arc'],
        ['project', 'project'],
        ['band', 'band'],
        ['chapter', 'chapter'],
        ['task', 'task'],
      ].forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        filterSelect.appendChild(option);
      });
      toolbar.appendChild(createLabeledField('范围筛选', filterSelect));
      card.appendChild(toolbar);
      const list = createNode('div', '', 'drawer-grid');
      const rows = [];
      items.slice(0, 40).forEach((item) => {
        const row = createNode('div', '', 'chapter-row');
        const eventId = String(item.id || item.decision_event_id || '').trim();
        if (eventId) row.id = `decision_event_${eventId}`;
        row.dataset.scope = String(item.scope || 'project');
        row.appendChild(createNode('strong', `${item.event_family || 'event'} · ${item.event_type || 'unknown'}`));
        row.appendChild(createNode('div', describeDecisionScope(item), 'meta-line'));
        if (item.summary) row.appendChild(createNode('div', item.summary, 'meta-line'));
        if (item.reason) row.appendChild(createNode('div', `reason：${item.reason}`, 'meta-line'));
        if (item.parent_event_id || item.causal_root_id) {
          row.appendChild(createNode('div', `parent：${item.parent_event_id || '-'} | root：${item.causal_root_id || '-'}`, 'meta-line'));
        }
        if (eventId) {
          const actions = createNode('div', '', 'action-row');
          actions.appendChild(createButton('定位', () => focusDecisionEvent(eventId, filterSelect.value), 'ghost'));
          row.appendChild(actions);
        }
        rows.push(row);
        list.appendChild(row);
      });
      const applyFilter = () => {
        const scope = filterSelect.value || 'all';
        rows.forEach((row) => {
          const visible = scope === 'all' || scope === 'arc' || row.dataset.scope === scope;
          row.style.display = visible ? '' : 'none';
        });
      };
      filterSelect.addEventListener('change', applyFilter);
      applyFilter();
      window.currentDecisionTimelineController = {
        setFilter(scope) {
          filterSelect.value = ['all', 'arc', 'project', 'band', 'chapter', 'task'].includes(scope) ? scope : 'all';
          applyFilter();
        },
      };
      card.appendChild(list);
      return card;
    }

    function appendProjectChapterRow(chapterList, item, project, chapter) {
      const row = createNode('div', '', 'chapter-row');
      const hasDraft = Boolean(chapter.has_draft);
      const hasReview = Boolean(chapter.has_review);
      row.appendChild(createNode('strong', `第${chapter.chapter_number}章《${chapter.title}》`));
      row.appendChild(createNode('div', `状态：${chapterStatusLabel(chapter.status)} | 字数：${chapter.char_count || 0}`, 'meta-line'));
      if (chapter.summary) row.appendChild(createNode('div', chapter.summary, 'meta-line'));
      const actions = createNode('div', '', 'action-row');
      const bodyId = `chapter_body_${item.project_id}_${chapter.chapter_number}`;
      if (hasDraft) {
        actions.appendChild(createButton('查看正文', () => toggleChapterBody(item.project_id, chapter.chapter_number, bodyId), 'ghost'));
        actions.appendChild(createButton('发布到平台', async () => {
          try {
            const chapterDetail = await requestJson(`/api/projects/${item.project_id}/chapters/${chapter.chapter_number}`);
            openTaskModal('upload', {
              project_id: item.project_id,
              platform: project.automation?.publish?.platform || '',
              book_name: project.automation?.publish?.book_name || project.title || item.title || '',
              chapter_title: chapter.title,
              body: chapterDetail.body || '',
              upload_url: project.automation?.publish?.upload_url || '',
              create_if_missing: Boolean(project.automation?.publish?.create_if_missing),
            });
          } catch (error) {
            setGlobalStatus(error.message || String(error), '章节读取失败');
          }
        }, 'secondary'));
      }
      if (chapter.status === 'needs_review' && hasReview) {
        actions.appendChild(createButton('查看 Review', () => showReview(item.project_id, chapter.chapter_number), 'ghost'));
        actions.appendChild(createButton('Review 决策链', () => jumpToReviewDecisionChain(item.project_id, chapter.chapter_number), 'ghost'));
        actions.appendChild(createButton('接受', () => approveReview(item.project_id, chapter.chapter_number, false), 'ghost'));
        actions.appendChild(createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary'));
      }
      if (!actions.childNodes.length) {
        const reason = chapter.status === 'needs_review'
          ? '该章当前停在待处理状态，但还没有可查看的 draft / review。'
          : '该章目前只有计划信息，还没有可查看正文。';
        row.appendChild(createNode('div', reason, 'meta-line'));
      } else {
        row.appendChild(actions);
      }
      const bodyBlock = createNode('div', '', 'chapter-body');
      bodyBlock.id = bodyId;
      row.appendChild(bodyBlock);
      chapterList.appendChild(row);
    }

    function buildQueryString(params = {}) {
      const search = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value === null || value === undefined) return;
        const normalized = String(value || '').trim();
        if (!normalized) return;
        search.set(key, normalized);
      });
      const query = search.toString();
      return query ? `?${query}` : '';
    }

    async function renderCausalReplayCard(item, project = {}) {
      const projectId = item.project_id;
      const latestCheckpoint = project.latest_band_checkpoint || item.generation_control?.latest_band_checkpoint || {};
      const defaultChapter = Number(item.current_chapter || item.generation_control?.current_chapter || 0);
      const defaultTaskId = String(item.task_id || '');
      const defaultBandId = String(latestCheckpoint.band_id || '');
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '因果回放', 'task-id'));
      const toolbar = createNode('div', '', 'action-row');
      const scopeSelect = document.createElement('select');
      ['arc', 'project', 'band', 'chapter', 'task'].forEach((scope) => {
        const option = document.createElement('option');
        option.value = scope;
        option.textContent = scope;
        scopeSelect.appendChild(option);
      });
      scopeSelect.value = 'arc';
      toolbar.appendChild(scopeSelect);
      toolbar.appendChild(createButton('刷新回放', () => loadReplay(), 'ghost'));
      card.appendChild(toolbar);
      const content = createNode('div', '正在加载因果回放...', 'meta-line');
      card.appendChild(content);

      const loadReplay = async () => {
        clearNode(content);
        content.appendChild(createNode('div', '正在加载因果回放...', 'meta-line'));
        const scope = scopeSelect.value || 'project';
        const query = buildQueryString({
          scope,
          arc_id: scope === 'arc' ? (project.active_arc_id || '') : '',
          band_id: scope === 'band' ? defaultBandId : '',
          chapter_number: scope === 'chapter' ? defaultChapter : '',
          task_id: scope === 'task' ? defaultTaskId : '',
        });
        try {
          const replay = await requestJson(`/api/projects/${projectId}/causal-replay${query}`);
          clearNode(content);
          if (!replay?.timeline?.length) {
            content.appendChild(createNode('div', '当前范围没有可回放的治理事件。', 'meta-line'));
            return;
          }
          const summaryLines = [
            replay.root_event?.summary ? `Root：${replay.root_event.summary}` : '',
            replay.current_outcome ? `当前结果：${replay.current_outcome}` : '',
            Array.isArray(replay.linked_review_refs) && replay.linked_review_refs.length ? `关联 Review：${replay.linked_review_refs.length}` : '',
            Array.isArray(replay.linked_checkpoint_refs) && replay.linked_checkpoint_refs.length ? `关联 Checkpoint：${replay.linked_checkpoint_refs.length}` : '',
          ].filter(Boolean);
          if (summaryLines.length) {
            content.appendChild(createNode('div', summaryLines.join('\\n'), 'meta-line'));
          }
          const list = createNode('div', '', 'list');
          replay.timeline.slice(0, 18).forEach((event) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${event.event_type || 'event'} · ${event.summary || '-'}`));
            row.appendChild(createNode('div', [
              event.scope ? `scope=${event.scope}` : '',
              event.chapter_number ? `chapter=${event.chapter_number}` : '',
              event.band_id ? `band=${event.band_id}` : '',
              event.parent_event_id ? `parent=${event.parent_event_id}` : '',
            ].filter(Boolean).join(' | '), 'meta-line'));
            if (event.id) {
              const actions = createNode('div', '', 'action-row');
              actions.appendChild(createButton('跳到时间线', () => focusDecisionEvent(event.id, scope), 'ghost'));
              row.appendChild(actions);
            }
            list.appendChild(row);
          });
          content.appendChild(list);
        } catch (error) {
          clearNode(content);
          content.appendChild(createNode('div', error.message || String(error), 'meta-line'));
        }
      };

      scopeSelect.addEventListener('change', loadReplay);
      await loadReplay();
      return card;
    }

    async function renderGovernanceInsightsCard(item) {
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '治理洞察', 'task-id'));
      const content = createNode('div', '正在加载治理洞察...', 'meta-line');
      card.appendChild(content);
      try {
        const insights = await requestJson(`/api/projects/${item.project_id}/governance-insights`);
        clearNode(content);
        const headline = [
          Array.isArray(insights.most_common_blocking_reasons) && insights.most_common_blocking_reasons.length
            ? `高频阻断：${insights.most_common_blocking_reasons.map((row) => `${row.name}(${row.count})`).join(' / ')}`
            : '高频阻断：暂无',
          Array.isArray(insights.top_override_rule_types) && insights.top_override_rule_types.length
            ? `高频 override：${insights.top_override_rule_types.map((row) => `${row.name}(${row.count})`).join(' / ')}`
            : '高频 override：暂无',
          `forced accept：${insights.forced_accept_frequency || 0}`,
        ];
        content.appendChild(createNode('div', headline.join('\\n'), 'meta-line'));
        if (Array.isArray(insights.recent_band_checkpoint_distribution) && insights.recent_band_checkpoint_distribution.length) {
          content.appendChild(createNode('div', `最近 checkpoint：${insights.recent_band_checkpoint_distribution.map((row) => `${row.name}(${row.count})`).join(' / ')}`, 'meta-line'));
        }
        if (Array.isArray(insights.issue_group_distribution) && insights.issue_group_distribution.length) {
          content.appendChild(createNode('div', `Issue group：${insights.issue_group_distribution.map((row) => `${row.name}(${row.count})`).join(' / ')}`, 'meta-line'));
        }
        if (Array.isArray(insights.recommended_adjustments) && insights.recommended_adjustments.length) {
          const reco = createNode('div', '', 'list');
          insights.recommended_adjustments.slice(0, 5).forEach((entry) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${entry.type || 'adjustment'} · ${entry.target || '-'}`));
            row.appendChild(createNode('div', `${entry.reason || ''}${entry.count ? `\\ncount=${entry.count}` : ''}`, 'meta-line'));
            reco.appendChild(row);
          });
          content.appendChild(reco);
        }
        if (Array.isArray(insights.recent_examples) && insights.recent_examples.length) {
          const examples = createNode('div', '', 'list');
          insights.recent_examples.slice(0, 6).forEach((entry) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${entry.event_type || 'event'} · ${entry.summary || '-'}`));
            row.appendChild(createNode('div', [
              entry.chapter_number ? `chapter=${entry.chapter_number}` : '',
              entry.band_id ? `band=${entry.band_id}` : '',
              entry.blocking_reason ? `block=${entry.blocking_reason}` : '',
            ].filter(Boolean).join(' | '), 'meta-line'));
            if (entry.event_id) {
              const actions = createNode('div', '', 'action-row');
              actions.appendChild(createButton('跳到时间线', () => focusDecisionEvent(entry.event_id, 'all'), 'ghost'));
              row.appendChild(actions);
            }
            examples.appendChild(row);
          });
          content.appendChild(examples);
        }
      } catch (error) {
        clearNode(content);
        content.appendChild(createNode('div', error.message || String(error), 'meta-line'));
      }
      return card;
    }

    async function executeSaveProjectGovernance(projectId, fields, reason) {
      try {
        const payload = { ...fields, reason };
        const result = await requestJson(`/api/projects/${projectId}/governance`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus(result.message || '项目治理设置已保存。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '治理设置保存失败');
      }
    }

    function saveProjectGovernanceFromDrawer(projectId, fields) {
      openGovernanceActionModal({
        title: '保存治理设置',
        description: '项目级治理会影响后续默认行为；本次修改原因会进入决策时间线。',
        confirmLabel: '保存治理设置',
        errorTitle: '治理设置保存失败',
        onSubmit: ({ reason }) => executeSaveProjectGovernance(projectId, fields, reason),
      });
    }

    async function executeCreateManualCheckpoint(projectId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/manual-checkpoints`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('manual checkpoint 已创建。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'manual checkpoint 创建失败');
      }
    }

    function createManualCheckpointFromDrawer(projectId, defaults = {}) {
      openGovernanceActionModal({
        title: '插入 Manual Checkpoint',
        description: 'v1 只支持章开始前、章 accepted 后、band 结束处三个边界。',
        confirmLabel: '创建 Checkpoint',
        errorTitle: 'manual checkpoint 创建失败',
        fields: [
          {
            name: 'boundary_kind',
            label: '边界',
            type: 'select',
            value: defaults.boundary_kind || 'band_end',
            options: ['chapter_start', 'chapter_accepted', 'band_end'],
          },
          {
            name: 'boundary_chapter',
            label: '章节号',
            type: 'number',
            value: defaults.boundary_chapter || 0,
            min: 0,
            step: 1,
          },
        ],
        onSubmit: ({ reason, boundary_kind, boundary_chapter }) => executeCreateManualCheckpoint(projectId, {
          boundary_kind: String(boundary_kind || '').trim(),
          boundary_chapter: Number.isFinite(Number(boundary_chapter)) ? Number(boundary_chapter) : 0,
          reason,
        }),
      });
    }

    async function executeCreateNarrativeConstraint(projectId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/constraints`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('narrative constraint 已创建。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'narrative constraint 创建失败');
      }
    }

    function createNarrativeConstraintFromDrawer(projectId) {
      openGovernanceActionModal({
        title: '新增 Narrative Constraint',
        description: 'constraint 可保存展示；只有启用 future constraints 后才参与 review/checkpoint 判定。',
        confirmLabel: '创建 Constraint',
        errorTitle: 'narrative constraint 创建失败',
        fields: [
          { name: 'constraint_type', label: '约束类型', type: 'select', value: 'character_availability', options: ['character_availability', 'secret_withhold', 'relationship_preserve', 'thread_keep_open', 'location_availability', 'rule_preserve'] },
          { name: 'level', label: '级别', type: 'select', value: 'hard', options: ['hard', 'soft', 'hint'] },
          { name: 'subject_name', label: '主体', type: 'text', value: '' },
          { name: 'description', label: '说明', type: 'text', value: '' },
          { name: 'effective_from_chapter', label: '起始章节', type: 'number', value: 1, min: 1, step: 1 },
          { name: 'protect_until_chapter', label: '保护到章节', type: 'number', value: 0, min: 0, step: 1 },
        ],
        onSubmit: ({ reason, constraint_type, level, subject_name, description, effective_from_chapter, protect_until_chapter }) => executeCreateNarrativeConstraint(projectId, {
          constraint_type,
          level,
          subject_name,
          description,
          effective_from_chapter: Number.isFinite(Number(effective_from_chapter)) ? Number(effective_from_chapter) : 1,
          protect_until_chapter: Number.isFinite(Number(protect_until_chapter)) ? Number(protect_until_chapter) : 0,
          reason,
        }),
      });
    }

    async function executeUpdateNarrativeConstraint(projectId, constraintId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/constraints/${constraintId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('narrative constraint 已更新。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'narrative constraint 更新失败');
      }
    }

    function editNarrativeConstraintFromDrawer(projectId, constraint) {
      if (!constraint?.id) return;
      openGovernanceActionModal({
        title: '编辑 Narrative Constraint',
        description: '修改会写入 constraint_updated 决策事件。',
        confirmLabel: '保存 Constraint',
        errorTitle: 'narrative constraint 更新失败',
        fields: [
          { name: 'constraint_type', label: '约束类型', type: 'select', value: constraint.constraint_type || 'character_availability', options: ['character_availability', 'secret_withhold', 'relationship_preserve', 'thread_keep_open', 'location_availability', 'rule_preserve'] },
          { name: 'level', label: '级别', type: 'select', value: constraint.level || 'hard', options: ['hard', 'soft', 'hint'] },
          { name: 'subject_name', label: '主体', type: 'text', value: constraint.subject_name || '' },
          { name: 'description', label: '说明', type: 'text', value: constraint.description || '' },
          { name: 'effective_from_chapter', label: '起始章节', type: 'number', value: constraint.effective_from_chapter || 1, min: 1, step: 1 },
          { name: 'protect_until_chapter', label: '保护到章节', type: 'number', value: constraint.protect_until_chapter || 0, min: 0, step: 1 },
        ],
        onSubmit: ({ reason, constraint_type, level, subject_name, description, effective_from_chapter, protect_until_chapter }) => executeUpdateNarrativeConstraint(projectId, constraint.id, {
          constraint_type,
          level,
          subject_name,
          description,
          effective_from_chapter: Number.isFinite(Number(effective_from_chapter)) ? Number(effective_from_chapter) : 1,
          protect_until_chapter: Number.isFinite(Number(protect_until_chapter)) ? Number(protect_until_chapter) : 0,
          reason,
        }),
      });
    }

    function archiveNarrativeConstraintFromDrawer(projectId, constraint) {
      if (!constraint?.id) return;
      openGovernanceActionModal({
        title: '停用 Narrative Constraint',
        description: '停用后 constraint 仍会保留展示，但不再作为 active constraint。',
        confirmLabel: '停用 Constraint',
        errorTitle: 'narrative constraint 停用失败',
        onSubmit: ({ reason }) => executeUpdateNarrativeConstraint(projectId, constraint.id, {
          status: 'inactive',
          reason,
        }),
      });
    }

    function normalizeTaskContractItems(rawText) {
      let parsed = [];
      try {
        parsed = JSON.parse(String(rawText || '[]'));
      } catch (error) {
        throw new Error('Task contract 必须是 JSON 数组。');
      }
      if (!Array.isArray(parsed)) {
        throw new Error('Task contract 必须是 JSON 数组。');
      }
      return parsed.map((item) => ({
        task_type: String(item?.task_type || 'plot_advance').trim(),
        description: String(item?.description || '').trim(),
        target_name: String(item?.target_name || '').trim(),
        required_keywords: Array.isArray(item?.required_keywords) ? item.required_keywords.map((entry) => String(entry || '').trim()).filter(Boolean) : [],
        forbidden_keywords: Array.isArray(item?.forbidden_keywords) ? item.forbidden_keywords.map((entry) => String(entry || '').trim()).filter(Boolean) : [],
        source: String(item?.source || 'manual').trim() || 'manual',
      }));
    }

    async function executeUpdateTaskContract(projectId, scope, identifier, items, reason) {
      const url = scope === 'band'
        ? `/api/projects/${projectId}/bands/${encodeURIComponent(identifier)}/task-contract`
        : `/api/projects/${projectId}/chapters/${Number(identifier || 0)}/task-contract`;
      await requestJson(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, reason }),
      });
      setGlobalStatus('task contract 已更新。', '治理设置');
      await loadBooks();
      if (currentDrawerTask?.project_id === projectId) {
        await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
      }
    }

    async function editTaskContractFromDrawer(projectId, scope, identifier) {
      if (!projectId || !identifier) {
        setGlobalStatus('缺少 task contract 目标。', '治理设置');
        return;
      }
      const url = scope === 'band'
        ? `/api/projects/${projectId}/bands/${encodeURIComponent(identifier)}/task-contract`
        : `/api/projects/${projectId}/chapters/${Number(identifier || 0)}/task-contract`;
      try {
        const current = await requestJson(url);
        openGovernanceActionModal({
          title: scope === 'band' ? `编辑 Band Task Contract · ${identifier}` : `编辑 Chapter Task Contract · 第${identifier}章`,
          description: '填写 PlanTaskItem JSON 数组。修改会进入决策时间线，review/checkpoint 会使用这份合同判断规划履约。',
          confirmLabel: '保存 Task Contract',
          errorTitle: 'task contract 更新失败',
          fields: [
            {
              name: 'items_json',
              label: 'PlanTaskItem JSON',
              type: 'textarea',
              rows: 10,
              value: JSON.stringify(current.items || [], null, 2),
            },
          ],
          onSubmit: ({ reason, items_json }) => executeUpdateTaskContract(
            projectId,
            scope,
            identifier,
            normalizeTaskContractItems(items_json),
            reason
          ),
        });
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'task contract 读取失败');
      }
    }

    async function executeApproveBandCheckpoint(projectId, bandId, status, reason) {
      try {
        await requestJson(`/api/projects/${projectId}/bands/${bandId}/checkpoint/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status, reason }),
        });
        setGlobalStatus('band checkpoint 已更新。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'band checkpoint 更新失败');
      }
    }

    function approveBandCheckpointFromDrawer(projectId, bandId, status = 'overridden') {
      openGovernanceActionModal({
        title: status === 'pass' ? `放行 Checkpoint · ${bandId}` : `Override Checkpoint · ${bandId}`,
        description: status === 'pass'
          ? '人工确认当前 band checkpoint 可以 pass。'
          : '当前 override 会进入治理时间线，后续 insight 会统计这类人工放行。',
        confirmLabel: status === 'pass' ? 'Pass Checkpoint' : 'Override Checkpoint',
        errorTitle: 'band checkpoint 更新失败',
        onSubmit: ({ reason }) => executeApproveBandCheckpoint(projectId, bandId, status, reason),
      });
    }

    function renderGovernanceCard(item, project = {}) {
      const governance = project.governance || {};
      const blockingReason = project.blocking_reason || item.generation_control?.blocking_reason || {};
      const latestCheckpoint = project.latest_band_checkpoint || item.generation_control?.latest_band_checkpoint || null;
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '治理设置', 'task-id'));
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', governance.default_operation_mode || 'blackbox', 'badge'));
      badges.appendChild(createNode('span', governance.progression_mode || 'serial_canon_band_guard', 'badge'));
      if (governance.auto_band_checkpoint) badges.appendChild(createNode('span', 'auto band checkpoint', 'badge ok'));
      badges.appendChild(createNode('span', governance.future_constraints_enabled ? 'future constraints 参与判定' : 'future constraints 仅保存/展示', governance.future_constraints_enabled ? 'badge ok' : 'badge warn'));
      card.appendChild(badges);
      card.appendChild(createNode('div', `当前策略：${governanceLabel(governance)}\n下一 gate：${project.next_gate || item.generation_control?.next_gate || '-'}\n人工检查间隔：${governance.review_interval_chapters || 0}`, 'meta-line'));
      if (blockingReason?.code) {
        card.appendChild(createNode('div', `阻断原因：${blockingReason.message || blockingReason.code}${blockingReason.detail ? `\n${blockingReason.detail}` : ''}`, 'meta-line'));
        if (blockingReason.decision_event_id) {
          const blockingActions = createNode('div', '', 'action-row');
          blockingActions.appendChild(createButton('跳到阻断决策', () => {
            if (!focusDecisionEvent(blockingReason.decision_event_id, 'all')) {
              setGlobalStatus('当前阻断原因没有映射到可见的决策事件。', '治理时间线');
            }
          }, 'ghost'));
          card.appendChild(blockingActions);
        }
      }
      if (latestCheckpoint) {
        card.appendChild(createNode('div', `最新 band checkpoint：${latestCheckpoint.band_id || '-'} | ${latestCheckpoint.status || 'pending'}${latestCheckpoint.summary ? `\n${latestCheckpoint.summary}` : ''}`, 'meta-line'));
        if (Array.isArray(latestCheckpoint.issues) && latestCheckpoint.issues.length) {
          card.appendChild(createNode('div', latestCheckpoint.issues.slice(0, 5).map((issue) => [
            issue.severity || 'info',
            issue.issue_group || '',
            issue.category || '',
            issue.description || issue.code || '',
          ].filter(Boolean).join(' · ')).join('\\n'), 'meta-line'));
        }
        if (Array.isArray(latestCheckpoint.decision_refs) && latestCheckpoint.decision_refs.length) {
          const checkpointActions = createNode('div', '', 'action-row');
          checkpointActions.appendChild(createButton('查看 Checkpoint 决策链', () => jumpToCheckpointDecisionChain(latestCheckpoint), 'ghost'));
          card.appendChild(checkpointActions);
        }
      }

      const form = createNode('div', '', 'drawer-grid');
      const operationMode = document.createElement('select');
      ['blackbox', 'copilot', 'checkpoint'].forEach((value) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        operationMode.appendChild(option);
      });
      operationMode.value = governance.default_operation_mode || 'blackbox';
      form.appendChild(createLabeledField('默认运行模式', operationMode));

      const progressionMode = document.createElement('select');
      ['serial_canon', 'serial_canon_band_guard'].forEach((value) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        progressionMode.appendChild(option);
      });
      progressionMode.value = governance.progression_mode || 'serial_canon_band_guard';
      form.appendChild(createLabeledField('推进策略', progressionMode));

      const reviewInterval = document.createElement('input');
      reviewInterval.type = 'number';
      reviewInterval.min = '0';
      reviewInterval.max = '200';
      reviewInterval.step = '1';
      reviewInterval.value = String(governance.review_interval_chapters || 0);
      form.appendChild(createLabeledField('每 N 章人工检查', reviewInterval));

      const autoBandCheckpoint = document.createElement('input');
      autoBandCheckpoint.type = 'checkbox';
      autoBandCheckpoint.checked = Boolean(governance.auto_band_checkpoint);
      const autoBandCheckpointWrap = document.createElement('label');
      autoBandCheckpointWrap.className = 'checkbox';
      autoBandCheckpointWrap.appendChild(autoBandCheckpoint);
      autoBandCheckpointWrap.appendChild(document.createTextNode('自动 band checkpoint'));
      form.appendChild(autoBandCheckpointWrap);

      const manualCheckpoint = document.createElement('input');
      manualCheckpoint.type = 'checkbox';
      manualCheckpoint.checked = Boolean(governance.manual_checkpoints_enabled);
      const manualCheckpointWrap = document.createElement('label');
      manualCheckpointWrap.className = 'checkbox';
      manualCheckpointWrap.appendChild(manualCheckpoint);
      manualCheckpointWrap.appendChild(document.createTextNode('允许 manual checkpoint'));
      form.appendChild(manualCheckpointWrap);

      const futureConstraints = document.createElement('input');
      futureConstraints.type = 'checkbox';
      futureConstraints.checked = Boolean(governance.future_constraints_enabled);
      const futureConstraintsWrap = document.createElement('label');
      futureConstraintsWrap.className = 'checkbox';
      futureConstraintsWrap.appendChild(futureConstraints);
      futureConstraintsWrap.appendChild(document.createTextNode('启用 future constraints'));
      form.appendChild(futureConstraintsWrap);

      card.appendChild(form);

      const actions = createNode('div', '', 'action-row');
      actions.appendChild(createButton('保存治理设置', () => saveProjectGovernanceFromDrawer(item.project_id, {
        default_operation_mode: operationMode.value,
        progression_mode: progressionMode.value,
        review_interval_chapters: normalizeReviewInterval(reviewInterval.value),
        auto_band_checkpoint: autoBandCheckpoint.checked,
        manual_checkpoints_enabled: manualCheckpoint.checked,
        future_constraints_enabled: futureConstraints.checked,
      }), 'primary'));
      actions.appendChild(createButton('插入 Manual Checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
        boundary_kind: latestCheckpoint?.boundary_kind || 'band_end',
        boundary_chapter: latestCheckpoint?.boundary_chapter || item.current_chapter || 0,
      }), 'secondary'));
      const contractChapter = Number(item.current_chapter || project.generation_control?.next_chapter || 0);
      if (contractChapter > 0) {
        actions.appendChild(createButton('编辑本章 Task Contract', () => editTaskContractFromDrawer(item.project_id, 'chapter', contractChapter), 'ghost'));
      }
      if (latestCheckpoint?.band_id) {
        actions.appendChild(createButton('编辑 Band Task Contract', () => editTaskContractFromDrawer(item.project_id, 'band', latestCheckpoint.band_id), 'ghost'));
      }
      actions.appendChild(createButton('新增 Constraint', () => createNarrativeConstraintFromDrawer(item.project_id), 'ghost'));
      if (latestCheckpoint?.band_id && ['warn', 'fail', 'pending', 'error'].includes(String(latestCheckpoint.status || ''))) {
        actions.appendChild(createButton('Override Checkpoint', () => approveBandCheckpointFromDrawer(item.project_id, latestCheckpoint.band_id, 'overridden'), 'ghost'));
      }
      card.appendChild(actions);

      const constraints = Array.isArray(project.narrative_constraints) ? project.narrative_constraints : [];
      if (constraints.length) {
        const constraintsList = createNode('div', '', 'list');
        constraints.slice(0, 8).forEach((constraint) => {
          const row = createNode('div', '', 'list-item');
          row.appendChild(createNode('strong', `${constraint.status || 'active'} · ${constraint.level || 'hard'} · ${constraint.constraint_type || ''}`));
          row.appendChild(createNode('div', `${constraint.subject_name || constraint.description || '-'}${constraint.protect_until_chapter ? `\nprotect_until=${constraint.protect_until_chapter}` : ''}`, 'meta-line'));
          const constraintActions = createNode('div', '', 'action-row');
          constraintActions.appendChild(createButton('编辑', () => editNarrativeConstraintFromDrawer(item.project_id, constraint), 'ghost'));
          if (String(constraint.status || 'active') === 'active') {
            constraintActions.appendChild(createButton('停用', () => archiveNarrativeConstraintFromDrawer(item.project_id, constraint), 'ghost'));
          }
          row.appendChild(constraintActions);
          constraintsList.appendChild(row);
        });
        card.appendChild(constraintsList);
      }
      return card;
    }

    async function renderGenerationDrawer(item, drawerProject = null, drawerChapters = null, drawerChapterPage = null) {
      const body = document.getElementById('drawer_body');
      let projectDetail = drawerProject || null;
      let projectChapterPage = drawerChapterPage || null;
      let projectChapters = Array.isArray(drawerChapters) ? drawerChapters : null;
      let projectLoadError = null;
      if (item.project_id) {
        try {
          projectDetail = projectDetail || await loadProjectDetail(item.project_id);
          if (!projectChapters) {
            projectChapterPage = await loadProjectChapterPage(item.project_id, 0, PROJECT_CHAPTER_RENDER_BATCH_SIZE);
            projectChapters = projectChapterPage.chapters;
          }
        } catch (error) {
          projectLoadError = error;
          projectChapters = projectChapters || [];
          projectChapterPage = projectChapterPage || {
            total: projectChapters.length,
            offset: 0,
            limit: projectChapters.length,
            has_more: false,
            chapters: projectChapters,
          };
        }
      }
      projectChapterPage = projectChapterPage || {
        total: Number(projectDetail?.chapter_count || projectChapters?.length || 0),
        offset: 0,
        limit: projectChapters?.length || 0,
        has_more: Number(projectDetail?.chapter_count || 0) > (projectChapters?.length || 0),
        chapters: projectChapters || [],
      };

      body.appendChild(renderGenerationControlPanel(item, projectDetail, projectChapters || []));
      if (item.project_id && projectDetail) {
        body.appendChild(renderGovernanceCard(item, projectDetail));
        body.appendChild(await renderCausalReplayCard(item, projectDetail));
        body.appendChild(await renderGovernanceInsightsCard(item));
      }

      const top = createNode('section', '', 'detail-card');
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
      if (item.project_id) badges.appendChild(createNode('span', `Project ${item.project_id}`, 'badge'));
      top.appendChild(badges);
      top.appendChild(renderMacroProgress(item));
      body.appendChild(top);

      const metrics = createNode('section', '', 'progress-grid');
      const control = item.generation_control || {};
      const acceptedCount = Array.isArray(control.accepted_chapters)
        ? control.accepted_chapters.length
        : (Array.isArray(item.completed_chapters) ? item.completed_chapters.length : 0);
      const generatedCount = Array.isArray(control.generated_chapters)
        ? control.generated_chapters.length
        : (
          acceptedCount
          + (Array.isArray(control.pending_review_chapters) ? control.pending_review_chapters.length : 0)
          + (Array.isArray(control.drafted_chapters) ? control.drafted_chapters.length : 0)
        );
      const reviewBlockedCount = Array.isArray(control.pending_review_chapters)
        ? control.pending_review_chapters.length
        : (Array.isArray(item.paused_chapters) ? item.paused_chapters.length : 0);
      [
        ['已生成', generatedCount],
        ['已接受', acceptedCount],
        ['失败', Array.isArray(item.failed_chapters) ? item.failed_chapters.length : 0],
        ['待 Review', reviewBlockedCount],
        ['当前章', item.current_chapter || 0],
      ].forEach(([label, value]) => {
        const metric = createNode('div', '', 'metric');
        metric.appendChild(createNode('strong', String(value)));
        metric.appendChild(createNode('span', label));
        metrics.appendChild(metric);
      });
      body.appendChild(metrics);

      const misc = createNode('section', '', 'detail-card');
      misc.appendChild(createNode('div', `请求章节数：${item.requested_chapters || 0}`, 'meta-line'));
      if (item.message) misc.appendChild(createNode('div', `消息：${item.message}`, 'meta-line'));
      if (item.error) misc.appendChild(createNode('div', `错误：${item.error}`, 'meta-line'));
      if (Array.isArray(item.failed_chapters) && item.failed_chapters.length) misc.appendChild(createNode('div', `失败章节：${item.failed_chapters.join(', ')}`, 'meta-line'));
      if (Array.isArray(item.paused_chapters) && item.paused_chapters.length) misc.appendChild(createNode('div', `暂停章节：${item.paused_chapters.join(', ')}`, 'meta-line'));
      if (Array.isArray(item.frozen_artifacts) && item.frozen_artifacts.length) misc.appendChild(createNode('div', `冻结产物：${item.frozen_artifacts.join('\\n')}`, 'meta-line'));
      const controlLines = [
        `计划状态：${control.plan_state || '-'}`,
        `写作状态：${control.writing_state || '-'}`,
        `Review：${control.review_state || '-'}`,
        `下一章：${control.next_chapter || 0}`,
        `距人工检查：${control.review_interval_chapters ? control.chapters_until_review : '未设置'}`,
        `距 replan 可触发：${control.chapters_until_replan_eligible || 0}`,
      ];
      misc.appendChild(createNode('div', controlLines.join('\\n'), 'meta-line'));
      body.appendChild(misc);

      if (!item.project_id) {
        body.appendChild(renderChapterTimeline(item, []));
        return;
      }
      if (projectLoadError) {
        body.appendChild(createNode('div', projectLoadError.message || String(projectLoadError), 'detail-card'));
        return;
      }

      const project = projectDetail || {};
      const chapters = projectChapters || [];
      try {
        body.appendChild(renderChapterTimeline(item, chapters));
        const automation = project.automation || {};
        const automationCard = createNode('section', '', 'detail-card');
        automationCard.appendChild(createNode('div', '每日自动化', 'task-id'));
        const automationBadges = createNode('div', '', 'badge-row');
        const writeQuota = automation.daily_write_quota || automation.daily_chapter_quota || 1;
        const planQuota = automation.daily_plan_quota || 0;
        const reviewQuota = automation.daily_review_quota || 0;
        const publishQuota = automation.daily_publish_quota || 0;
        automationBadges.appendChild(createNode('span', automation.enabled ? '已开启' : '已关闭', `badge ${automation.enabled ? 'ok' : 'warn'}`));
        automationBadges.appendChild(createNode('span', `写 ${writeQuota} 章`, 'badge'));
        if (planQuota) automationBadges.appendChild(createNode('span', `规划 ${planQuota}`, 'badge'));
        if (reviewQuota) automationBadges.appendChild(createNode('span', `提醒 review ${reviewQuota}`, 'badge'));
        if (publishQuota) automationBadges.appendChild(createNode('span', `发布 ${publishQuota}`, 'badge'));
        automationBadges.appendChild(createNode('span', `${automation.daily_start_time || '09:00'} 开始`, 'badge'));
        if (automation.auto_publish) automationBadges.appendChild(createNode('span', '完成后自动发布', 'badge ok'));
        automationCard.appendChild(automationBadges);

        const automationStatus = [
          automation.last_scheduler_at ? `上次调度：${automation.last_scheduler_at}` : '',
          automation.last_scheduler_action ? `动作：${automation.last_scheduler_action}` : '',
          automation.last_scheduler_message ? `说明：${automation.last_scheduler_message}` : '',
          automation.last_scheduler_task_id ? `任务：${automation.last_scheduler_task_id}` : '',
        ].filter(Boolean).join('\\n');
        if (automationStatus) {
          automationCard.appendChild(createNode('div', automationStatus, 'meta-line'));
        }

        const automationForm = createNode('div', '', 'drawer-grid');
        const enabledInput = document.createElement('input');
        enabledInput.type = 'checkbox';
        enabledInput.checked = Boolean(automation.enabled);
        const enabledWrap = document.createElement('label');
        enabledWrap.className = 'checkbox';
        enabledWrap.appendChild(enabledInput);
        enabledWrap.appendChild(document.createTextNode('到点后自动处理这个书本的生成任务'));
        automationForm.appendChild(enabledWrap);

        const autoPublishInput = document.createElement('input');
        autoPublishInput.type = 'checkbox';
        autoPublishInput.checked = Boolean(automation.auto_publish);
        const autoPublishWrap = document.createElement('label');
        autoPublishWrap.className = 'checkbox';
        autoPublishWrap.appendChild(autoPublishInput);
        autoPublishWrap.appendChild(document.createTextNode('生成完成后自动创建发布任务'));
        automationForm.appendChild(autoPublishWrap);

        const stopReviewInput = document.createElement('input');
        stopReviewInput.type = 'checkbox';
        stopReviewInput.checked = automation.stop_when_review_pending !== false;
        const stopReviewWrap = document.createElement('label');
        stopReviewWrap.className = 'checkbox';
        stopReviewWrap.appendChild(stopReviewInput);
        stopReviewWrap.appendChild(document.createTextNode('存在待 review 章节时暂停自动生成'));
        automationForm.appendChild(stopReviewWrap);

        const timeInput = document.createElement('input');
        timeInput.type = 'time';
        timeInput.value = automation.daily_start_time || '09:00';
        automationForm.appendChild(createLabeledField('每日开始时间', timeInput));

        const planQuotaInput = document.createElement('input');
        planQuotaInput.type = 'number';
        planQuotaInput.min = '0';
        planQuotaInput.max = '20';
        planQuotaInput.step = '1';
        planQuotaInput.value = String(planQuota);
        automationForm.appendChild(createLabeledField('每日规划额度', planQuotaInput));

        const quotaInput = document.createElement('input');
        quotaInput.type = 'number';
        quotaInput.min = '1';
        quotaInput.max = '20';
        quotaInput.step = '1';
        quotaInput.value = String(writeQuota);
        automationForm.appendChild(createLabeledField('每天最多写几章', quotaInput));

        const reviewQuotaInput = document.createElement('input');
        reviewQuotaInput.type = 'number';
        reviewQuotaInput.min = '0';
        reviewQuotaInput.max = '20';
        reviewQuotaInput.step = '1';
        reviewQuotaInput.value = String(reviewQuota);
        automationForm.appendChild(createLabeledField('每日 review 提醒额度', reviewQuotaInput));

        const publishQuotaInput = document.createElement('input');
        publishQuotaInput.type = 'number';
        publishQuotaInput.min = '0';
        publishQuotaInput.max = '20';
        publishQuotaInput.step = '1';
        publishQuotaInput.value = String(publishQuota);
        automationForm.appendChild(createLabeledField('每日发布额度', publishQuotaInput));

        const platformSelect = document.createElement('select');
        const selectedPlatform = automation.publish?.platform || '';
        const platformOptions = Array.isArray(platformsState) ? platformsState : [];
        if (!selectedPlatform) {
          const emptyOption = document.createElement('option');
          emptyOption.value = '';
          emptyOption.textContent = '选择发布平台';
          platformSelect.appendChild(emptyOption);
        }
        if (selectedPlatform && !platformOptions.some((entry) => entry.platform_id === selectedPlatform)) {
          const currentOption = document.createElement('option');
          currentOption.value = selectedPlatform;
          currentOption.textContent = selectedPlatform;
          platformSelect.appendChild(currentOption);
        }
        platformOptions.forEach((entry) => {
          const option = document.createElement('option');
          option.value = entry.platform_id;
          option.textContent = entry.display_name || entry.platform_id;
          platformSelect.appendChild(option);
        });
        platformSelect.value = selectedPlatform;
        automationForm.appendChild(createLabeledField('自动发布平台', platformSelect));

        const bookNameInput = document.createElement('input');
        bookNameInput.type = 'text';
        bookNameInput.value = automation.publish?.book_name || project.title || '';
        automationForm.appendChild(createLabeledField('发布时作品名', bookNameInput));

        const uploadUrlInput = document.createElement('input');
        uploadUrlInput.type = 'text';
        uploadUrlInput.value = automation.publish?.upload_url || '';
        automationForm.appendChild(createLabeledField('固定上传页 URL', uploadUrlInput));

        const createIfMissingInput = document.createElement('input');
        createIfMissingInput.type = 'checkbox';
        createIfMissingInput.checked = Boolean(automation.publish?.create_if_missing);
        const createIfMissingWrap = document.createElement('label');
        createIfMissingWrap.className = 'checkbox';
        createIfMissingWrap.appendChild(createIfMissingInput);
        createIfMissingWrap.appendChild(document.createTextNode('若作品不存在则自动新建'));
        automationForm.appendChild(createIfMissingWrap);

        const audienceInput = document.createElement('input');
        audienceInput.type = 'text';
        audienceInput.value = automation.publish?.book_meta?.audience || '';
        automationForm.appendChild(createLabeledField('目标读者', audienceInput));

        const primaryCategoryInput = document.createElement('input');
        primaryCategoryInput.type = 'text';
        primaryCategoryInput.value = automation.publish?.book_meta?.primary_category || '';
        automationForm.appendChild(createLabeledField('主分类', primaryCategoryInput));

        const protagonistNamesInput = document.createElement('input');
        protagonistNamesInput.type = 'text';
        protagonistNamesInput.value = Array.isArray(automation.publish?.book_meta?.protagonist_names)
          ? automation.publish.book_meta.protagonist_names.join(', ')
          : '';
        automationForm.appendChild(createLabeledField('主角名（逗号分隔）', protagonistNamesInput));

        const introInput = document.createElement('textarea');
        introInput.rows = 4;
        introInput.value = automation.publish?.book_meta?.intro || '';
        automationForm.appendChild(createLabeledField('作品简介', introInput));

        const publishBindingsSummary = formatPublishBindingsSummary(automation);
        if (publishBindingsSummary) {
          automationCard.appendChild(createNode('div', `已绑定平台：${publishBindingsSummary}`, 'meta-line'));
        }
        automationCard.appendChild(automationForm);
        automationCard.appendChild(createNode(
          'div',
          '说明：系统每天只会在设定时间之后检查一次；如果仍有章节待 review，当天会跳过自动生成。',
          'meta-line',
        ));
        automationCard.appendChild(createButton('保存自动化设置', async () => {
          try {
            const payload = {
              enabled: enabledInput.checked,
              daily_start_time: timeInput.value || '09:00',
              daily_chapter_quota: Number(quotaInput.value || 1),
              daily_plan_quota: Number(planQuotaInput.value || 0),
              daily_write_quota: Number(quotaInput.value || 1),
              daily_review_quota: Number(reviewQuotaInput.value || 0),
              daily_publish_quota: Number(publishQuotaInput.value || 0),
              stop_when_review_pending: stopReviewInput.checked,
              auto_publish: autoPublishInput.checked,
              publish: {
                platform: platformSelect.value || '',
                book_name: bookNameInput.value.trim(),
                upload_url: uploadUrlInput.value.trim(),
                create_if_missing: createIfMissingInput.checked,
                book_meta: {
                  audience: audienceInput.value.trim(),
                  primary_category: primaryCategoryInput.value.trim(),
                  protagonist_names: protagonistNamesInput.value
                    .split(',')
                    .map((name) => name.trim())
                    .filter(Boolean),
                  intro: introInput.value.trim(),
                },
              },
            };
            const result = await requestJson(`/api/projects/${item.project_id}/automation`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
            setGlobalStatus(result.message || '自动化设置已保存。', '书本自动化');
            await loadBooks();
            if (currentDrawerTask?.project_id === item.project_id) {
              await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
            }
          } catch (error) {
            setGlobalStatus(error.message || String(error), '自动化设置保存失败');
          }
        }, 'primary'));
        body.appendChild(automationCard);

        const section = createNode('section', '', 'detail-card');
        section.appendChild(createNode('div', '项目章节', 'task-id'));
        const chapterList = createNode('div', '', 'drawer-grid');
        let visibleChapters = chapters.filter((chapter) => chapter.status !== 'planned');
        let totalChapterCount = Number(projectChapterPage?.total || visibleChapters.length);
        let nextChapterOffset = Number(projectChapterPage?.offset || 0) + (Array.isArray(projectChapterPage?.chapters) ? projectChapterPage.chapters.length : visibleChapters.length);
        let hasMoreChapterPages = Boolean(projectChapterPage?.has_more);
        if (!visibleChapters.length) {
          chapterList.appendChild(createNode('div', '项目已创建，但还没有可展示的已生成章节。', 'empty'));
        } else {
          let renderedChapterCount = Math.min(PROJECT_CHAPTER_RENDER_BATCH_SIZE, visibleChapters.length);
          const pagingStatus = createNode('div', '', 'meta-line');
          const loadMoreButton = createButton('加载更多章节', async () => {
            if (renderedChapterCount < visibleChapters.length) {
              renderedChapterCount = Math.min(
                renderedChapterCount + PROJECT_CHAPTER_RENDER_BATCH_SIZE,
                visibleChapters.length,
              );
              renderVisibleProjectChapters();
              return;
            }
            if (!hasMoreChapterPages || !item.project_id) return;
            loadMoreButton.disabled = true;
            try {
              const page = await loadProjectChapterPage(item.project_id, nextChapterOffset, PROJECT_CHAPTER_RENDER_BATCH_SIZE);
              const incoming = (Array.isArray(page.chapters) ? page.chapters : [])
                .filter((chapter) => chapter.status !== 'planned');
              visibleChapters = visibleChapters.concat(incoming);
              totalChapterCount = Number(page.total || totalChapterCount || visibleChapters.length);
              nextChapterOffset = Number(page.offset || nextChapterOffset) + (Array.isArray(page.chapters) ? page.chapters.length : incoming.length);
              hasMoreChapterPages = Boolean(page.has_more);
              renderedChapterCount = visibleChapters.length;
              renderVisibleProjectChapters();
            } catch (error) {
              setGlobalStatus(error.message || String(error), '章节分页加载失败');
            } finally {
              loadMoreButton.disabled = false;
            }
          }, 'ghost');
          const renderVisibleProjectChapters = () => {
            clearNode(chapterList);
            visibleChapters
              .slice(0, renderedChapterCount)
              .forEach((chapter) => appendProjectChapterRow(chapterList, item, project, chapter));
            pagingStatus.textContent = `已显示 ${renderedChapterCount} / ${totalChapterCount || visibleChapters.length} 章`;
            loadMoreButton.style.display = renderedChapterCount < visibleChapters.length || hasMoreChapterPages ? '' : 'none';
          };
          renderVisibleProjectChapters();
          if (visibleChapters.length > PROJECT_CHAPTER_RENDER_BATCH_SIZE || hasMoreChapterPages) {
            section.appendChild(pagingStatus);
            const pagingActions = createNode('div', '', 'action-row');
            pagingActions.appendChild(loadMoreButton);
            section.appendChild(pagingActions);
          }
        }
        section.appendChild(chapterList);
        body.appendChild(section);
        body.appendChild(renderDecisionTimeline(project));
      } catch (error) {
        body.appendChild(createNode('div', error.message || String(error), 'detail-card'));
      }
    }
