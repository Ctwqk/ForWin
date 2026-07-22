    async function boot() {
      document.getElementById('upload_resume_form').addEventListener('submit', resumeUploadJob);
      document.getElementById('upload_resume_cancel').addEventListener('click', closeUploadResumeDialog);
      await loadPlatforms();
      await loadUploadJobs(true);
      document.getElementById('platform').addEventListener('change', (event) => {
        selectedPlatformId = event.target.value || '';
      });
      await pingExtension();
      window.setInterval(loadPlatforms, 5000);
    }

    boot();
  
