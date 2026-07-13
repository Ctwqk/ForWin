    const CHAPTER_PIPELINE_STAGES = [
      ['assembling_context', '组装'],
      ['writing_chapter', '写作'],
      ['continuity_review', 'Candidate Draft Review'],
      ['repairing_chapter', '修复重写'],
      ['repair_review', '修复复审'],
      ['applying_canon', 'Canon'],
      ['running_post_acceptance', '后置'],
      ['paused_for_review', '人工检查'],
      ['chapter_failed', '失败'],
    ];
    const CHAPTER_RUNTIME_STAGES = new Set(CHAPTER_PIPELINE_STAGES.map(([stage]) => stage));
    const CHAPTER_TIMELINE_RENDER_BATCH_SIZE = 60;

    function taskHistory(item) {
      return Array.isArray(item.stage_history) ? item.stage_history.filter((entry) => entry && entry.stage) : [];
    }

    function latestHistoryEntry(history, stage) {
      for (let index = history.length - 1; index >= 0; index -= 1) {
        if (history[index]?.stage === stage) return history[index];
      }
      return null;
    }

    function latestChapterStageEntry(history, chapterNumber, stage) {
      for (let index = history.length - 1; index >= 0; index -= 1) {
        const entry = history[index];
        if (Number(entry?.chapter || 0) === Number(chapterNumber) && entry?.stage === stage) return entry;
      }
      return null;
    }

    function taskIntValues(...groups) {
      return groups.flatMap((values) => (Array.isArray(values) ? values : []))
        .map((value) => Number(value || 0))
        .filter(Boolean);
    }

    function taskIntSet(...groups) {
      return new Set(taskIntValues(...groups));
    }

    function acceptedChapterSet(item) {
      const control = item?.generation_control || {};
      return taskIntSet(control.accepted_chapters, item?.completed_chapters);
    }

    function pendingReviewChapterSet(item) {
      const control = item?.generation_control || {};
      return taskIntSet(control.pending_review_chapters, item?.paused_chapters);
    }

    function failedChapterSet(item) {
      const control = item?.generation_control || {};
      return taskIntSet(control.failed_chapters, item?.failed_chapters);
    }

    function generatedChapterSet(item) {
      const control = item?.generation_control || {};
      return taskIntSet(
        control.generated_chapters,
        control.accepted_chapters,
        control.drafted_chapters,
        control.pending_review_chapters,
        item?.completed_chapters,
        item?.paused_chapters,
      );
    }

    function chapterStatusFromTask(item, chapter) {
      const number = Number(chapter?.chapter_number || 0);
      if (chapter?.status) return chapter.status;
      if (pendingReviewChapterSet(item).has(number)) return 'needs_review';
      if (failedChapterSet(item).has(number)) return 'failed';
      if (acceptedChapterSet(item).has(number)) return 'accepted';
      if (Number(item.current_chapter || 0) === number && CHAPTER_RUNTIME_STAGES.has(item.current_stage)) return 'running';
      return number ? 'planned' : '';
    }

    function formatStageNote(entry, fallback = '') {
      if (!entry) return fallback;
      const notes = [];
      if (entry.at) notes.push(entry.at);
      if (entry.chapter) notes.push(`章 ${entry.chapter}`);
      return notes.join(' | ') || fallback;
    }

    function stageFailureInspectable(item, chapter, stage, state, entry, status) {
      if (state === 'failed' || state === 'paused') return true;
      if (entry?.message && ['chapter_failed', 'paused_for_review', 'scenario_rehearsal_blocked', 'failed'].includes(stage)) return true;
      if (stage === 'chapter_failed' && Array.isArray(item.failed_chapters) && item.failed_chapters.includes(Number(chapter?.chapter_number || 0))) return true;
      if (status === 'failed' && stage === 'chapter_failed') return true;
      if (status === 'needs_review' && stage === 'paused_for_review') return true;
      return false;
    }

    async function showStageFailureDetail(item, chapter, stage, entry, state, status) {
      const chapterNumber = Number(chapter?.chapter_number || entry?.chapter || 0);
      const lines = [
        `步骤：${stageLabel(stage)}`,
        chapterNumber ? `章节：第${chapterNumber}章${chapter?.title ? `《${chapter.title}》` : ''}` : '',
        `步骤状态：${state || '-'}`,
        status ? `章节状态：${chapterStatusLabel(status)}` : '',
        entry?.at ? `到达时间：${entry.at}` : '',
        entry?.message ? `阶段消息：${entry.message}` : '',
        item.message ? `任务消息：${item.message}` : '',
        item.error ? `任务错误：${item.error}` : '',
        Array.isArray(item.failed_chapters) && item.failed_chapters.length ? `失败章节：${item.failed_chapters.join(', ')}` : '',
        Array.isArray(item.paused_chapters) && item.paused_chapters.length ? `待 review 章节：${item.paused_chapters.join(', ')}` : '',
        Array.isArray(item.frozen_artifacts) && item.frozen_artifacts.length ? `冻结产物：${item.frozen_artifacts.join('\\n')}` : '',
      ].filter(Boolean);

      if (item.project_id && chapterNumber && (status === 'needs_review' || chapter?.has_review)) {
        try {
          const review = await requestJson(`/api/projects/${item.project_id}/chapters/${chapterNumber}/review`);
          setGlobalStatus(lines.join(' · '), '章节阶段详情');
          await openReviewModal(item.project_id, chapterNumber, review);
          return;
        } catch (error) {
          lines.push(`Review 读取失败：${error.message || String(error)}`);
        }
      }

      window.alert(lines.join('\\n') || '这个步骤没有记录到失败原因。');
    }

    function renderMacroProgress(item) {
      const history = taskHistory(item);
      const accepted = acceptedChapterSet(item).size;
      const generated = generatedChapterSet(item).size;
      const failed = failedChapterSet(item).size;
      const paused = pendingReviewChapterSet(item).size;
      const requested = Number(item.requested_chapters || 0);
      const hasChapterWork = history.some((entry) => CHAPTER_RUNTIME_STAGES.has(entry.stage));
      const hasTerminal = ['completed', 'failed', 'partial_failed', 'needs_review', 'cancelled', 'paused'].includes(item.status);
      const scenarioEntry = latestHistoryEntry(history, 'running_scenario_rehearsal');
      const scenarioPatch = latestHistoryEntry(history, 'scenario_rehearsal_patch_required');
      const scenarioBlocked = latestHistoryEntry(history, 'scenario_rehearsal_blocked');
      const currentIsChapterWork = CHAPTER_RUNTIME_STAGES.has(item.current_stage);
      const nodes = [
        {
          key: 'queued',
          label: '排队',
          state: latestHistoryEntry(history, 'queued') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'queued'), '等待开始'),
        },
        {
          key: 'planning_arc',
          label: '大纲',
          state: latestHistoryEntry(history, 'planning_arc') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'planning_arc'), '未记录'),
        },
        {
          key: 'resolving_arc_envelope',
          label: 'Arc',
          state: latestHistoryEntry(history, 'resolving_arc_envelope') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'resolving_arc_envelope'), '未解析'),
        },
        {
          key: 'scenario_rehearsal',
          label: 'Scenario Rehearsal',
          state: scenarioBlocked ? 'failed' : (scenarioPatch ? 'paused' : (scenarioEntry ? 'completed' : 'upcoming')),
          note: scenarioBlocked
            ? formatStageNote(scenarioBlocked, '推演阻断')
            : (scenarioPatch
              ? formatStageNote(scenarioPatch, '等待 patch approve / rerun')
              : formatStageNote(scenarioEntry, '低风险跳过或已通过')),
        },
        {
          key: 'chapter_loop',
          label: '逐章生成',
          state: currentIsChapterWork ? 'current' : (hasChapterWork ? 'completed' : 'upcoming'),
          note: `${generated}/${requested || '-'} 已生成 · ${accepted} accepted · ${failed} 失败 · ${paused} 待 review`,
        },
        {
          key: 'terminal',
          label: '结果',
          state: item.status === 'failed' || item.status === 'partial_failed' ? 'failed'
            : (['needs_review', 'paused'].includes(item.status) ? 'paused'
              : (hasTerminal ? 'completed' : 'upcoming')),
          note: stageLabel(item.current_stage || item.status),
        },
      ];

      const wrap = createNode('div', '', 'task-map');
      const head = createNode('div', '', 'task-map-head');
      const title = createNode('div', '', 'task-map-title');
      title.appendChild(createNode('strong', '任务主线'));
      title.appendChild(createNode('span', '一次性 gate · 逐章循环见下方章节流水线。'));
      head.appendChild(title);
      head.appendChild(createNode('span', item.status || '', `badge ${badgeKindByStatus(item.status)}`));
      wrap.appendChild(head);

      const flow = createNode('div', '', 'macro-flow');
      nodes.forEach((nodeInfo) => {
        const node = createNode('div', '', `macro-node ${nodeInfo.state}`);
        node.appendChild(createNode('div', nodeInfo.label, 'stage-name'));
        node.appendChild(createNode('div', nodeInfo.note, 'stage-note'));
        flow.appendChild(node);
      });
      wrap.appendChild(flow);
      return wrap;
    }

    function chapterNumbersForTimeline(item, chapters) {
      const numbers = new Set();
      const requested = Number(item.requested_chapters || 0);
      for (let number = 1; number <= requested; number += 1) numbers.add(number);
      taskHistory(item).forEach((entry) => {
        const chapter = Number(entry.chapter || 0);
        if (chapter) numbers.add(chapter);
      });
      (Array.isArray(chapters) ? chapters : []).forEach((chapter) => {
        const number = Number(chapter.chapter_number || 0);
        if (number) numbers.add(number);
      });
      return Array.from(numbers).sort((a, b) => a - b);
    }

    function chapterLineState(item, chapter, status) {
      const number = Number(chapter?.chapter_number || 0);
      if (status === 'failed') return 'failed';
      if (status === 'accepted' || status === 'completed') return 'accepted';
      if (status === 'needs_review') return 'current';
      if (Number(item.current_chapter || 0) === number && CHAPTER_RUNTIME_STAGES.has(item.current_stage)) return 'current';
      return '';
    }

    function chapterStepState(item, history, chapter, stage, status) {
      const number = Number(chapter.chapter_number || 0);
      const entry = latestChapterStageEntry(history, number, stage);
      if (Number(item.current_chapter || 0) === number && item.current_stage === stage) {
        return stage === 'paused_for_review' ? 'paused' : 'current';
      }
      if (stage === 'chapter_failed' && status === 'failed') return 'failed';
      if (stage === 'paused_for_review' && status === 'needs_review') return 'paused';
      if (stage === 'paused_for_review' && ['accepted', 'completed'].includes(status)) return 'skipped';
      if (!entry) {
        if (['accepted', 'completed'].includes(status) && !['chapter_failed', 'paused_for_review'].includes(stage)) {
          return 'completed';
        }
        if (status === 'drafted' && ['assembling_context', 'writing_chapter', 'continuity_review'].includes(stage)) {
          return 'completed';
        }
        return 'upcoming';
      }
      if (stage === 'chapter_failed') return 'failed';
      if (stage === 'paused_for_review') return 'paused';
      return 'completed';
    }

    function renderChapterTimeline(item, chapters = []) {
      const history = taskHistory(item);
      const chapterByNumber = new Map((Array.isArray(chapters) ? chapters : []).map((chapter) => [Number(chapter.chapter_number || 0), chapter]));
      const numbers = chapterNumbersForTimeline(item, chapters);
      const section = createNode('section', '', 'detail-card');
      const head = createNode('div', '', 'task-map-head');
      const title = createNode('div', '', 'task-map-title');
      title.appendChild(createNode('strong', '章节流水线'));
      title.appendChild(createNode('span', '每一行是一章；横向从组装、写作、审查到写入 Canon，不再混用不同章节的 stage。'));
      head.appendChild(title);
      head.appendChild(createNode('span', `${numbers.length || 0} 章`, 'badge'));
      section.appendChild(head);

      const list = createNode('div', '', 'chapter-timeline');
      const pagingStatus = createNode('div', '', 'meta-line');
      let renderedNumberCount = Math.min(CHAPTER_TIMELINE_RENDER_BATCH_SIZE, numbers.length);
      const loadMoreButton = createButton('加载更多章节', () => {
        renderedNumberCount = Math.min(
          renderedNumberCount + CHAPTER_TIMELINE_RENDER_BATCH_SIZE,
          numbers.length,
        );
        renderVisibleTimelineRows();
      }, 'ghost');
      const renderTimelineRow = (number) => {
        const chapter = chapterByNumber.get(number) || { chapter_number: number };
        const status = chapterStatusFromTask(item, chapter);
        const badgeStatus = status || 'planned';
        const row = createNode('div', '', `chapter-line ${chapterLineState(item, chapter, status)}`);
        const rowHead = createNode('div', '', 'chapter-line-head');
        rowHead.appendChild(createNode('strong', `第${number}章`));
        rowHead.appendChild(createNode('span', chapterStatusLabel(badgeStatus), `badge ${badgeKindByStatus(status)}`));
        const details = [
          chapter.title ? `《${chapter.title}》` : '',
          chapter.char_count ? `${chapter.char_count} 字` : '',
          chapter.has_draft ? '有正文' : '',
          chapter.has_review ? '有审查' : '',
        ].filter(Boolean).join(' · ');
        rowHead.appendChild(createNode('div', details || '尚无产物', 'stage-note'));
        if (item.project_id) {
          const chapterActions = createNode('div', '', 'action-row');
          chapterActions.appendChild(createButton('开始前 checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
            boundary_kind: 'chapter_start',
            boundary_chapter: number,
          }), 'ghost'));
          chapterActions.appendChild(createButton('accepted 后 checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
            boundary_kind: 'chapter_accepted',
            boundary_chapter: number,
          }), 'ghost'));
          rowHead.appendChild(chapterActions);
        }
        row.appendChild(rowHead);

        const steps = createNode('div', '', 'chapter-steps');
        CHAPTER_PIPELINE_STAGES.forEach(([stage, label]) => {
          const state = chapterStepState(item, history, chapter, stage, status);
          const step = createNode('div', '', `chapter-step ${state}`);
          step.appendChild(createNode('strong', label));
          const entry = latestChapterStageEntry(history, number, stage);
          let note = entry?.at || '';
          if (!note) {
            if (state === 'upcoming') note = '未到达';
            else if (state === 'failed') note = '失败';
            else if (state === 'paused') note = '待处理';
            else if (state === 'skipped') note = '未触发';
            else if (state === 'current') note = '进行中';
            else note = '已完成';
          }
          step.appendChild(createNode('span', note));
          if (stageFailureInspectable(item, chapter, stage, state, entry, status)) {
            step.classList.add('inspectable');
            step.title = '点击查看失败 / 暂停原因';
            step.setAttribute('role', 'button');
            step.tabIndex = 0;
            const inspect = () => showStageFailureDetail(item, chapter, stage, entry, state, status);
            step.addEventListener('click', inspect);
            step.addEventListener('keydown', (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                inspect();
              }
            });
          }
          steps.appendChild(step);
        });
        row.appendChild(steps);
        list.appendChild(row);
      };
      const renderVisibleTimelineRows = () => {
        clearNode(list);
        if (!numbers.length) {
          list.appendChild(createNode('div', '暂无章节进度。', 'empty'));
          return;
        }
        numbers.slice(0, renderedNumberCount).forEach(renderTimelineRow);
        pagingStatus.textContent = `已显示 ${renderedNumberCount} / ${numbers.length} 章`;
        loadMoreButton.style.display = renderedNumberCount < numbers.length ? '' : 'none';
      };
      renderVisibleTimelineRows();
      section.appendChild(list);
      if (numbers.length > CHAPTER_TIMELINE_RENDER_BATCH_SIZE) {
        section.appendChild(pagingStatus);
        const pagingActions = createNode('div', '', 'action-row');
        pagingActions.appendChild(loadMoreButton);
        section.appendChild(pagingActions);
      }
      return section;
    }

    async function loadProjectChapterPage(projectId, offset = 0, limit = 60) {
      const query = new URLSearchParams({
        offset: String(Math.max(0, Number(offset || 0))),
        limit: String(Math.max(1, Number(limit || 60))),
      });
      const payload = await requestJson(`/api/projects/${projectId}/chapters/page?${query.toString()}`);
      if (Array.isArray(payload)) {
        return {
          project_id: projectId,
          total: payload.length,
          offset: 0,
          limit: payload.length,
          has_more: false,
          chapters: payload,
        };
      }
      const chapters = Array.isArray(payload?.chapters) ? payload.chapters : [];
      return {
        project_id: payload?.project_id || projectId,
        total: Number(payload?.total || chapters.length),
        offset: Number(payload?.offset || 0),
        limit: Number(payload?.limit || limit || 60),
        has_more: Boolean(payload?.has_more),
        chapters,
      };
    }

    async function loadProjectChapters(projectId) {
      const page = await loadProjectChapterPage(projectId, 0, 60);
      return page.chapters;
    }

    async function loadProjectDetail(projectId) {
      return requestJson(`/api/projects/${projectId}`);
    }

    async function toggleChapterBody(projectId, chapterNumber, bodyId) {
      const body = document.getElementById(bodyId);
      if (!body) return;
      if (body.classList.contains('open')) {
        body.classList.remove('open');
        return;
      }
      if (!body.dataset.loaded) {
        try {
          const chapter = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}`);
          body.textContent = chapter.body || '';
          body.dataset.loaded = '1';
        } catch (error) {
          body.textContent = error.message || String(error);
          body.dataset.loaded = '1';
        }
      }
      body.classList.add('open');
    }

    function describeDecisionScope(item) {
      return [
        item.created_at || '',
        item.scope || '',
        item.band_id ? `band ${item.band_id}` : '',
        item.chapter_number ? `chapter ${item.chapter_number}` : '',
        item.related_object_type || '',
        item.related_object_id ? `#${item.related_object_id}` : '',
      ].filter(Boolean).join(' | ');
    }

    function latestDecisionRefId(refs) {
      const items = Array.isArray(refs) ? refs : [];
      for (let index = items.length - 1; index >= 0; index -= 1) {
        const value = String(items[index]?.id || items[index]?.decision_event_id || '').trim();
        if (value) return value;
      }
      return '';
    }

    function focusDecisionEvent(decisionEventId, preferredFilter = 'all') {
      const eventId = String(decisionEventId || '').trim();
      if (!eventId) return false;
      if (window.currentDecisionTimelineController?.setFilter) {
        window.currentDecisionTimelineController.setFilter(preferredFilter || 'all');
      }
      const row = document.getElementById(`decision_event_${eventId}`);
      if (!row) return false;
      const parent = row.parentElement;
      if (parent) {
        parent.querySelectorAll('.chapter-row.focused').forEach((node) => node.classList.remove('focused'));
      }
      row.classList.add('focused');
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => row.classList.remove('focused'), 2200);
      return true;
    }

    async function jumpToReviewDecisionChain(projectId, chapterNumber) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        const targetId = latestDecisionRefId(data.decision_refs);
        if (!targetId || !focusDecisionEvent(targetId, 'chapter')) {
          setGlobalStatus('当前 Review 还没有可跳转的决策链，或时间线里暂未返回对应事件。', '治理时间线');
          return;
        }
        setGlobalStatus(`已跳到第 ${chapterNumber} 章 Review 的决策链。`, '治理时间线');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 决策链读取失败');
      }
    }

    function jumpToCheckpointDecisionChain(checkpoint = null) {
      const targetId = latestDecisionRefId(checkpoint?.decision_refs);
      if (!targetId || !focusDecisionEvent(targetId, 'band')) {
        setGlobalStatus('当前 checkpoint 还没有可跳转的决策链。', '治理时间线');
        return;
      }
      setGlobalStatus(`已跳到 band ${checkpoint?.band_id || '-'} checkpoint 的决策链。`, '治理时间线');
    }

    const REVIEW_LAYER_ORDER = [
      'draft_review',
      'repair',
      'residual_eligibility',
      'gate_delegation',
      'canon',
    ];
    const REVIEW_LAYER_LABELS = {
      draft_review: '草稿评审',
      repair: '修复',
      residual_eligibility: '残余资格',
      gate_delegation: '门禁委托',
      canon: 'Canon',
    };
    let currentReviewModal = null;

    function reviewLayerBadgeClass(layer) {
      if (layer?.blocking) return 'badge danger';
      if (['complete', 'not_required'].includes(String(layer?.status || ''))) return 'badge ok';
      return 'badge warn';
    }

    function appendReviewDetailRow(container, label, badges, copy, evidenceRefs = []) {
      const row = createNode('div', '', 'review-detail-row');
      const labelNode = createNode('div', '', 'review-detail-label');
      labelNode.appendChild(createNode('strong', label || '未命名'));
      (Array.isArray(badges) ? badges : []).filter(Boolean).forEach((badge) => {
        labelNode.appendChild(createNode('span', badge, 'badge'));
      });
      const copyNode = createNode('div', copy || '无补充信息。', 'review-detail-copy');
      const refs = (Array.isArray(evidenceRefs) ? evidenceRefs : []).filter(Boolean);
      if (refs.length) {
        copyNode.appendChild(createNode('div', `证据：${refs.join(' · ')}`, 'meta-line'));
      }
      row.append(labelNode, copyNode);
      container.appendChild(row);
    }

    function renderReviewLayer(layer) {
      const panel = createNode('article', '', 'review-layer-panel');
      panel.dataset.blocking = String(Boolean(layer.blocking));
      const title = createNode('div', '', 'review-layer-title');
      title.appendChild(createNode('strong', REVIEW_LAYER_LABELS[layer.layer] || layer.layer || '决策层'));
      title.appendChild(createNode('span', layer.status || 'unknown', reviewLayerBadgeClass(layer)));
      panel.appendChild(title);
      panel.appendChild(createNode('div', layer.outcome || '-', 'review-layer-outcome'));
      panel.appendChild(createNode('div', layer.summary || '尚无摘要。', 'review-layer-summary'));

      const footer = createNode('div', '', 'review-layer-footer');
      const evidence = createNode('div', '', 'review-evidence-list');
      const evidenceRefs = Array.isArray(layer.evidence_refs) ? layer.evidence_refs : [];
      if (evidenceRefs.length) {
        evidenceRefs.forEach((ref) => evidence.appendChild(createNode('div', ref, 'review-evidence-item')));
      } else {
        evidence.appendChild(createNode('div', '无独立证据引用', 'meta-line'));
      }
      footer.appendChild(evidence);
      const auditButton = createButton('审计', () => jumpToReviewLayerDecisionChain(layer), 'ghost review-layer-audit');
      auditButton.disabled = !latestDecisionRefId(layer.decision_refs);
      footer.appendChild(auditButton);
      panel.appendChild(footer);
      return panel;
    }

    function renderReviewModal(data, projectId, chapterNumber) {
      currentReviewModal = { projectId, chapterNumber, data };
      document.getElementById('review_modal_kicker').textContent = `Chapter ${chapterNumber} Review`;
      document.getElementById('review_modal_title').textContent = data.title || `第${chapterNumber}章`;
      document.getElementById('review_modal_meta').textContent = [
        data.status ? `章节状态 ${data.status}` : '',
        data.verdict ? `草稿 verdict ${data.verdict}` : '',
        data.acceptance_mode ? `接受模式 ${data.acceptance_mode}` : '',
        data.canon_risk_level ? `Canon 风险 ${data.canon_risk_level}` : '',
      ].filter(Boolean).join(' · ');

      const layerFlow = document.getElementById('review_layer_flow');
      clearNode(layerFlow);
      const sourceLayers = Array.isArray(data.decision_layers) ? data.decision_layers : [];
      const layerMap = new Map(sourceLayers.map((layer) => [layer.layer, layer]));
      REVIEW_LAYER_ORDER.forEach((key) => {
        layerFlow.appendChild(renderReviewLayer(layerMap.get(key) || {
          layer: key,
          status: 'missing',
          outcome: 'not_reported',
          summary: '后端未返回该决策层。',
          blocking: true,
          evidence_refs: [],
          decision_refs: [],
        }));
      });

      const issues = [
        ...(Array.isArray(data.issues) ? data.issues.map((issue) => ({ ...issue, source: '草稿' })) : []),
        ...(Array.isArray(data.residual_review_issues)
          ? data.residual_review_issues.map((issue) => ({ ...issue, source: '残余' }))
          : []),
      ];
      document.getElementById('review_issue_count').textContent = `${issues.length} 项`;
      const issueList = document.getElementById('review_issue_list');
      clearNode(issueList);
      if (!issues.length) {
        issueList.appendChild(createNode('div', '没有记录问题。', 'empty'));
      } else {
        issues.forEach((issue, index) => appendReviewDetailRow(
          issueList,
          `${index + 1}. ${issue.rule_name || issue.issue_type || '未命名问题'}`,
          [issue.source, issue.severity, issue.issue_group, issue.issue_type],
          [issue.description, issue.suggested_fix ? `建议：${issue.suggested_fix}` : ''].filter(Boolean).join(' / '),
          issue.evidence_refs,
        ));
      }

      const attempts = Array.isArray(data.rewrite_attempts) ? data.rewrite_attempts : [];
      document.getElementById('review_repair_count').textContent = `${attempts.length} 次`;
      const repairList = document.getElementById('review_repair_list');
      clearNode(repairList);
      if (!attempts.length) {
        repairList.appendChild(createNode('div', '没有修复尝试。', 'empty'));
      } else {
        attempts.forEach((attempt) => {
          const verification = attempt.verification || {};
          appendReviewDetailRow(
            repairList,
            `Attempt ${attempt.attempt_no || '-'}`,
            [attempt.repair_phase, attempt.repair_scope, attempt.result_verdict || '无 verdict'],
            [
              attempt.failure_reason || '',
              Object.keys(verification).length
                ? `验证：must-fix ${verification.fixed_all_must_fix ? '通过' : '未通过'}；must-preserve ${verification.preserved_all_must_preserve ? '通过' : '未通过'}`
                : '未记录独立验证。',
            ].filter(Boolean).join(' / '),
            [attempt.result_review_id ? `review:${attempt.result_review_id}` : ''].filter(Boolean),
          );
        });
      }
      if (data.rule_decision?.rule_id) {
        appendReviewDetailRow(
          repairList,
          `资格规则 ${data.rule_decision.rule_id}`,
          [data.rule_decision.outcome || 'unknown'],
          data.rule_decision.reason || '无规则说明。',
          data.rule_decision.missing_evidence || [],
        );
      }

      const canonLayer = layerMap.get('canon');
      const committed = canonLayer?.outcome === 'committed';
      document.getElementById('review_modal_retry').disabled = committed;
      document.getElementById('review_modal_approve').disabled = committed;
      document.getElementById('review_modal_continue').disabled = committed;
      document.getElementById('review_modal_audit').disabled = !latestDecisionRefId(data.decision_refs);
    }

    async function openReviewModal(projectId, chapterNumber, preloadedData = null) {
      const shell = document.getElementById('review_modal_shell');
      shell.classList.add('open');
      document.getElementById('review_modal_title').textContent = `第${chapterNumber}章 · 正在读取`;
      try {
        const data = preloadedData || await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        renderReviewModal(data, projectId, chapterNumber);
      } catch (error) {
        closeReviewModal();
        setGlobalStatus(error.message || String(error), 'Review 读取失败');
      }
    }

    function closeReviewModal() {
      document.getElementById('review_modal_shell').classList.remove('open');
      currentReviewModal = null;
    }

    function jumpToReviewLayerDecisionChain(layer) {
      const targetId = latestDecisionRefId(layer?.decision_refs);
      if (!targetId) return;
      closeReviewModal();
      if (!focusDecisionEvent(targetId, 'chapter')) {
        setGlobalStatus('时间线里暂未返回该层对应的审计事件。', 'Review 审计');
      }
    }

    function jumpToReviewModalDecisionChain() {
      const targetId = latestDecisionRefId(currentReviewModal?.data?.decision_refs);
      if (!targetId) return;
      closeReviewModal();
      if (!focusDecisionEvent(targetId, 'chapter')) {
        setGlobalStatus('时间线里暂未返回 Review 对应的审计事件。', 'Review 审计');
      }
    }

    function approveReviewFromModal(continueGeneration = false) {
      const context = currentReviewModal;
      if (!context) return;
      closeReviewModal();
      approveReview(context.projectId, context.chapterNumber, continueGeneration);
    }

    function retryReviewFromModal() {
      const context = currentReviewModal;
      if (!context) return;
      closeReviewModal();
      retryReview(context.projectId, context.chapterNumber, false);
    }

    async function showReview(projectId, chapterNumber) {
      await openReviewModal(projectId, chapterNumber);
    }

    async function executeApproveReview(projectId, chapterNumber, continueGeneration = false, reason = '') {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            continue_generation: Boolean(continueGeneration),
            reason: String(reason || '').trim(),
          }),
        });
        setGlobalStatus(data.message || `第${chapterNumber}章已接受。`, 'Review 处理');
        await loadTaskCenter();
        await loadBooks();
        if (data.task_id) {
          await openTaskDrawer('generation', data.task_id);
        } else if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 处理失败');
      }
    }

    function approveReview(projectId, chapterNumber, continueGeneration = false) {
      openProjectControlActionModal({
        title: continueGeneration ? `接受并继续 · 第${chapterNumber}章` : `接受 Review · 第${chapterNumber}章`,
        description: continueGeneration
          ? '本次会先接受当前 review，再尝试继续生成；如果仍命中治理 gate，会保留阻断。'
          : '接受当前 chapter review，并把理由写入决策时间线。',
        confirmLabel: continueGeneration ? '接受并继续' : '接受 Review',
        errorTitle: 'Review 处理失败',
        onSubmit: ({ reason }) => executeApproveReview(projectId, chapterNumber, continueGeneration, reason),
      });
    }

    async function executeRetryReview(projectId, chapterNumber, continueGeneration = false, reason = '', allowAccepted = false) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review/retry`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            continue_generation: Boolean(continueGeneration),
            reason: String(reason || '').trim(),
            allow_accepted: Boolean(allowAccepted),
          }),
        });
        setGlobalStatus(data.message || `第${chapterNumber}章已重置为 planned。`, 'Review 重试');
        await loadTaskCenter();
        await loadBooks();
        if (data.task_id) {
          await openTaskDrawer('generation', data.task_id);
        } else if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 重试失败');
      }
    }

    function retryReview(projectId, chapterNumber, continueGeneration = false, allowAccepted = false) {
      openProjectControlActionModal({
        title: continueGeneration ? `Retry 并继续 · 第${chapterNumber}章` : `Retry Review · 第${chapterNumber}章`,
        description: '把当前章节重置为 planned 并记录 retry reason。后续生成只会选择 planned / failed 章节。',
        confirmLabel: continueGeneration ? 'Retry 并继续' : 'Retry Review',
        errorTitle: 'Review 重试失败',
        onSubmit: ({ reason }) => executeRetryReview(projectId, chapterNumber, continueGeneration, reason, allowAccepted),
      });
    }

    async function createOperatorProposal(projectId, payload) {
      const created = await requestJson(`/api/projects/${projectId}/proposals`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setGlobalStatus(`已创建 proposal：${created.proposal_type || created.id || 'operator action'}`, 'Operator Action');
      await loadBooks();
      if (currentDrawerTask?.project_id === projectId) {
        await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
      }
      return created;
    }

    function registerSubworldEntityFromReview(projectId, chapterNumber) {
      openProjectControlActionModal({
        title: `Register Subworld Entity · 第${chapterNumber}章`,
        description: '创建一个可审计 proposal，用于把 review 中的未准入实体登记到 subworld/canon 入口。',
        confirmLabel: 'Register Entity Proposal',
        fields: [
          { name: 'entity_name', label: 'Entity Name' },
          { name: 'subworld_id', label: 'Subworld ID' },
          { name: 'role_hint', label: 'Role Hint' },
        ],
        errorTitle: '创建实体 proposal 失败',
        onSubmit: ({ reason, entity_name, subworld_id, role_hint }) => createOperatorProposal(projectId, {
          source: 'operator_home',
          proposal_type: 'SubworldEntityRegistrationProposal',
          reason,
          human_notes: `chapter=${chapterNumber}`,
          created_by: 'operator_home',
          proposed_patch: {
            action: 'register_entity',
            chapter_number: chapterNumber,
            entity_name,
            subworld_id,
            role_hint,
          },
        }),
      });
    }

    function recordBackgroundEntityDecisionFromReview(projectId, chapterNumber) {
      openProjectControlActionModal({
        title: `Record Background Entity · 第${chapterNumber}章`,
        description: '创建一个可审计 proposal，用于记录未计划实体不进入 canon 的背景决策。',
        confirmLabel: 'Record Decision',
        fields: [
          { name: 'source_reference', label: 'Source Reference' },
        ],
        errorTitle: '创建 background entity decision 失败',
        onSubmit: ({ reason, source_reference }) => createOperatorProposal(projectId, {
          source: 'operator_home',
          proposal_type: 'EntityBackgroundDecisionProposal',
          reason,
          human_notes: `chapter=${chapterNumber}`,
          created_by: 'operator_home',
          proposed_patch: {
            action: 'record_background_generic_decision',
            chapter_number: chapterNumber,
            source_reference,
          },
        }),
      });
    }

    function createObligationFromReview(projectId, chapterNumber) {
      openProjectControlActionModal({
        title: `Create Obligation · 第${chapterNumber}章`,
        description: '创建一个叙事义务 proposal，供后续计划/BookState 处理。',
        confirmLabel: 'Create Obligation Proposal',
        fields: [
          { name: 'summary', label: 'Summary', type: 'textarea', rows: 4 },
          { name: 'priority', label: 'Priority', type: 'select', options: ['P0', 'P1', 'P2'], value: 'P1' },
          { name: 'deadline_chapter', label: 'Deadline Chapter', type: 'number', min: chapterNumber, value: chapterNumber + 2 },
          { name: 'payoff_test', label: 'Payoff Test', type: 'textarea', rows: 4 },
        ],
        errorTitle: '创建 obligation proposal 失败',
        onSubmit: ({ reason, summary, priority, deadline_chapter, payoff_test }) => createOperatorProposal(projectId, {
          source: 'operator_home',
          proposal_type: 'NarrativeObligationProposal',
          reason,
          human_notes: `chapter=${chapterNumber}`,
          created_by: 'operator_home',
          proposed_patch: {
            action: 'create_obligation',
            origin_chapter_number: chapterNumber,
            summary,
            priority,
            deadline_chapter,
            payoff_test,
          },
        }),
      });
    }

    function uniqueChapterNumbers(...groups) {
      const numbers = new Set();
      groups.forEach((values) => {
        (Array.isArray(values) ? values : []).forEach((value) => {
          const number = Number(value?.chapter_number || value || 0);
          if (number) numbers.add(number);
        });
      });
      return Array.from(numbers).sort((left, right) => left - right);
    }

    function chapterLookup(chapters = []) {
      return new Map((Array.isArray(chapters) ? chapters : []).map((chapter) => [
        Number(chapter.chapter_number || 0),
        chapter,
      ]));
    }

    function chaptersForNumbers(chapters, numbers) {
      const lookup = chapterLookup(chapters);
      return uniqueChapterNumbers(numbers).map((number) => lookup.get(number) || { chapter_number: number });
    }

    function actionableReviewChapters(item, control, chapters = []) {
      const pendingFromControl = Array.isArray(control.pending_review_chapters) ? control.pending_review_chapters : [];
      const pausedFromTask = Array.isArray(item.paused_chapters) ? item.paused_chapters : [];
      const pendingFromChapters = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => chapter.status === 'needs_review')
        .map((chapter) => chapter.chapter_number);
      return chaptersForNumbers(chapters, uniqueChapterNumbers(pendingFromControl, pausedFromTask, pendingFromChapters));
    }

    function actionableFailedChapters(item, control, chapters = []) {
      const failedFromControl = Array.isArray(control.failed_chapters) ? control.failed_chapters : [];
      const failedFromTask = Array.isArray(item.failed_chapters) ? item.failed_chapters : [];
      const failedFromChapters = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => chapter.status === 'failed')
        .map((chapter) => chapter.chapter_number);
      return chaptersForNumbers(chapters, uniqueChapterNumbers(failedFromControl, failedFromTask, failedFromChapters));
    }

    function firstPlannedOrFailedChapter(control, chapters = []) {
      const next = Number(control.next_chapter || 0);
      if (next) return next;
      const candidates = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => ['planned', 'failed'].includes(chapter.status))
        .map((chapter) => Number(chapter.chapter_number || 0))
        .filter(Boolean)
        .sort((left, right) => left - right);
      return candidates[0] || 0;
    }

    function projectAsBook(item, project, chapters) {
      return {
        id: item.project_id,
        title: project?.title || item.title || '',
        chapters: Array.isArray(chapters) ? chapters : [],
        automation: project?.automation || {},
      };
    }

    function generationGuidance(item, project, chapters = []) {
      const control = item.generation_control || {};
      const reviewChapters = actionableReviewChapters(item, control, chapters);
      const failedChapters = actionableFailedChapters(item, control, chapters);
      const accepted = acceptedChapterSet(item).size;
      const generated = generatedChapterSet(item).size;
      const requested = Number(item.requested_chapters || project?.target_total_chapters || 0);
      const nextChapter = firstPlannedOrFailedChapter(control, chapters);
      const currentStage = stageLabel(item.current_stage || item.status);
      const currentChapter = Number(item.current_chapter || 0);
      const isActive = ACTIVE_TASK_STATUSES.has(item.status);

      if (item.pause_requested) {
        return {
          tone: 'blocked',
          eyebrow: '暂停请求已发出',
          title: '等待安全 checkpoint',
          description: '系统不会中断正在进行的 LLM 请求。当前请求或当前小阶段结束后，任务会保存进度并进入 paused。',
          next: '等待暂停落点',
          safety: '不要重启容器；等待任务自己进入 paused。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (reviewChapters.length || item.status === 'needs_review') {
        const first = reviewChapters[0];
        const chapterLabel = first?.chapter_number ? `第 ${first.chapter_number} 章` : '待 review 章节';
        return {
          tone: 'blocked',
          eyebrow: '人工检查阻塞',
          title: `${chapterLabel} 需要处理`,
          description: '状态机要求先处理 needs_review。继续生成会被拒绝，直到人工查看 review、接受或修复该章。',
          next: '查看 Review 并决定是否接受',
          safety: '接受并继续会新建 continue task，不会重写已 accepted 章节。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (item.status === 'paused') {
        return {
          tone: 'blocked',
          eyebrow: '已安全暂停',
          title: '进度已保存，可以继续',
          description: '任务停在安全边界。继续生成会从 planned / failed 章节恢复，不会重写已写入 Canon 的章节。',
          next: control.can_resume ? '继续生成剩余章节' : '等待可继续章节',
          safety: '如果还有 needs_review，系统会先要求处理 review。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (['failed', 'partial_failed'].includes(item.status) || failedChapters.length) {
        return {
          tone: 'failed',
          eyebrow: item.status === 'partial_failed' ? '部分失败' : '生成失败',
          title: failedChapters.length ? `第 ${failedChapters[0].chapter_number} 章开始需要处理` : '生成任务失败',
          description: control.can_resume
            ? '失败章节可通过继续生成重试。先点失败节点查看原因，再决定是否直接继续。'
            : '当前没有可继续章节。先查看失败原因，确认是模型/API 问题还是流程问题。',
          next: control.can_resume ? '查看失败原因或重试剩余章节' : '查看失败原因',
          safety: '继续生成只会选择 failed / planned 章节。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (isActive) {
        return {
          tone: '',
          eyebrow: '正在执行',
          title: currentChapter ? `第 ${currentChapter} 章 · ${currentStage}` : currentStage,
          description: '任务正在按状态机推进。安全暂停只会请求在 checkpoint 停住；强制终止用于必须中断的场景。',
          next: currentChapter ? `完成第 ${currentChapter} 章当前阶段` : '等待当前阶段完成',
          safety: '运行中不要重启容器，除非接受当前任务中断。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (item.status === 'completed') {
        return {
          tone: '',
          eyebrow: '本轮完成',
          title: '章节已写入 Canon',
          description: '本轮选择的章节已经完成写作、review、canon 和后置处理。可以查看正文，或把最近章节发布到平台。',
          next: '查看正文或发布章节',
          safety: '后续继续生成会从下一批 planned 章节开始。',
          reviewChapters,
          failedChapters,
          completed: accepted,
          accepted,
          generated,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      return {
        tone: control.can_resume ? 'blocked' : '',
        eyebrow: control.can_resume ? '可继续' : '书本状态',
        title: control.can_resume ? '还有章节未完成' : '没有活跃生成任务',
        description: control.can_resume
          ? '当前书本还有 planned / failed 章节，可以继续生成。'
          : '当前没有运行中的生成任务。可以从书本页新建生成，或查看已有章节与自动化配置。',
        next: control.can_resume ? '继续生成剩余章节' : '按需要创建新任务',
        safety: '书本入口不会自动重写已 accepted 章节。',
        reviewChapters,
        failedChapters,
        completed: accepted,
        accepted,
        generated,
        requested,
        nextChapter,
        currentStage,
        currentChapter,
        isActive,
      };
    }

    function appendControlSideRow(parent, label, value) {
      const row = createNode('div', '', 'control-side-row');
      row.appendChild(createNode('span', label));
      row.appendChild(createNode('strong', String(value || '-')));
      parent.appendChild(row);
    }

    function addReasonCount(counts, reason) {
      const key = String(reason || '').trim();
      if (!key) return;
      counts[key] = (counts[key] || 0) + 1;
    }

    function stopReasonDistribution(item, project, guidance) {
      const control = item.generation_control || {};
      const counts = {};
      addReasonCount(counts, control.blocking_reason?.code);
      addReasonCount(counts, project?.blocking_reason?.code);
      if (guidance.reviewChapters.length) addReasonCount(counts, 'pending_review_blocker');
      if (guidance.failedChapters.length) addReasonCount(counts, 'failed_chapters_blocker');
      if (item.status === 'needs_review') addReasonCount(counts, 'needs_review_status');
      if (item.status === 'failed' || item.status === 'partial_failed') addReasonCount(counts, item.status);
      if (item.pause_requested) addReasonCount(counts, 'pause_requested');
      if (!Object.keys(counts).length) addReasonCount(counts, 'none');
      return counts;
    }

    function autoContinueChainHealth(item, guidance) {
      const control = item.generation_control || {};
      if (guidance.isActive) return 'running';
      if (guidance.reviewChapters.length) return 'stopped: needs_review';
      if (guidance.failedChapters.length) return control.can_resume ? 'recoverable: failed chapters' : 'blocked: failed chapters';
      if (control.can_resume) return 'ready_to_continue';
      if (item.status === 'completed') return 'complete';
      return item.status || 'idle';
    }

    function renderOperatorQueueHealth(item, project, guidance) {
      const box = createNode('div', '', 'operator-queue-health');
      const stopReasons = stopReasonDistribution(item, project, guidance);
      const reasonText = Object.entries(stopReasons)
        .map(([reason, count]) => `${reason}(${count})`)
        .join(' / ');
      const repairExhaustedCount = guidance.reviewChapters.filter((chapter) => (
        Number(chapter.repair_attempt_count || 0) > 0
        || String(chapter.acceptance_mode || '').includes('force_accept')
        || String(chapter.canon_risk_level || '').trim()
      )).length;
      box.appendChild(createNode('div', `Stop Reason Distribution：${reasonText}`, 'meta-line'));
      box.appendChild(createNode('div', `Auto-continue Chain Health：${autoContinueChainHealth(item, guidance)}`, 'meta-line'));
      box.appendChild(createNode('div', `needs-review queue：${guidance.reviewChapters.length} · repair-exhausted queue：${repairExhaustedCount}`, 'meta-line'));
      return box;
    }

    function renderGenerationQueue(item, project, chapters, guidance) {
      const queue = createNode('div', '', 'operator-queue');
      const control = item.generation_control || {};
      const reviewItems = guidance.reviewChapters.slice(0, 4);
      const failedItems = guidance.failedChapters.slice(0, 4);

      queue.appendChild(renderOperatorQueueHealth(item, project, guidance));

      reviewItems.forEach((chapter) => {
        const row = createNode('div', '', 'queue-item warn');
        const main = createNode('div', '', 'queue-main');
        main.appendChild(createNode('strong', `第${chapter.chapter_number}章 · 待人工检查`));
        const reviewLabels = [
          chapter.title ? `《${chapter.title}》` : 'Review checkpoint 已阻塞继续生成。',
          Number(chapter.repair_attempt_count || 0) > 0 ? `repair attempts=${chapter.repair_attempt_count}` : '',
          chapter.canon_risk_level ? `canon risk=${chapter.canon_risk_level}` : '',
        ].filter(Boolean).join(' · ');
        main.appendChild(createNode('span', reviewLabels));
        row.appendChild(main);
        const actions = createNode('div', '', 'queue-actions');
        const canReview = Boolean(item.project_id && chapter.chapter_number && chapter.has_review);
        const reviewButton = createButton('查看 Review', () => showReview(item.project_id, chapter.chapter_number), 'ghost');
        reviewButton.disabled = !canReview;
        actions.appendChild(reviewButton);
        const decisionButton = createButton('Review 决策链', () => jumpToReviewDecisionChain(item.project_id, chapter.chapter_number), 'ghost');
        decisionButton.disabled = !canReview;
        actions.appendChild(decisionButton);
        const acceptButton = createButton('接受', () => approveReview(item.project_id, chapter.chapter_number, false), 'secondary');
        acceptButton.disabled = !canReview;
        actions.appendChild(acceptButton);
        const softAcceptButton = createButton('Accept Soft', () => approveReview(item.project_id, chapter.chapter_number, false), 'secondary');
        softAcceptButton.disabled = !canReview;
        actions.appendChild(softAcceptButton);
        const retryButton = createButton('Retry Review', () => retryReview(item.project_id, chapter.chapter_number, false), 'secondary');
        retryButton.disabled = !canReview;
        actions.appendChild(retryButton);
        const continueButton = createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary');
        continueButton.disabled = !canReview;
        actions.appendChild(continueButton);
        const registerButton = createButton('Register Entity', () => registerSubworldEntityFromReview(item.project_id, chapter.chapter_number), 'ghost');
        registerButton.disabled = !canReview;
        actions.appendChild(registerButton);
        const backgroundDecisionButton = createButton('Background Decision', () => recordBackgroundEntityDecisionFromReview(item.project_id, chapter.chapter_number), 'ghost');
        backgroundDecisionButton.disabled = !canReview;
        actions.appendChild(backgroundDecisionButton);
        const obligationButton = createButton('Create Obligation', () => createObligationFromReview(item.project_id, chapter.chapter_number), 'ghost');
        obligationButton.disabled = !canReview;
        actions.appendChild(obligationButton);
        row.appendChild(actions);
        queue.appendChild(row);
      });

      failedItems.forEach((chapter) => {
        const row = createNode('div', '', 'queue-item failed');
        const main = createNode('div', '', 'queue-main');
        main.appendChild(createNode('strong', `第${chapter.chapter_number}章 · 生成失败`));
        main.appendChild(createNode('span', chapter.title ? `《${chapter.title}》` : '点击失败节点可查看记录到的失败信息。'));
        row.appendChild(main);
        const actions = createNode('div', '', 'queue-actions');
        actions.appendChild(createButton('查看原因', () => showStageFailureDetail(
          item,
          chapter,
          'chapter_failed',
          latestChapterStageEntry(taskHistory(item), chapter.chapter_number, 'chapter_failed'),
          'failed',
          'failed',
        ), 'ghost'));
        if (control.can_resume && item.project_id) {
          actions.appendChild(createButton('重试剩余章节', () => continueProjectGeneration(item.project_id), 'primary'));
        }
        row.appendChild(actions);
        queue.appendChild(row);
      });

      if (!reviewItems.length && !failedItems.length) {
        queue.appendChild(createNode('div', guidance.safety, 'checkpoint-note'));
      }
      return queue;
    }

    function renderGenerationControlPanel(item, project, chapters = []) {
      const guidance = generationGuidance(item, project, chapters);
      const control = item.generation_control || {};
      const panel = createNode('section', '', `control-cockpit ${guidance.tone || ''}`);
      const main = createNode('div', '', 'control-main');
      const copy = createNode('div', '', 'control-copy');
      copy.appendChild(createNode('div', guidance.eyebrow, 'control-eyebrow'));
      copy.appendChild(createNode('h3', guidance.title, 'control-title'));
      copy.appendChild(createNode('div', guidance.description, 'control-description'));
      const actions = createNode('div', '', 'control-actions');
      if (guidance.reviewChapters.length) {
        const firstReview = guidance.reviewChapters[0];
        const reviewButton = createButton('处理第一个 Review', () => showReview(item.project_id, firstReview.chapter_number), 'primary');
        reviewButton.disabled = !(item.project_id && firstReview?.chapter_number && firstReview?.has_review);
        actions.appendChild(reviewButton);
      } else if (control.can_resume && item.project_id) {
        actions.appendChild(createButton('继续生成剩余章节', () => continueProjectGeneration(item.project_id), 'primary'));
      }
      if (item.pausable) actions.appendChild(createButton(item.pause_requested ? '已请求暂停' : '安全暂停', () => pauseTask(item), 'secondary'));
      if (project && item.project_id && pickLatestPublishableChapter(projectAsBook(item, project, chapters))) {
        actions.appendChild(createButton('发布最近章节', () => openBookPublishModal(projectAsBook(item, project, chapters)), 'secondary'));
      }
      if (item.terminable) actions.appendChild(createButton('强制终止', () => terminateTask(item), 'danger'));
      if (actions.childNodes.length) copy.appendChild(actions);
      main.appendChild(copy);

      const side = createNode('div', '', 'control-side');
      appendControlSideRow(side, '当前阶段', guidance.currentStage);
      appendControlSideRow(side, '当前章', guidance.currentChapter || '未进入章节');
      appendControlSideRow(side, '下一步', guidance.next);
      appendControlSideRow(side, '计划状态', control.plan_state || 'none');
      appendControlSideRow(side, '写作状态', control.writing_state || 'not_started');
      appendControlSideRow(side, 'Review', control.review_state || 'none');
      appendControlSideRow(side, '阻断原因', control.blocking_reason?.code ? (control.blocking_reason.message || control.blocking_reason.code) : '无');
      appendControlSideRow(side, 'Next Gate', control.next_gate || '未计算');
      appendControlSideRow(side, '下次人工检查', control.review_interval_chapters ? `${control.chapters_until_review} 章后` : '未设置');
      appendControlSideRow(side, 'Replan 可触发', `${control.chapters_until_replan_eligible || 0} 章后`);
      main.appendChild(side);
      panel.appendChild(main);
      panel.appendChild(renderGenerationQueue(item, project, chapters, guidance));
      return panel;
    }
