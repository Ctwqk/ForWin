    async function bootstrap() {
      switchTab(initialHomeTabFromLocation());
      await loadSettings();
      await ensureFreshPlatforms({ force: true, reason: 'bootstrap' });
      await loadBooks();
      await loadTaskCenter();
      setGlobalStatus('首页已加载。先看书本，再按需要进入任务中心。');
      window.setInterval(async () => {
        if (!shouldAutoRefreshPlatforms()) return;
        await ensureFreshPlatforms({ force: true, reason: 'background_poll' });
      }, 5000);
      document.addEventListener('visibilitychange', () => {
        if (!shouldAutoRefreshPlatforms()) return;
        void ensureFreshPlatforms({ maxAgeMs: 1000, reason: 'visibility_resume' });
      });
      window.setInterval(async () => {
        if (!taskPollHasActive && !currentDrawerTask) return;
        const refreshResult = await loadTaskCenter();
        if (refreshResult?.booksImpactChanged && currentHomeTab === 'book') {
          await loadBooks();
        }
        if (currentDrawerTask) {
          await refreshCurrentDrawerIfChanged();
        }
      }, 2500);
    }

    bootstrap();
  
