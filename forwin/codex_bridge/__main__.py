from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "forwin.codex_bridge.http:app",
        host=os.environ.get("FORWIN_CODEX_BRIDGE_HOST", "127.0.0.1"),
        port=int(os.environ.get("FORWIN_CODEX_BRIDGE_PORT", "8897")),
    )


if __name__ == "__main__":
    main()
