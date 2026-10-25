"""Every Python module is imported in its own isolated interpreter."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
# No optional-dependency exclusions are currently required.
OPTIONAL_DEPENDENCY_EXCLUSIONS: dict[str, str] = {}


def _modules() -> list[str]:
    modules = set()
    for path in (ROOT / 'forwin').rglob('*.py'):
        parts = path.relative_to(ROOT).with_suffix('').parts
        if parts[-1] == '__init__':
            parts = parts[:-1]
        modules.add('.'.join(parts))
    return sorted(modules - OPTIONAL_DEPENDENCY_EXCLUSIONS.keys())


def _cold_import(module: str) -> str:
    setup = f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
    if module == 'forwin.migrations.env':
        # Alembic's entry script needs its execution context, even for an offline import.
        code = setup + '''
import importlib
import io
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
config = Config()
config.set_main_option('script_location', 'forwin/migrations')
config.set_main_option('sqlalchemy.url', 'sqlite://')
with EnvironmentContext(config, ScriptDirectory.from_config(config), as_sql=True,
                        output_buffer=io.StringIO(), fn=lambda revision, context: []):
    importlib.import_module('forwin.migrations.env')
'''
    else:
        code = setup + f'import importlib; importlib.import_module({module!r})'
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    return f'{module}:\n{result.stderr}' if result.returncode else ''


def test_modules_import_in_fresh_interpreters() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        failures = [failure for failure in pool.map(_cold_import, _modules()) if failure]
    assert not failures, '\n\n'.join(failures)
