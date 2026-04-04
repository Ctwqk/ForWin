from __future__ import annotations

"""Archived backend webpage executor.

This module remains only as an explicit compatibility stub while ForWin runs
with the browser extension as the sole webpage executor.
"""


class ServerPublisherUploader:
    def __init__(self, *_args, **_kwargs) -> None:
        raise RuntimeError(
            "ServerPublisherUploader has been archived. "
            "ForWin now uses the browser extension as the only webpage executor."
        )
