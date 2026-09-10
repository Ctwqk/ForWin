from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherProfilePreservationTests(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("Bash is required")
        version = subprocess.check_output(
            [self.bash, "-c", "printf '%s' ${BASH_VERSINFO[0]}"], text=True
        )
        if int(version) < 4:
            self.skipTest("Linux launcher requires Bash 4+; run this test in the runtime image")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        (self.root / "browser_extension/forwin-publisher").mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "scripts/launch_linux_extension_browser.sh"
        shutil.copy2(source, self.scripts / source.name)
        for name in ("qualify_linux_extension_profile.py", "check_publisher_browser_heartbeat.py"):
            (self.scripts / name).touch()
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self._executable("xdpyinfo", "#!/bin/sh\nexit 0\n")
        self._executable("browser", "#!/bin/sh\nexec sleep 5\n")
        self._executable("python-stub", """#!/bin/sh
case "$1" in
  *qualify_linux_extension_profile.py)
    case "$2" in
      --check) [ "$LAUNCHER_SCENARIO" != fresh ] ;;
      --check-active) [ "$LAUNCHER_SCENARIO" != inactive ] ;;
      --qualify) printf 'qualified\n' >> "$LAUNCHER_QUALIFY_LOG" ;;
    esac ;;
  *check_publisher_browser_heartbeat.py) exit 1 ;;
esac
""")
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.journal = self.profile / "upload-journal-evidence"
        self.journal.write_bytes(b"mutation_started: unresolved external result\x00\xff")

    def _executable(self, name, content):
        path = self.binary / name
        path.write_text(content)
        path.chmod(0o700)

    def _run(self, scenario):
        env = {
            **os.environ,
            "PATH": str(self.binary) + os.pathsep + os.environ.get("PATH", ""),
            "FORWIN_EXTENSION_PYTHON": str(self.binary / "python-stub"),
            "FORWIN_EXTENSION_TEST_BROWSER": str(self.binary / "browser"),
            "FORWIN_EXTENSION_TEST_PROFILE": str(self.profile),
            "FORWIN_EXTENSION_DISPLAY_MODE": "external",
            "FORWIN_EXTENSION_DISPLAY": ":121",
            "FORWIN_EXTENSION_STARTUP_HEARTBEAT_TIMEOUT_SECONDS": "1",
            "FORWIN_EXTENSION_AUTO_RESET_PROFILE_ON_HEARTBEAT_FAILURE": "true",
            "LAUNCHER_SCENARIO": scenario,
            "LAUNCHER_QUALIFY_LOG": str(self.root / "qualification.log"),
        }
        return subprocess.run(
            [self.bash, str(self.scripts / "launch_linux_extension_browser.sh")],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )

    def test_inactive_qualified_profile_keeps_unresolved_journal(self):
        original = self.journal.read_bytes()
        result = self._run("inactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("qualified but inactive", result.stderr)
        self.assertTrue(self.journal.exists(), result.stdout + result.stderr)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertFalse((self.root / "qualification.log").exists())

    def test_heartbeat_failure_keeps_profile_even_with_old_auto_reset_setting(self):
        original = self.journal.read_bytes()
        result = self._run("heartbeat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not register", result.stderr)
        self.assertTrue(self.journal.exists(), result.stdout + result.stderr)
        self.assertEqual(self.journal.read_bytes(), original)
        self.assertFalse((self.root / "qualification.log").exists())

    def test_new_profile_can_still_be_qualified(self):
        self.journal.unlink()
        result = self._run("fresh")
        self.assertNotEqual(result.returncode, 0)  # The fake heartbeat remains unavailable.
        self.assertEqual((self.root / "qualification.log").read_text(), "qualified\n")


if __name__ == "__main__":
    unittest.main()
