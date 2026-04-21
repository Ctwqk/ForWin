    function stopUploadPolling() {
      if (uploadPollTimer) {
        window.clearTimeout(uploadPollTimer);
        uploadPollTimer = null;
      }
    }

    function renderUploadJob(data) {
      const lines = [
        `任务状态：${data.status}`,
        `平台：${data.display_name}`,
        `作品：${data.book_name}`,
        `章节：${data.chapter_title}`,
        data.message ? `说明：${data.message}` : '',
        data.error ? `错误：${data.error}` : '',
        data.current_url ? `当前页面：${data.current_url}` : '',
        data.started_at ? `开始时间：${data.started_at}` : '',
        data.finished_at ? `结束时间：${data.finished_at}` : '',
      ].filter(Boolean);
      const el = document.getElementById('upload_status');
      el.textContent = lines.join('\\n');
      el.className = `status ${data.status === 'succeeded' ? 'ok' : (data.status === 'failed' ? 'warn' : '')}`;
    }

    function stopUploadJobsPolling() {
      if (uploadJobsPollTimer) {
        window.clearTimeout(uploadJobsPollTimer);
        uploadJobsPollTimer = null;
      }
    }

    function renderUploadJobs(items) {
      const statusEl = document.getElementById('upload_jobs_status');
      const listEl = document.getElementById('upload_jobs_list');
      clearNode(listEl);
      if (!items.length) {
        statusEl.textContent = '最近没有上传任务。';
        statusEl.className = 'status';
        return false;
      }
      const activeCount = items.filter((item) => item.status === 'pending' || item.status === 'running').length;
      statusEl.textContent = activeCount
        ? `最近任务中有 ${activeCount} 条仍在执行或排队，列表会自动刷新。`
        : `最近展示 ${items.length} 条上传任务。`;
      statusEl.className = `status ${activeCount ? 'warn' : 'ok'}`;
      items.forEach((item) => {
        const node = document.createElement('div');
        node.className = 'task-item';
        node.appendChild(createNode('strong', `${item.display_name} | ${item.status} | ${item.book_name} / ${item.chapter_title}`));
        const lines = [
          item.extension_client_id ? `执行端：${item.extension_client_id}` : '执行端：等待分配',
          item.created_at ? `创建时间：${item.created_at}` : '',
          item.started_at ? `开始时间：${item.started_at}` : '',
          item.finished_at ? `结束时间：${item.finished_at}` : '',
          item.message ? `说明：${item.message}` : '',
          item.error ? `错误：${item.error}` : '',
        ].filter(Boolean);
        node.appendChild(createNode('div', lines.join('\\n'), 'status'));
        if (item.current_url) {
          const linkWrap = document.createElement('p');
          linkWrap.className = 'muted';
          linkWrap.appendChild(document.createTextNode('当前页面：'));
          const link = document.createElement('a');
          link.href = item.current_url;
          link.target = '_blank';
          link.rel = 'noreferrer';
          link.textContent = item.current_url;
          linkWrap.appendChild(link);
          node.appendChild(linkWrap);
        }
        listEl.appendChild(node);
      });
      return activeCount > 0;
    }

    async function loadUploadJobs(immediate = false) {
      stopUploadJobsPolling();
      const run = async () => {
        const res = await fetch('/api/publishers/upload-jobs?limit=30');
        const data = await res.json();
        const hasActive = renderUploadJobs(Array.isArray(data) ? data : []);
        uploadJobsPollTimer = window.setTimeout(run, hasActive ? 2000 : 12000);
      };
      if (immediate) {
        await run();
      } else {
        uploadJobsPollTimer = window.setTimeout(run, 0);
      }
    }

    async function pollUploadJob(jobId, immediate = false) {
      stopUploadPolling();
      const run = async () => {
        const res = await fetch(`/api/publishers/upload-jobs/${jobId}`);
        const data = await res.json();
        renderUploadJob(data);
        await loadUploadJobs(true);
        if (data.status === 'succeeded' || data.status === 'failed') {
          await loadPlatforms();
          return;
        }
        uploadPollTimer = window.setTimeout(run, 1500);
      };
      if (immediate) {
        await run();
      } else {
        uploadPollTimer = window.setTimeout(run, 0);
      }
    }

    async function upload(publish) {
      selectedPlatformId = document.getElementById('platform').value;
      const protagonistNames = [
        document.getElementById('book_protagonist_1').value,
        document.getElementById('book_protagonist_2').value,
      ].map((item) => item.trim()).filter(Boolean);
      const payload = {
        platform: selectedPlatformId,
        book_name: document.getElementById('book_name').value,
        chapter_title: document.getElementById('chapter_title').value,
        body: document.getElementById('body').value,
        upload_url: document.getElementById('upload_url').value || null,
        publish,
        create_if_missing: document.getElementById('create_if_missing').checked,
        book_meta: {
          audience: document.getElementById('book_audience').value,
          primary_category: document.getElementById('book_primary_category').value.trim(),
          protagonist_names: protagonistNames,
          intro: document.getElementById('book_intro').value.trim(),
        },
      };
      const res = await fetch('/api/publishers/upload-jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        document.getElementById('upload_status').textContent = data.detail || '创建上传任务失败';
        document.getElementById('upload_status').className = 'status warn';
        return;
      }
      renderUploadJob(data);
      document.getElementById('upload_status').textContent += '\\n任务已入队，等待首选 Linux 扩展优先领取。';
      await loadUploadJobs(true);
      await pollUploadJob(data.job_id, true);
    }
