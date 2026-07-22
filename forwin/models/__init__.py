from __future__ import annotations

from .base import Base, get_engine, get_session_factory, init_db, new_id
from .canon import CanonCommitRecord
from .draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from .canon_quality import (
    ArtifactCollectionLedgerRow,
    CanonAdmissionRunRow,
    CanonQualitySignalRow,
    ChapterBodyMetricRow,
    CharacterStateTransitionRow,
    CountdownLedgerRow,
    QualityAnalysisRunRow,
    RevealRegistryEntryRow,
    StoryObligationRow,
)
from .entity import Entity, EntityAlias
from .genesis import BookGenesisRevision, PromptTrace
from .planning_control import BandCheckpoint, NarrativeConstraint
from .audit import DecisionEvent
from .observability import PerformanceSpan
from .outbox import OutboxEvent
from .maintenance import PostCanonMaintenanceRun
from .narrative_obligation import (
    FuturePlanAuditRunRow,
    NarrativeObligationRow,
    NarrativePlanPatchRow,
)
from .phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ChapterRewriteAttempt,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalPromotionRecord,
    WorldProjectionDeltaRow,
)
from .phase4 import WorldSimulationTurn
from .progression import ProjectProgressionRule
from .publisher import (
    CommentSignalCandidate,
    FeedbackActionRecord,
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherChapterBinding,
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherCoverAsset,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
    PublisherMilestone,
    PublisherOperatorAction,
    PublisherRawComment,
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherUploadReceipt,
    PublisherWorkBinding,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
)
from .projection import ProjectionCheckpoint
from .project import ArcPlanVersion, ChapterPlan, Project
from .subworld import SubWorld, SubWorldRosterItem
from .task import GenerationTask
from forwin.map.models import (
    MapEdgeRow,
    MapGenerationRunRow,
    MapNodeRow,
    MapRegionEdgeRow,
    MapRegionRow,
)
from .knowledge import (
    KnowledgeEditProposalRow,
    KnowledgeProjectionPageRow,
)
from .world_contract import (
    ArcWorldContractRow,
    BandWorldContractRow,
    ChapterWorldDeltaIntentRow,
)
from .book_state import (
    BookCognitionSnapshotRow,
    CharacterIdentityMapRow,
    BookReaderExperienceDeltaRow,
    BookReaderPromiseRow,
    CognitionOverlayPatchRow,
    CognitionOverlayRow,
    FactNodeRow,
    GraphDeltaPatchRow,
    GraphDeltaRow,
    MapSnapshotRow,
    NarrativeEdgeRow,
    NarrativeNodeRow,
    WorldEdgeRow,
    WorldNodeRow,
    WorldNodeStateRow,
    WorldSnapshotRow,
)

__all__ = [
    "Base",
    "get_engine",
    "get_session_factory",
    "init_db",
    "OutboxEvent",
    "CanonCommitRecord",
    "ProjectionCheckpoint",
    "PostCanonMaintenanceRun",
    "new_id",
    # project
    "Project",
    "ArcPlanVersion",
    "ChapterPlan",
    "ProjectProgressionRule",
    "GenerationTask",
    "SubWorld",
    "SubWorldRosterItem",
    # entity
    "Entity",
    "EntityAlias",
    "BookGenesisRevision",
    "PromptTrace",
    "BandCheckpoint",
    "NarrativeConstraint",
    "DecisionEvent",
    "PerformanceSpan",
    "NarrativeObligationRow",
    "NarrativePlanPatchRow",
    "FuturePlanAuditRunRow",
    "ProjectStageAnalysis",
    "ProjectReplanEvent",
    "ArcEnvelope",
    "ArcStructureDraft",
    "ArcEnvelopeAnalysis",
    "BandExperiencePlan",
    "ChapterRewriteAttempt",
    "ProvisionalPromotionRecord",
    "WorldProjectionDeltaRow",
    "WorldSimulationTurn",
    # draft
    "ChapterDraft",
    "ChapterReview",
    "CandidateDraftRecord",
    "ArtifactCollectionLedgerRow",
    "CanonAdmissionRunRow",
    "CanonQualitySignalRow",
    "ChapterBodyMetricRow",
    "CharacterStateTransitionRow",
    "CountdownLedgerRow",
    "QualityAnalysisRunRow",
    "RevealRegistryEntryRow",
    "StoryObligationRow",
    "KnowledgeProjectionPageRow",
    "KnowledgeEditProposalRow",
    # publisher
    "CommentSignalCandidate",
    "FeedbackActionRecord",
    "PublisherExtensionClient",
    "PublisherExtensionPlatformState",
    "PublisherConnectionState",
    "PublisherBrowserSession",
    "PublisherBrowserSessionEntry",
    "PublisherUploadJob",
    "PublisherUploadAttempt",
    "PublisherUploadReceipt",
    "PublisherOperatorAction",
    "PublisherWorkBinding",
    "PublisherChapterBinding",
    "PublisherCoverAsset",
    "PublisherMilestone",
    "PublisherCommentSyncJob",
    "PublisherRawComment",
    "ReaderScaleSnapshot",
    "SignalWindowAggregate",
    # planning state
    "ArcWorldContractRow",
    "BandWorldContractRow",
    "ChapterWorldDeltaIntentRow",
    # final book state
    "WorldNodeRow",
    "WorldNodeStateRow",
    "CharacterIdentityMapRow",
    "WorldEdgeRow",
    "FactNodeRow",
    "MapNodeRow",
    "MapEdgeRow",
    "MapRegionRow",
    "MapRegionEdgeRow",
    "MapGenerationRunRow",
    "GraphDeltaRow",
    "GraphDeltaPatchRow",
    "CognitionOverlayRow",
    "CognitionOverlayPatchRow",
    "WorldSnapshotRow",
    "MapSnapshotRow",
    "BookCognitionSnapshotRow",
    "BookReaderPromiseRow",
    "BookReaderExperienceDeltaRow",
    "NarrativeNodeRow",
    "NarrativeEdgeRow",
]
