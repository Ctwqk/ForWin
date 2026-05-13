from __future__ import annotations

from .audit import PublisherAuditService
from .auth import ExtensionAuthService
from .browser_sessions import BrowserCookieCodec, BrowserSessionService
from .comment_sync import CommentSyncService
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog
from .upload_jobs import CodexInterventionHandler, UploadJobService


class PublisherRuntimeService:
    def __init__(
        self,
        *,
        session_factory,
        extension_api_key: str,
        heartbeat_stale_seconds: int,
        preferred_client_id: str,
        publisher_session_secret: str,
        publisher_session_encryption_required: bool,
        strict_preferred_client: bool = False,
        observability=None,
        codex_intervention_handler: CodexInterventionHandler | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.observability = observability
        self.platform_catalog = PlatformCatalog()
        self.auth = ExtensionAuthService(extension_api_key=extension_api_key)
        self.audit = PublisherAuditService(session_factory=session_factory, observability=observability)
        self.browser_cookie_codec = BrowserCookieCodec(
            publisher_session_secret=publisher_session_secret,
            publisher_session_encryption_required=publisher_session_encryption_required,
        )
        self.connection_state = ExtensionConnectionService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            codec=self.browser_cookie_codec,
            heartbeat_stale_seconds=heartbeat_stale_seconds,
            preferred_client_id=preferred_client_id,
            strict_preferred_client=strict_preferred_client,
        )
        self.browser_sessions = BrowserSessionService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            codec=self.browser_cookie_codec,
            connection_state=self.connection_state,
        )
        self.upload_jobs = UploadJobService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            connection_state=self.connection_state,
            audit=self.audit,
            codex_intervention_handler=codex_intervention_handler,
        )
        self.comment_sync = CommentSyncService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            connection_state=self.connection_state,
            audit=self.audit,
        )
