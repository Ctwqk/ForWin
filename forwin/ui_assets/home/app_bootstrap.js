    async function bootstrap() {
      document.getElementById('task_generation_operation_mode').value = @@OPERATION_MODE_JSON@@;
      document.getElementById('task_generation_freeze_failed_candidates').checked = @@FREEZE_FAILED_JSON@@;
      document.getElementById('task_generation_min_chapter_chars').value = @@MIN_CHAPTER_CHARS_JSON@@;
      document.getElementById('config_generation_operation_mode').value = @@OPERATION_MODE_JSON@@;
      document.getElementById('config_generation_freeze_failed_candidates').checked = @@FREEZE_FAILED_JSON@@;
      document.getElementById('config_generation_min_chapter_chars').value = @@MIN_CHAPTER_CHARS_JSON@@;
      document.getElementById('config_generation_review_interval_chapters').value = @@REVIEW_INTERVAL_CHAPTERS_JSON@@;
      await loadSettings();
      await loadPlatforms();
      await loadBooks();
      await loadTaskCenter();
      setGlobalStatus('首页已加载。先看书本，再按需要进入任务中心。');
      window.setInterval(async () => {
        await loadPlatforms();
      }, 5000);
      window.setInterval(async () => {
        if (!taskPollHasActive && !currentDrawerTask) return;
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask) {
          await refreshCurrentDrawerIfChanged();
        }
      }, 2500);
    }

    bootstrap();
  
