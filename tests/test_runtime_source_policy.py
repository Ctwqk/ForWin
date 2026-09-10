from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_runtime_source.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim\nCOPY forwin/ forwin/\n")
    old = tmp_path / "deploy/forwin-runtime-hotfixes/old/runtime-files/forwin"
    old.mkdir(parents=True)
    (old / "old.py").write_text("# read-only rollback evidence\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    _git(root, "add", ".")
    return subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(root), "--base", "HEAD"],
        text=True, capture_output=True, check=False,
    )


def test_ordinary_source_change_preserves_read_only_rollback(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "forwin").mkdir()
    (root / "forwin/service.py").write_text("def run():\n    return 1\n")
    result = _check(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_production_source_copy_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    copy = root / "deploy/release-copy/forwin"
    copy.mkdir(parents=True)
    (copy / "service.py").write_text("def run():\n    return 1\n")
    result = _check(root)
    assert result.returncode == 1
    assert "deploy/release-copy/forwin/service.py" in result.stdout


def test_retrofitting_old_hotfix_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    old = root / "deploy/forwin-runtime-hotfixes/old/runtime-files/forwin/old.py"
    old.write_text("# new patch hidden in old rollback tree\n")
    assert _check(root).returncode == 1


def test_compat_base_image_cannot_become_normal_build(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "Dockerfile").write_text("FROM forwin-forwin:compat-old\n")
    assert _check(root).returncode == 1


def test_runtime_string_patch_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    script = root / "deploy/patch.py"
    script.write_text(
        "from pathlib import Path\np = Path('/app/forwin/service.py')\n"
        "p.write_text(p.read_text().replace('old', 'new'))\n"
    )
    assert _check(root).returncode == 1


def test_atomic_deploy_state_write_does_not_count_as_source_patch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "deploy/state.py").write_text(
        "import os\n"
        "MIGRATION = ['/app/.venv/bin/python', '-m', 'forwin.migrations']\n"
        "def finish_write(temporary, state_path):\n"
        "    os.replace(temporary, state_path)\n"
    )
    result = _check(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_atomic_overwrite_of_runtime_source_remains_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "deploy/patch.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "os.replace('/tmp/patched.py', Path('/app/forwin/service.py'))\n"
    )
    assert _check(root).returncode == 1


def test_atomic_runtime_overwrite_through_static_target_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "deploy/patch.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "target = Path('/app/forwin/service.py')\n"
        "os.replace('/tmp/patched.py', target)\n"
    )
    assert _check(root).returncode == 1


def test_removing_retired_rollback_copy_is_allowed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    old = root / "deploy/forwin-runtime-hotfixes/old/runtime-files/forwin/old.py"
    old.unlink()
    assert _check(root).returncode == 0
