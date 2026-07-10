from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PAGE_DOM_HELPERS_JS = """
    function clearNode(node) {
      if (!node) return;
      node.replaceChildren();
    }

    function createNode(tag, text = '', className = '') {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text) node.textContent = text;
      return node;
    }

    function createButton(label, onClick, className = '') {
      const button = document.createElement('button');
      if (className) button.className = className;
      button.textContent = label;
      button.addEventListener('click', onClick);
      return button;
    }

    function createLabeledField(labelText, control) {
      const wrap = document.createElement('div');
      const label = document.createElement('label');
      label.textContent = labelText;
      if (control.id) label.htmlFor = control.id;
      wrap.appendChild(label);
      wrap.appendChild(control);
      return wrap;
    }

    function isProjectBackedTaskId(taskId) {
      return typeof taskId === 'string' && taskId.startsWith('project-');
    }
"""

PAGE_FAVICON_DATA_URI = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%23121922'/%3E"
    "%3Ctext x='50%25' y='56%25' text-anchor='middle' font-size='30' "
    "font-family='Arial,sans-serif' font-weight='700' fill='%23f4efe6'%3EFW%3C/text%3E"
    "%3C/svg%3E"
)

_PAGE_ASSET_ROOT = Path(__file__).with_name("ui_assets")


@lru_cache(maxsize=None)
def _load_page_asset(relative_path: str) -> str:
    path = _PAGE_ASSET_ROOT / relative_path
    return path.read_text(encoding="utf-8").strip("\n")


def join_page_assets(*relative_paths: str) -> str:
    return "\n\n".join(_load_page_asset(relative_path) for relative_path in relative_paths if relative_path)


def render_page_document(
    *,
    title: str,
    css_paths: tuple[str, ...],
    body_paths: tuple[str, ...],
    script_paths: tuple[str, ...],
    replacements: dict[str, str],
) -> str:
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="icon" href="{PAGE_FAVICON_DATA_URI}">
  <style>
{join_page_assets(*css_paths)}
  </style>
</head>
<body>
{join_page_assets(*body_paths)}
  <script>
{join_page_assets(*script_paths)}
  </script>
</body>
</html>
"""
    for source, target in replacements.items():
        html = html.replace(source, target)
    return html
