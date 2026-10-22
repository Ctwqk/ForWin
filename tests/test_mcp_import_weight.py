from pathlib import Path
import subprocess
import sys


def test_mcp_http_import_does_not_load_sqlalchemy():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-I", "-c", "\n".join([
            "import sys",
            f"sys.path.insert(0, {str(repo_root)!r})",
            "import forwin.mcp.http",
            "assert not any(name == 'sqlalchemy' or name.startswith('sqlalchemy.') for name in sys.modules)",
            "assert not any(name == 'forwin.api_schema' or name.startswith('forwin.api_schema.') for name in sys.modules)",
        ])],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
