from __future__ import annotations

from fastapi import HTTPException

from forwin import api_world_model_routes


def test_world_studio_asset_rejects_common_prefix_sibling(tmp_path, monkeypatch) -> None:
    root = tmp_path / "world-studio"
    asset_root = root / "dist" / "assets"
    sibling = root / "dist" / "assets_evil"
    asset_root.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (sibling / "app.js").write_text("alert('outside')", encoding="utf-8")

    monkeypatch.setattr(api_world_model_routes, "_world_studio_root", lambda: root)

    try:
        api_world_model_routes._world_studio_asset("../assets_evil/app.js")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("sibling path with common prefix should be rejected")
