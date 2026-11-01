from __future__ import annotations

import threading

from forwin.publish_hub.client import PublishHubClient

from .audit import PublisherAuditService
from .auth import ExtensionAuthService
from .attempts import PublisherAttemptService
from .backend_jobs import PublisherBackendJobRunner
from .browser_sessions import BrowserCookieCodec, BrowserSessionService
from .bindings import PublisherBindingService
from .canon_jobs import CanonPublisherJobService
from .comment_sync import CommentSyncService
from .connection_state import ExtensionConnectionService
from .covers import MiniMaxImageClient, PublisherCoverService
from .platform_catalog import PlatformCatalog
from .platform_catalogs import PlatformMetadataCatalog
from .preflight import PublisherPreflightService
from .receipts import PublisherReceiptService
from .upload_jobs import UploadJobService


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
        minimax_api_key: str = "",
        minimax_base_url: str = "",
        publisher_cover_dir: str = "",
        minimax_image_client=None,
        publish_hub_url: str = "",
        publish_hub_api_key: str = "",
        publish_hub_timeout_seconds: int = 20,
    ) -> None:
        self.session_factory = session_factory
        self.observability = observability
        self.publish_hub_url = str(publish_hub_url or "").strip()
        self.publish_hub_api_key = str(publish_hub_api_key or "").strip()
        self.publish_hub_timeout_seconds = publish_hub_timeout_seconds
        self._publish_hub_client: PublishHubClient | None = None
        self._publish_hub_client_lock = threading.Lock()
        self.platform_catalog = PlatformCatalog()
        self.platform_metadata_catalog = PlatformMetadataCatalog()
        self.preflight = PublisherPreflightService(
            platform_metadata_catalog=self.platform_metadata_catalog
        )
        self.auth = ExtensionAuthService(extension_api_key=extension_api_key)
        self.audit = PublisherAuditService(
            session_factory=session_factory, observability=observability
        )
        self.bindings = PublisherBindingService(session_factory=session_factory)
        self.cover_service = PublisherCoverService(
            session_factory=session_factory,
            image_client=minimax_image_client
            or MiniMaxImageClient(api_key=minimax_api_key, base_url=minimax_base_url),
            cover_dir=publisher_cover_dir,
        )
        self.backend_jobs = PublisherBackendJobRunner(
            session_factory=session_factory,
            cover_service=self.cover_service,
        )
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
            platform_metadata_catalog=self.platform_metadata_catalog,
            preflight=self.preflight,
            connection_state=self.connection_state,
            audit=self.audit,
            bindings=self.bindings,
            cover_service=self.cover_service,
        )
        self.receipts = PublisherReceiptService()
        self.attempts = PublisherAttemptService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            connection_state=self.connection_state,
            audit=self.audit,
            job_serializer=self.upload_jobs.serialize_upload_job,
            default_lease_seconds=heartbeat_stale_seconds,
        )
        self.upload_jobs.attempts = self.attempts
        self.upload_jobs.receipts = self.receipts
        self.upload_jobs.publish_hub_client_provider = lambda: self.publish_hub_client
        self.cover_service.publish_hub_client_provider = (
            self.upload_jobs.publish_hub_client_provider
        )
        self.canon_jobs = CanonPublisherJobService(
            session_factory=session_factory,
            upload_jobs=self.upload_jobs,
        )
        self.comment_sync = CommentSyncService(
            session_factory=session_factory,
            platform_catalog=self.platform_catalog,
            connection_state=self.connection_state,
            audit=self.audit,
        )

    @property
    def publish_hub_client(self) -> PublishHubClient | None:
        """The publish-hub client, built on first use; None unless both URL and key are set."""
        if not self.publish_hub_url or not self.publish_hub_api_key:
            return None
        with self._publish_hub_client_lock:
            if self._publish_hub_client is None:
                self._publish_hub_client = PublishHubClient(
                    self.publish_hub_url,
                    self.publish_hub_api_key,
                    self.publish_hub_timeout_seconds,
                )
            return self._publish_hub_client
