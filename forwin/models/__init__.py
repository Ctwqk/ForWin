from __future__ import annotations

from .base import Base, get_engine, get_session_factory, init_db, new_id
from .draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from .entity import Entity, EntityAlias, EntityState, RelationEdge
from .event import CanonEvent, EventEntityLink
from .genesis import BookGenesisRevision, PromptTrace
from .governance import BandCheckpoint, DecisionEvent, NarrativeConstraint
from .phase import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcStructureDraft,
    BandExperiencePlan,
    ChapterRewriteAttempt,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    ProvisionalChapterLedger,
    ProvisionalBandExecution,
    ProvisionalPromotionRecord,
    WorldProjectionDeltaRow,
)
from .phase4 import NPCIntentSnapshot, WorldSimulationTurn
from .publisher import (
    CommentSignalCandidate,
    FeedbackActionRecord,
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
    PublisherRawComment,
    PublisherUploadJob,
    ReaderScaleSnapshot,
    SignalWindowAggregate,
)
from .project import ArcPlanVersion, ChapterPlan, Project
from .subworld import SubWorld, SubWorldRosterItem
from .task import GenerationTask
from .thread import PlotThread, PlotThreadBeat
from .timeline import ChapterTimeline, StoryTimePoint
from .world_model import (
    WorldEditProposalRow,
    WorldModelCompileRunRow,
    WorldModelConflictRow,
    WorldModelLinkRow,
    WorldModelPageRow,
    WorldModelSnapshotRow,
)
from .world_v4 import (
    ArcWorldContractRow,
    BandWorldContractRow,
    BeliefRow,
    ChapterWorldDeltaIntentRow,
    CognitionSnapshotRow,
    KnowledgeGapRow,
    KnowledgeUpdateEventRow,
    ReaderExperienceDeltaRow,
    RevealEventRow,
    ScenarioRehearsalRunRow,
    ScenarioPlanPatchRow,
    WorldCompileRunV4Row,
    WorldDeltaRow,
    WorldLineRow,
    WorldModelSnapshotV4Row,
)

__all__ = [
    "Base",
    "get_engine",
    "get_session_factory",
    "init_db",
    "new_id",
    # project
    "Project",
    "ArcPlanVersion",
    "ChapterPlan",
    "GenerationTask",
    "SubWorld",
    "SubWorldRosterItem",
    # entity
    "Entity",
    "EntityAlias",
    "EntityState",
    "RelationEdge",
    # event
    "CanonEvent",
    "EventEntityLink",
    "BookGenesisRevision",
    "PromptTrace",
    "BandCheckpoint",
    "NarrativeConstraint",
    "DecisionEvent",
    "ProjectStageAnalysis",
    "ProjectReplanEvent",
    "ArcEnvelope",
    "ArcStructureDraft",
    "ArcEnvelopeAnalysis",
    "BandExperiencePlan",
    "ChapterRewriteAttempt",
    "ProvisionalChapterLedger",
    "ProvisionalBandExecution",
    "ProvisionalPromotionRecord",
    "WorldProjectionDeltaRow",
    "NPCIntentSnapshot",
    "WorldSimulationTurn",
    # thread
    "PlotThread",
    "PlotThreadBeat",
    # timeline
    "StoryTimePoint",
    "ChapterTimeline",
    # draft
    "ChapterDraft",
    "ChapterReview",
    "CandidateDraftRecord",
    "WorldModelSnapshotRow",
    "WorldModelPageRow",
    "WorldModelLinkRow",
    "WorldEditProposalRow",
    "WorldModelConflictRow",
    "WorldModelCompileRunRow",
    # publisher
    "CommentSignalCandidate",
    "FeedbackActionRecord",
    "PublisherExtensionClient",
    "PublisherExtensionPlatformState",
    "PublisherConnectionState",
    "PublisherBrowserSession",
    "PublisherBrowserSessionEntry",
    "PublisherUploadJob",
    "PublisherCommentSyncJob",
    "PublisherRawComment",
    "ReaderScaleSnapshot",
    "SignalWindowAggregate",
    # world v4
    "WorldLineRow",
    "WorldDeltaRow",
    "BeliefRow",
    "CognitionSnapshotRow",
    "KnowledgeGapRow",
    "RevealEventRow",
    "KnowledgeUpdateEventRow",
    "ReaderExperienceDeltaRow",
    "ScenarioRehearsalRunRow",
    "ScenarioPlanPatchRow",
    "WorldModelSnapshotV4Row",
    "WorldCompileRunV4Row",
    "ArcWorldContractRow",
    "BandWorldContractRow",
    "ChapterWorldDeltaIntentRow",
]
