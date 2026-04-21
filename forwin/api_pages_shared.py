from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from forwin.config import (
    DEFAULT_MINIMAX_BASE_URL,
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_MOONSHOT_BASE_URL,
    DEFAULT_MOONSHOT_MODEL,
)


LLM_PROVIDER_PRESETS = [
    {
        "id": "minimax",
        "label": "MiniMax 中文站",
        "default_name": "MiniMax 主账号",
        "base_url": DEFAULT_MINIMAX_BASE_URL,
        "sites": [
            {
                "label": "MiniMax 中文站 / 开放平台",
                "base_url": DEFAULT_MINIMAX_BASE_URL,
            },
        ],
        "default_model": DEFAULT_MINIMAX_MODEL,
        "recommended_models": [
            DEFAULT_MINIMAX_MODEL,
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2.1-highspeed",
            "MiniMax-M2",
            "MiniMax-M2-Her",
        ],
        "hint": "使用 MiniMax 中文站 OpenAI 兼容接口。",
    },
    {
        "id": "moonshot",
        "label": "Kimi 中文站 / Moonshot.cn",
        "default_name": "Kimi 主账号",
        "base_url": DEFAULT_MOONSHOT_BASE_URL,
        "sites": [
            {
                "label": "Kimi 中文站 / Moonshot.cn",
                "base_url": DEFAULT_MOONSHOT_BASE_URL,
            },
        ],
        "default_model": DEFAULT_MOONSHOT_MODEL,
        "recommended_models": [
            DEFAULT_MOONSHOT_MODEL,
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "kimi-k2",
            "kimi-k2-0905-preview",
            "kimi-k2-turbo-preview",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
            "moonshot-v1-8k",
        ],
        "hint": "Moonshot.cn 中文站 OpenAI 兼容接口，默认推荐 kimi-k2.5。",
    },
]


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
      wrap.appendChild(label);
      wrap.appendChild(control);
      return wrap;
    }

    function isProjectBackedTaskId(taskId) {
      return typeof taskId === 'string' && taskId.startsWith('project-');
    }
"""

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
