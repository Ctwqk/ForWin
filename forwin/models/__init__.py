from __future__ import annotations

from .base import Base, get_engine, get_session_factory, init_db, new_id
from .canon import CanonCommitRecord
from .draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from .canon_quality import (
    CanonAdmissionRunRow,
    CanonQualitySignalRow,
    ChapterBodyMetricRow,
    QualityAnalysisRunRow,
)
from .entity import Entity, EntityAlias
from .genesis import BookGenesisRevision, PromptTrace
from .planning_control import BandCheckpoint
from .planning_v6 import ArcBeatSheetRow, BandPlanRow
from .audit import DecisionEvent
from .book_frame import BookLexiconRow, StoryCalendarRow
from .world_conflict import WorldConflictResultRefRow, WorldPlanConflictRow
from .observability import PerformanceSpan
from .outbox import OutboxEvent
from .maintenance import PostCanonMaintenanceRun
from .narrative_obligation import (
    FuturePlanAuditRunRow,
    NarrativeObligationRow,
    NarrativePlanPatchRow,
)
from .phase import (
    BandExperiencePlan,
    ChapterRewriteAttempt,
)
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
from .subworld import SubWorld
from .task import GenerationTask
from .map import (
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
from .book_state import (
    BookCognitionSnapshotRow,
    CharacterIdentityMapRow,
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
    "ArcBeatSheetRow",
    "BandPlanRow",
    "ProjectProgressionRule",
    "GenerationTask",
    "SubWorld",
    # entity
    "Entity",
    "EntityAlias",
    "BookGenesisRevision",
    "PromptTrace",
    "BandCheckpoint",
    "DecisionEvent",
    "PerformanceSpan",
    "NarrativeObligationRow",
    "NarrativePlanPatchRow",
    "FuturePlanAuditRunRow",
    "BandExperiencePlan",
    "ChapterRewriteAttempt",
    # draft
    "ChapterDraft",
    "ChapterReview",
    "CandidateDraftRecord",
    "CanonAdmissionRunRow",
    "CanonQualitySignalRow",
    "ChapterBodyMetricRow",
    "QualityAnalysisRunRow",
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
    "NarrativeNodeRow",
    "NarrativeEdgeRow",
    "WorldPlanConflictRow",
    "WorldConflictResultRefRow",
    # book frame
    "StoryCalendarRow",
    "BookLexiconRow",
]
