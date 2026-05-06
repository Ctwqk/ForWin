from __future__ import annotations

import hmac


class PublisherExtensionAuthError(ValueError):
    pass


class PublisherExtensionAuthNotConfigured(RuntimeError):
    pass


class ExtensionAuthService:
    def __init__(self, *, extension_api_key: str = "") -> None:
        self.extension_api_key = str(extension_api_key or "").strip()

    @staticmethod
    def normalize_client_id(client_id: str | None) -> str:
        return str(client_id or "").strip()

    def verify_extension_api_key(self, provided_key: str | None) -> None:
        expected = str(self.extension_api_key or "").strip()
        candidate = str(provided_key or "").strip()
        if not expected:
            raise PublisherExtensionAuthNotConfigured(
                "Publisher extension API key is not configured"
            )
        if not candidate or not hmac.compare_digest(candidate, expected):
            raise PublisherExtensionAuthError("Invalid publisher extension API key")

    def backend_ready_payload(self) -> dict[str, bool]:
        return {"extension_api_key_configured": bool(self.extension_api_key)}
