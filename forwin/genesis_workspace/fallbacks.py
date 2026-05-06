from __future__ import annotations

from typing import Any


def _legacy():
    from forwin import book_genesis as legacy

    return legacy


def initial_pack(project: Any, brief_seed: dict[str, Any] | None = None) -> dict[str, Any]:
    return _legacy()._initial_pack(project, brief_seed)


def initial_pack_dummy_merge(payload: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._initial_pack_dummy_merge(payload)


def fallback_brief(project: Any, book_brief: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_brief(project, book_brief)


def fallback_world(project: Any, pack: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_world(project, pack)


def fallback_map(pack: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_map(pack)


def fallback_story_engine(pack: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_story_engine(pack)


def fallback_book_blueprint(project: Any, pack: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_blueprint(project, pack)


def fallback_bootstrap(project: Any, pack: dict[str, Any]) -> dict[str, Any]:
    return _legacy()._fallback_bootstrap(project, pack)


def fallback_culture_profiles() -> list[dict[str, Any]]:
    return _legacy()._fallback_culture_profiles()

