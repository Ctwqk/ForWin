    const CHAPTER_PIPELINE_STAGES = [
      ['assembling_context', '组装'],
      ['writing_chapter', '写作'],
      ['continuity_review', 'Candidate Draft Review'],
      ['repairing_chapter', '修复写作'],
      ['repair_review', '修复复核'],
      ['applying_canon', 'Canon'],
      ['running_post_acceptance', '后置'],
      ['paused_for_review', '人工检查'],
      ['chapter_failed', '失败'],
    ];
    const CHAPTER_RUNTIME_STAGES = new Set(CHAPTER_PIPELINE_STAGES.map(([stage]) => stage));

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
      if (entry?.message && ['chapter_failed', 'paused_for_review', 'scenario_rehearsal_blocked', 'provisional_failed', 'failed'].includes(stage)) return true;
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
          lines.push(`Review verdict：${review.verdict || '-'}`);
          if (Array.isArray(review.issues) && review.issues.length) {
            lines.push('Review 问题：');
            review.issues.forEach((issue, index) => {
              const tags = [issue.severity || '-', issue.issue_group || '', issue.issue_type || ''].filter(Boolean).join(' / ');
              lines.push(`${index + 1}. [${tags}] ${issue.description || issue.rule_name || '未命名问题'}`);
            });
          }
          if (review.recommended_action) lines.push(`建议动作：${review.recommended_action}`);
          if (review.review_summary) lines.push(`Review 摘要：${review.review_summary}`);
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
      const provisionalEntry = latestHistoryEntry(history, 'running_provisional_preview');
      const provisionalFailed = latestHistoryEntry(history, 'provisional_failed');
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
          key: 'legacy_preview',
          label: 'Legacy Preview',
          state: provisionalFailed ? 'failed' : (provisionalEntry ? 'completed' : 'upcoming'),
          note: provisionalFailed
            ? formatStageNote(provisionalFailed, 'legacy preview 失败')
            : formatStageNote(provisionalEntry, '默认关闭'),
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
      title.appendChild(createNode('span', '这里展示一次性 gate；逐章循环见下方章节流水线。'));
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
      if (!numbers.length) {
        list.appendChild(createNode('div', '暂无章节进度。', 'empty'));
      }
      numbers.forEach((number) => {
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
      });
      section.appendChild(list);
      return section;
    }

    async function loadProjectChapters(projectId) {
      return requestJson(`/api/projects/${projectId}/chapters`);
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

    async function showReview(projectId, chapterNumber) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        const lines = [
          `章节：第${chapterNumber}章《${data.title}》`,
          `状态：${data.status}`,
          `Verdict：${data.verdict}`,
          data.recommended_action ? `建议动作：${data.recommended_action}` : '',
          data.review_summary ? `摘要：${data.review_summary}` : '',
          Array.isArray(data.issues) && data.issues.length
            ? data.issues.map((issue, index) => `${index + 1}. [${[issue.severity, issue.issue_group, issue.issue_type].filter(Boolean).join(' / ')}] ${issue.description}`).join('\\n')
            : '无问题',
          Array.isArray(data.decision_refs) && data.decision_refs.length
            ? `决策链：${data.decision_refs.map((ref) => `${ref.event_type || 'event'}#${ref.id || ref.decision_event_id || '?'}`).join(', ')}`
            : '',
        ];
        window.alert(lines.join('\\n'));
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 读取失败');
      }
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
      openGovernanceActionModal({
        title: continueGeneration ? `接受并继续 · 第${chapterNumber}章` : `接受 Review · 第${chapterNumber}章`,
        description: continueGeneration
          ? '本次会先接受当前 review，再尝试继续生成；如果仍命中治理 gate，会保留阻断。'
          : '接受当前 chapter review，并把理由写入决策时间线。',
        confirmLabel: continueGeneration ? '接受并继续' : '接受 Review',
        errorTitle: 'Review 处理失败',
        onSubmit: ({ reason }) => executeApproveReview(projectId, chapterNumber, continueGeneration, reason),
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

    function renderGenerationQueue(item, project, chapters, guidance) {
      const queue = createNode('div', '', 'operator-queue');
      const control = item.generation_control || {};
      const reviewItems = guidance.reviewChapters.slice(0, 4);
      const failedItems = guidance.failedChapters.slice(0, 4);

      reviewItems.forEach((chapter) => {
        const row = createNode('div', '', 'queue-item warn');
        const main = createNode('div', '', 'queue-main');
        main.appendChild(createNode('strong', `第${chapter.chapter_number}章 · 待人工检查`));
        main.appendChild(createNode('span', chapter.title ? `《${chapter.title}》` : 'Review checkpoint 已阻塞继续生成。'));
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
        const continueButton = createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary');
        continueButton.disabled = !canReview;
        actions.appendChild(continueButton);
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
