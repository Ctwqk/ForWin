    async function boot() {
      await loadPlatforms();
      await loadUploadJobs(true);
      document.getElementById('platform').addEventListener('change', (event) => {
        selectedPlatformId = event.target.value || '';
      });
      await pingExtension();
      window.setInterval(loadPlatforms, 5000);
    }

    boot();
  
