    function renderUploadDrawer(item) {
      const body = document.getElementById('drawer_body');
      const top = createNode('section', '', 'detail-card');
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
      if (item.display_name) badges.appendChild(createNode('span', item.display_name, 'badge'));
      if (item.project_id) badges.appendChild(createNode('span', `Book ${item.project_id}`, 'badge'));
      top.appendChild(badges);
      const lines = [
        item.subtitle ? `章节：${item.subtitle}` : '',
        item.extension_client_id ? `执行端：${item.extension_client_id}` : '执行端：待领取',
        item.current_url ? `当前页面：${item.current_url}` : '',
        item.created_at ? `创建：${item.created_at}` : '',
        item.updated_at ? `更新：${item.updated_at}` : '',
        item.started_at ? `开始：${item.started_at}` : '',
        item.finished_at ? `结束：${item.finished_at}` : '',
        item.message ? `消息：${item.message}` : '',
        item.error ? `错误：${item.error}` : '',
      ].filter(Boolean).join('\\n');
      top.appendChild(createNode('div', lines, 'meta-line'));
      body.appendChild(top);

      const timelineCard = createNode('section', '', 'detail-card');
      timelineCard.appendChild(createNode('div', '时间线', 'task-id'));
      const timeline = createNode('div', '', 'drawer-grid');
      const phase = item.result_payload?.phase ? `phase=${item.result_payload.phase}` : '';
      [
        item.created_at ? `创建任务 | ${item.created_at}` : '',
        item.claimed_at ? `扩展领取 | ${item.claimed_at}` : '',
        item.started_at ? `开始执行 | ${item.started_at}` : '',
        phase ? `当前阶段 | ${phase}` : '',
        item.abort_requested ? '终止请求 | 已发出' : '',
        item.finished_at ? `结束执行 | ${item.finished_at}` : '',
        item.status ? `最终状态 | ${item.status}` : '',
      ].filter(Boolean).forEach((line) => {
        timeline.appendChild(createNode('div', line, 'meta-line'));
      });
      timelineCard.appendChild(timeline);
      body.appendChild(timelineCard);

      const payloadCard = createNode('section', '', 'detail-card');
      payloadCard.appendChild(createNode('div', '任务参数摘要', 'task-id'));
      payloadCard.appendChild(createNode('div', `平台：${item.platform || ''}\n作品：${item.title || ''}\n发布：${item.publish ? '是' : '否'}\n终止请求：${item.abort_requested ? '已发出' : '否'}`, 'meta-line'));
      const code = createNode('div', JSON.stringify(item.result_payload || {}, null, 2), 'code');
      payloadCard.appendChild(code);
      body.appendChild(payloadCard);
    }

    async function loadDrawerSnapshot(taskKind, taskId) {
      const item = await requestJson(`/api/task-center/items/${taskKind}/${taskId}`);
      if (item.task_kind !== 'generation' || !item.project_id) {
        return { item };
      }
      const project = await loadProjectDetail(item.project_id);
      const chapters = Array.isArray(project.chapters) ? project.chapters : await loadProjectChapters(item.project_id);
      return { item, project, chapters };
    }

    function captureDrawerBodyState() {
      const body = document.getElementById('drawer_body');
      const openChapterBodies = [];
      body.querySelectorAll('.chapter-body.open').forEach((node) => {
        openChapterBodies.push({
          id: node.id,
          text: node.textContent || '',
          loaded: node.dataset.loaded || '',
        });
      });
      return {
        scrollTop: body.scrollTop,
        openChapterBodies,
      };
    }

    function restoreDrawerBodyState(snapshot) {
      if (!snapshot) return;
      const body = document.getElementById('drawer_body');
      body.scrollTop = snapshot.scrollTop || 0;
      (snapshot.openChapterBodies || []).forEach((chapterBody) => {
        const node = document.getElementById(chapterBody.id);
        if (!node) return;
        node.textContent = chapterBody.text || '';
        if (chapterBody.loaded) node.dataset.loaded = chapterBody.loaded;
        node.classList.add('open');
      });
    }

    async function renderDrawerSnapshot(snapshot, { preserveBodyState = false, requestToken = drawerRequestToken } = {}) {
      if (requestToken !== drawerRequestToken) return;
      const overlay = document.getElementById('task_drawer_overlay');
      const body = document.getElementById('drawer_body');
      const bodyState = preserveBodyState ? captureDrawerBodyState() : null;
      const item = snapshot.item;
      currentDrawerTask = item;
      currentDrawerSignature = dataSignature(snapshot);
      document.getElementById('drawer_task_id').textContent = `${serializeTaskType(item.task_kind)} · ${item.task_id}`;
      document.getElementById('drawer_title').textContent = item.title || '未命名任务';
      document.getElementById('drawer_meta').textContent = [
        item.subtitle || '',
        item.project_id ? `书本 ${item.project_id}` : '',
        item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
      ].filter(Boolean).join(' | ');
      clearNode(body);
      overlay.classList.add('open');
      if (item.task_kind === 'generation') {
        await renderGenerationDrawer(item, snapshot.project || null, snapshot.chapters || null);
      } else {
        renderUploadDrawer(item);
      }
      if (requestToken !== drawerRequestToken) return;
      restoreDrawerBodyState(bodyState);
    }

    async function openTaskDrawer(taskKind, taskId) {
      const requestToken = ++drawerRequestToken;
      try {
        const snapshot = await loadDrawerSnapshot(taskKind, taskId);
        if (requestToken !== drawerRequestToken) return;
        await renderDrawerSnapshot(snapshot, {
          preserveBodyState: currentDrawerTask?.task_kind === taskKind && currentDrawerTask?.task_id === taskId,
          requestToken,
        });
      } catch (error) {
        if (requestToken !== drawerRequestToken) return;
        if (String(error?.message || '').includes('404')) {
          closeTaskDrawer();
          setGlobalStatus('任务详情不存在，已关闭右侧详情。', '任务详情');
          return;
        }
        setGlobalStatus(error.message || String(error), '任务详情读取失败');
      }
    }

    async function refreshCurrentDrawerIfChanged() {
      if (!currentDrawerTask) return;
      const requestToken = drawerRequestToken;
      try {
        const snapshot = await loadDrawerSnapshot(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        if (requestToken !== drawerRequestToken || !currentDrawerTask) return;
        const nextSignature = dataSignature(snapshot);
        if (nextSignature === currentDrawerSignature) return;
        await renderDrawerSnapshot(snapshot, { preserveBodyState: true, requestToken });
      } catch (error) {
        if (requestToken !== drawerRequestToken) return;
        if (String(error?.message || '').includes('404')) {
          closeTaskDrawer();
          setGlobalStatus('当前任务已不存在，已关闭右侧详情。', '任务详情');
          return;
        }
        setGlobalStatus(error.message || String(error), '任务详情刷新失败');
      }
    }

    function closeTaskDrawer(event) {
      if (event && event.target && event.target !== event.currentTarget) return;
      drawerRequestToken += 1;
      currentDrawerTask = null;
      currentDrawerSignature = '';
      document.getElementById('task_drawer_overlay').classList.remove('open');
    }

    async function openExtensionOptions() {
      try {
        await bridgeRequest('open-options', {}, 1500);
      } catch (error) {
        setGlobalStatus(`${error.message || String(error)}\n若尚未安装扩展，请解压并加载：${EXTENSION_INSTALL_PATH}`, '浏览器扩展');
      }
    }

    window.addEventListener('message', async (event) => {
      if (event.source !== window) return;
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object') return;
      if (data.channel !== EXTENSION_BRIDGE_CHANNEL || data.direction !== 'extension-to-page') return;
      if (data.kind === 'response' && data.correlationId) {
        const pending = pendingBridgeRequests.get(data.correlationId);
        if (!pending) return;
        window.clearTimeout(pending.timer);
        pendingBridgeRequests.delete(data.correlationId);
        if (data.ok) pending.resolve(data.payload);
        else pending.reject(new Error(data.error || '浏览器扩展返回失败。'));
        return;
      }
      if (data.kind !== 'event') return;
      if (data.event === 'login-status' && data.payload?.platform) {
        setGlobalStatus(data.payload.message || '登录状态有更新。', '平台登录');
        await loadPlatforms();
      }
      if (data.event === 'upload-status' && data.payload?.jobId) {
        setGlobalStatus(data.payload.message || '上传状态有更新。', '上传任务');
        await loadTaskCenter();
        if (currentDrawerTask?.task_kind === 'upload' && currentDrawerTask?.task_id === data.payload.jobId) {
          await openTaskDrawer('upload', data.payload.jobId);
        }
      }
    });
