"""Book state must remain usable below planning, world execution and canon admission."""

import ast
from importlib.util import resolve_name
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('forwin.planning', 'forwin.world_engine', 'forwin.canon')


def test_book_state_has_no_upward_imports():
    violations = []
    for path in sorted((ROOT / 'forwin/book_state').rglob('*.py')):
        parts = path.relative_to(ROOT).with_suffix('').parts
        package = '.'.join(parts[:-1])
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    module = resolve_name('.' * node.level + module, package)
                names = [module, *(f'{module}.{alias.name}' for alias in node.names)]
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                if ((isinstance(function, ast.Name) and function.id in {'__import__', 'import_module'})
                        or (isinstance(function, ast.Attribute) and function.attr == 'import_module')):
                    if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        names = [node.args[0].value]
            for name in names:
                if any(name == prefix or name.startswith(prefix + '.') for prefix in FORBIDDEN):
                    violations.append(f'{path.relative_to(ROOT)}:{node.lineno}: {name}')
    assert not violations, '\n'.join(violations)
