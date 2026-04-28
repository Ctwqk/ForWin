from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(ValueError):
    pass


def _fernet_from_secret(secret: str) -> Fernet:
    normalized = str(secret or "").strip()
    if not normalized:
        raise SecretStoreError("Publisher session secret is not configured")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_json_with_secret(secret: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet_from_secret(secret).encrypt(raw).decode("ascii")


def decrypt_json_with_secret(secret: str, ciphertext: str) -> Any:
    try:
        raw = _fernet_from_secret(secret).decrypt(str(ciphertext or "").encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise SecretStoreError("Encrypted publisher session could not be decrypted") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretStoreError("Encrypted publisher session payload is invalid") from exc
