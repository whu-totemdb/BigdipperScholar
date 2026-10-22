from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class ErrorEnvelope(ApiModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Paper(ApiModel):
    id: str
    title: str
    year: int
    authors: list[str]
    venue: str
    abstract: str
    doi: str | None = None
    citation_count: int = Field(alias="citationCount")
    topic: str
    doctype: str | None = None
    language: str | None = None


class PaperEdge(ApiModel):
    id: str
    source: str
    target: str
    type: Literal["cites", "extends", "similar", "uses_method"]


class GraphPosition(ApiModel):
    x: float = 0
    y: float = 0


class PaperSearchItem(Paper):
    node_type: Literal["paper"] = Field(default="paper", alias="nodeType")
    label: str
    summary: str
    metric: float = 0
    position: GraphPosition = Field(default_factory=GraphPosition)


class ToolSpec(ApiModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(ApiModel):
    name: str
    arguments: dict[str, Any]
    summary: str


class ResearchStep(ApiModel):
    id: str
    round: int
    public_name: str = Field(alias="publicName")
    status: Literal["success", "failed"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    query_text: str | None = Field(default=None, alias="queryText")
    query_language: Literal["sql", "mmsql"] | None = Field(default=None, alias="queryLanguage")
    tool_latency_ms: float | None = Field(default=None, alias="toolLatencyMs")
    database_latency_ms: float | None = Field(default=None, alias="databaseLatencyMs")
    row_count: int | None = Field(default=None, alias="rowCount")
    result_summary: str | None = Field(default=None, alias="resultSummary")
    selected_for_report: bool = Field(default=False, alias="selectedForReport")


RunStatus = Literal["completed", "cancelled", "failed"]
TraceKind = Literal["plan", "tool", "observation", "llm", "final", "error", "cancel"]
TraceStatus = Literal["pending", "running", "completed", "cancelled", "failed"]
ResourceType = Literal["llm", "tool", "database", "graph", "vector", "embedding", "rerank", "pdf", "vision", "system"]
ResourceStatus = Literal["success", "failed", "cancelled"]


class TraceStep(ApiModel):
    id: str
    round: int
    kind: TraceKind
    status: TraceStatus
    title: str
    summary: str
    tool_name: str | None = Field(default=None, alias="toolName")
    arguments: dict[str, Any] | None = None
    result_summary: str | None = Field(default=None, alias="resultSummary")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    duration_ms: float | None = Field(default=None, alias="durationMs")


class FrontendPayload(ApiModel):
    type: Literal["graph"] = "graph"
    papers: list[Paper] = Field(default_factory=list)
    edges: list[PaperEdge] = Field(default_factory=list)
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    query_context: str | None = Field(default=None, alias="queryContext")


class ResourceTraceStep(ApiModel):
    step_id: str = Field(alias="stepId")
    name: str
    type: ResourceType
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    latency_ms: float = Field(alias="latencyMs")
    status: ResourceStatus
    input_preview: str | None = Field(default=None, alias="inputPreview")
    model: str | None = None
    backend: str | None = None
    prompt_tokens: int | None = Field(default=None, alias="promptTokens")
    completion_tokens: int | None = Field(default=None, alias="completionTokens")
    total_tokens: int | None = Field(default=None, alias="totalTokens")


class ResourceUsageSummary(ApiModel):
    llm_calls: int = Field(default=0, alias="llmCalls")
    tool_calls: int = Field(default=0, alias="toolCalls")
    db_queries: int = Field(default=0, alias="dbQueries")
    postgres_queries: int = Field(default=0, alias="postgresQueries")
    sqlite_queries: int = Field(default=0, alias="sqliteQueries")
    graph_queries: int = Field(default=0, alias="graphQueries")
    vector_queries: int = Field(default=0, alias="vectorQueries")
    embedding_calls: int = Field(default=0, alias="embeddingCalls")
    rerank_calls: int = Field(default=0, alias="rerankCalls")
    pdf_calls: int = Field(default=0, alias="pdfCalls")
    vision_calls: int = Field(default=0, alias="visionCalls")
    prompt_tokens: int | None = Field(default=None, alias="promptTokens")
    completion_tokens: int | None = Field(default=None, alias="completionTokens")
    total_tokens: int | None = Field(default=None, alias="totalTokens")
    cpu_percent: float | None = Field(default=None, alias="cpuPercent")
    memory_mb: float | None = Field(default=None, alias="memoryMb")
    peak_memory_mb: float | None = Field(default=None, alias="peakMemoryMb")


class ResourceUsageModule(ApiModel):
    name: str
    type: ResourceType
    calls: int
    latency_ms: float = Field(alias="latencyMs")
    status: ResourceStatus
    prompt_tokens: int | None = Field(default=None, alias="promptTokens")
    completion_tokens: int | None = Field(default=None, alias="completionTokens")
    total_tokens: int | None = Field(default=None, alias="totalTokens")


class ResourceUsage(ApiModel):
    request_id: str = Field(alias="requestId")
    conversation_id: str = Field(alias="conversationId")
    message_id: str | None = Field(default=None, alias="messageId")
    status: RunStatus
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    latency_ms: float = Field(alias="latencyMs")
    should_display: bool = Field(default=False, alias="shouldDisplay")
    hidden_reason: str | None = Field(default=None, alias="hiddenReason")
    summary: ResourceUsageSummary
    modules: list[ResourceUsageModule] = Field(default_factory=list)
    trace: list[ResourceTraceStep] = Field(default_factory=list)


class ChatRequest(ApiModel):
    workspace_id: str | None = Field(default=None, alias="workspaceId")
    conversation_id: str | None = Field(default=None, alias="conversationId")
    run_id: str | None = Field(default=None, alias="runId")
    message: str = Field(min_length=1)
    include_trace: bool = Field(default=False, alias="includeTrace")


class ChatGraphArtifact(ApiModel):
    status: Literal["none", "available", "unavailable"] = "none"
    run_id: str | None = Field(default=None, alias="runId")
    title: str = "Evidence Graph"
    reason: str = ""
    node_preview_count: int = Field(default=0, alias="nodePreviewCount")
    edge_preview_count: int = Field(default=0, alias="edgePreviewCount")
    warnings: list[str] = Field(default_factory=list)


class ChatResponse(ApiModel):
    conversation_id: str = Field(alias="conversationId")
    run_id: str = Field(default="", alias="runId")
    status: RunStatus = "completed"
    message: str
    tool_calls: list[ToolCall] = Field(default_factory=list, alias="toolCalls")
    trace: list[TraceStep] | None = None
    research_steps: list[ResearchStep] = Field(default_factory=list, alias="researchSteps")
    frontend_payload: FrontendPayload | None = Field(default=None, alias="frontendPayload")
    graph_artifact: ChatGraphArtifact | None = Field(default=None, alias="graphArtifact")
    resource_usage: ResourceUsage | None = Field(default=None, alias="resourceUsage")
    internal_artifact: dict[str, Any] = Field(default_factory=dict, exclude=True, alias="internalArtifact")


class CancelRequest(ApiModel):
    reason: str | None = None


class CancelResponse(ApiModel):
    run_id: str = Field(alias="runId")
    status: Literal["cancelled", "completed", "not_found"]
    cancelled_at: datetime | None = Field(default=None, alias="cancelledAt")


class MessageOut(ApiModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(alias="createdAt")


class StoredToolCall(ApiModel):
    id: int
    name: str
    arguments: dict[str, Any]
    summary: str
    created_at: datetime = Field(alias="createdAt")


class ConversationResponse(ApiModel):
    conversation_id: str = Field(alias="conversationId")
    messages: list[MessageOut]
    tool_calls: list[StoredToolCall] = Field(default_factory=list, alias="toolCalls")


class AuthUser(ApiModel):
    id: str
    email: str
    display_name: str = Field(alias="displayName")
    created_at: datetime = Field(alias="createdAt")


class RegisterRequest(ApiModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = Field(alias="displayName", min_length=1)


class LoginRequest(ApiModel):
    email: str
    password: str = Field(min_length=1)


class AuthResponse(ApiModel):
    user: AuthUser
    access_token: str = Field(alias="accessToken")


class MeResponse(ApiModel):
    user: AuthUser


class LogoutResponse(ApiModel):
    ok: bool = True


class DeleteWorkspaceResponse(ApiModel):
    ok: bool = True


WorkspaceEntryType = Literal["paper_search", "agent_chat"]
ViewMode = Literal["default", "list", "graph"]


class WorkspaceSummary(ApiModel):
    id: str
    entry_type: WorkspaceEntryType = Field(alias="entryType")
    title: str
    preview: str
    conversation_id: str | None = Field(default=None, alias="conversationId")
    active_graph_state_id: str | None = Field(default=None, alias="activeGraphStateId")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class GraphNode(ApiModel):
    id: str
    node_type: str = Field(alias="nodeType")
    label: str
    title: str | None = None
    year: int | None = None
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    abstract: str | None = None
    doi: str | None = None
    citation_count: int | None = Field(default=None, alias="citationCount")
    topic: str | None = None
    doctype: str | None = None
    language: str | None = None
    summary: str | None = None
    metric: float | None = None
    position: GraphPosition = Field(default_factory=GraphPosition)
    paper_ids: list[str] = Field(default_factory=list, alias="paperIds")
    publication_count: int | None = Field(default=None, alias="publicationCount")
    institution_id: str | None = Field(default=None, alias="institutionId")
    institution: str | None = None
    author_ids: list[str] = Field(default_factory=list, alias="authorIds")
    paper_count: int | None = Field(default=None, alias="paperCount")
    country: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class GraphSnapshot(ApiModel):
    graph_id: str | None = Field(alias="graphId")
    title: str
    query_context: str = Field(alias="queryContext")
    seed_node_id: str | None = Field(default=None, alias="seedNodeId")
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    default_view_mode: ViewMode = Field(default="graph", alias="defaultViewMode")
    nodes: list[GraphNode] = Field(default_factory=list)
    papers: list[PaperSearchItem] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GraphResponse(ApiModel):
    graph_id: str = Field(alias="graphId")
    workspace_id: str | None = Field(default=None, alias="workspaceId")
    title: str
    query_context: str = Field(alias="queryContext")
    seed_node_id: str | None = Field(default=None, alias="seedNodeId")
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    default_view_mode: ViewMode = Field(default="graph", alias="defaultViewMode")
    nodes: list[GraphNode] = Field(default_factory=list)
    papers: list[PaperSearchItem] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


GlobalSemanticDataset = Literal["all", "dblp", "astro"]
GlobalSemanticEdgeType = Literal["co_occurrence", "semantic_relation"]


class GlobalSemanticExamplePaperDetail(ApiModel):
    paper_id: str = Field(alias="paperId")
    title: str | None = None
    year: int | None = None


class GlobalSemanticEntityItem(ApiModel):
    global_entity_id: str = Field(alias="globalEntityId")
    name_group_id: str = Field(alias="nameGroupId")
    canonical_name: str = Field(alias="canonicalName")
    normalized_name: str = Field(alias="normalizedName")
    entity_type: str = Field(alias="entityType")
    normalized_entity_type: str = Field(alias="normalizedEntityType")
    type_distribution: dict[str, int] = Field(default_factory=dict, alias="typeDistribution")
    paper_count: int = Field(alias="paperCount")
    mention_count: int = Field(alias="mentionCount")
    datasets: list[str] = Field(default_factory=list)
    example_papers: list[str] = Field(default_factory=list, alias="examplePapers")
    example_paper_details: list[GlobalSemanticExamplePaperDetail] = Field(
        default_factory=list,
        alias="examplePaperDetails",
    )


class GlobalSemanticOverviewResponse(ApiModel):
    dataset: GlobalSemanticDataset
    min_paper_count: int = Field(alias="minPaperCount")
    total_entity_count: int = Field(alias="totalEntityCount")
    eligible_entity_count: int = Field(alias="eligibleEntityCount")
    mention_count: int = Field(alias="mentionCount")
    co_occurrence_edge_count: int = Field(alias="coOccurrenceEdgeCount")
    semantic_relation_edge_count: int = Field(alias="semanticRelationEdgeCount")
    build_id: str | None = Field(default=None, alias="buildId")
    built_at: datetime | None = Field(default=None, alias="builtAt")
    build_min_paper_count: int | None = Field(default=None, alias="buildMinPaperCount")
    warnings: list[str] = Field(default_factory=list)


class GlobalSemanticEntitySearchResponse(ApiModel):
    query: str
    dataset: GlobalSemanticDataset
    min_paper_count: int = Field(alias="minPaperCount")
    items: list[GlobalSemanticEntityItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GlobalSemanticGraphQueryRequest(ApiModel):
    seed_global_entity_id: str | None = Field(default=None, alias="seedGlobalEntityId")
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    seed_entity_id: str | None = Field(default=None, alias="seedEntityId")
    query: str | None = None
    dataset: GlobalSemanticDataset = "all"
    min_paper_count: int = Field(default=2, ge=1, alias="minPaperCount")
    edge_min_weight: int = Field(default=2, ge=1, alias="edgeMinWeight")
    edge_types: list[GlobalSemanticEdgeType] = Field(
        default_factory=lambda: ["co_occurrence", "semantic_relation"],
        alias="edgeTypes",
    )
    entity_types: list[str] = Field(default_factory=list, alias="entityTypes")
    depth: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=100, ge=1, le=200)


GlobalSemanticExpandDataset = Literal["dblp", "astro"]


class GlobalSemanticGraphExpandRequest(ApiModel):
    """Parameters for adding a global neighborhood to an existing paper graph.

    This endpoint requires a local paper/entity seed and never accepts the
    combined 'all' materialized scope. The limit bounds both returned global
    entities and unique paper nodes, while evidenceLimit bounds papers per
    seed/entity or relation edge; the caller merges the payload into its graph.
    """

    seed_paper_id: str = Field(alias="seedPaperId", min_length=1)
    seed_entity_id: str = Field(alias="seedEntityId", min_length=1)
    dataset: GlobalSemanticExpandDataset
    min_paper_count: int = Field(default=2, ge=1, alias="minPaperCount")
    edge_min_weight: int = Field(default=2, ge=1, alias="edgeMinWeight")
    edge_types: list[GlobalSemanticEdgeType] = Field(
        default_factory=lambda: ["co_occurrence", "semantic_relation"],
        alias="edgeTypes",
    )
    entity_types: list[str] = Field(default_factory=list, alias="entityTypes")
    depth: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=100, ge=1, le=200)
    evidence_limit: int = Field(default=5, ge=1, le=50, alias="evidenceLimit")


class GlobalSemanticGraphStats(ApiModel):
    dataset: GlobalSemanticDataset
    min_paper_count: int = Field(alias="minPaperCount")
    edge_min_weight: int = Field(alias="edgeMinWeight")
    depth: int
    returned_node_count: int = Field(alias="returnedNodeCount")
    returned_edge_count: int = Field(alias="returnedEdgeCount")
    available_neighbor_count: int = Field(alias="availableNeighborCount")
    truncated: bool
    build_id: str | None = Field(default=None, alias="buildId")
    built_at: datetime | None = Field(default=None, alias="builtAt")


class GlobalSemanticGraphResponse(GraphResponse):
    stats: GlobalSemanticGraphStats


class GlobalSemanticGraphExpandSeed(ApiModel):
    paper_id: str = Field(alias="paperId")
    local_entity_id: str = Field(alias="localEntityId")
    global_entity_id: str = Field(alias="globalEntityId")
    canonical_name: str = Field(alias="canonicalName")
    entity_type: str = Field(alias="entityType")


class GlobalSemanticGraphExpandStats(ApiModel):
    dataset: GlobalSemanticExpandDataset
    min_paper_count: int = Field(alias="minPaperCount")
    edge_min_weight: int = Field(alias="edgeMinWeight")
    depth: int
    evidence_limit: int = Field(alias="evidenceLimit")
    node_count: int = Field(alias="nodeCount")
    edge_count: int = Field(alias="edgeCount")
    paper_node_count: int = Field(alias="paperNodeCount")
    global_entity_node_count: int = Field(alias="globalEntityNodeCount")
    available_neighbor_count: int = Field(default=0, alias="availableNeighborCount")
    truncated: bool = False
    build_id: str | None = Field(default=None, alias="buildId")
    built_at: datetime | None = Field(default=None, alias="builtAt")


class GlobalSemanticGraphExpandResponse(ApiModel):
    seed: GlobalSemanticGraphExpandSeed
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    stats: GlobalSemanticGraphExpandStats
    warnings: list[str] = Field(default_factory=list)


class GraphStateSummary(ApiModel):
    id: str
    title: str
    query_context: str = Field(alias="queryContext")
    seed_node_id: str | None = Field(default=None, alias="seedNodeId")
    created_at: datetime = Field(alias="createdAt")


class GraphStateListResponse(ApiModel):
    items: list[GraphStateSummary]


class GraphStateCreateGraph(ApiModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    papers: list[PaperSearchItem] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GraphStateCreateRequest(ApiModel):
    title: str
    query_context: str = Field(alias="queryContext")
    seed_node_id: str | None = Field(default=None, alias="seedNodeId")
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    default_view_mode: ViewMode = Field(default="graph", alias="defaultViewMode")
    graph: GraphStateCreateGraph


class GraphStateCreateResponse(ApiModel):
    graph_id: str = Field(alias="graphId")


GraphQueryType = Literal[
    "paper_search",
    "paper_references",
    "paper_citations",
    "paper_neighborhood",
    "paper_similar",
    "paper_connections",
    "paper_semantic_graph",
]


class GraphQueryRequest(ApiModel):
    query_type: GraphQueryType = Field(alias="queryType")
    workspace_id: str | None = Field(default=None, alias="workspaceId")
    query: str | None = None
    seed_paper_id: str | None = Field(default=None, alias="seedPaperId")
    limit: int = Field(default=20, ge=1, le=50)
    include_references: bool = Field(default=True, alias="includeReferences")
    include_citations: bool = Field(default=True, alias="includeCitations")
    include_authors: bool = Field(default=False, alias="includeAuthors")
    include_institutions: bool = Field(default=False, alias="includeInstitutions")
    include_fields: bool = Field(default=False, alias="includeFields")


class ChatSeedMessage(ApiModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(alias="createdAt")
    pending_send: bool = Field(default=False, alias="pendingSend")
    run_id: str | None = Field(default=None, alias="runId")
    graph_artifact: ChatGraphArtifact | None = Field(default=None, alias="graphArtifact")
    research_steps: list[ResearchStep] = Field(default_factory=list, alias="researchSteps")


class WorkspaceListResponse(ApiModel):
    items: list[WorkspaceSummary]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class WorkspaceCreateRequest(ApiModel):
    entry_type: WorkspaceEntryType = Field(alias="entryType")
    query: str | None = None
    message: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class WorkspaceCreateResponse(ApiModel):
    workspace: WorkspaceSummary
    initial_view_mode: ViewMode = Field(alias="initialViewMode")
    initial_graph: GraphSnapshot = Field(alias="initialGraph")
    initial_chat_messages: list[ChatSeedMessage] = Field(default_factory=list, alias="initialChatMessages")


class WorkspaceDetailResponse(ApiModel):
    workspace: WorkspaceSummary
    active_graph: GraphSnapshot | None = Field(default=None, alias="activeGraph")
    initial_chat_messages: list[ChatSeedMessage] = Field(default_factory=list, alias="initialChatMessages")


class PaperSearchRequest(ApiModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    year_from: int | None = Field(default=None, alias="yearFrom")
    year_to: int | None = Field(default=None, alias="yearTo")


class PaperSearchResponse(ApiModel):
    query: str
    items: list[PaperSearchItem]
    warnings: list[str] = Field(default_factory=list)


class PaperDetailResponse(ApiModel):
    paper: PaperSearchItem


AgentExperimentMode = Literal["mmdb_scholar", "baseline_polystore"]
AgentExperimentStatus = Literal["completed", "failed"]


class AgentExperimentRequest(ApiModel):
    query: str = Field(min_length=1)
    agent_mode: AgentExperimentMode = Field(default="mmdb_scholar", alias="agentMode")
    run_id: str | None = Field(default=None, alias="runId")
    max_rounds: int = Field(default=8, ge=1, le=20, alias="maxRounds")
    top_k: int = Field(default=20, ge=1, le=100, alias="topK")
    task_family: str | None = Field(default=None, alias="taskFamily")
    include_trace: bool = Field(default=False, alias="includeTrace")


class AgentExperimentResponse(ApiModel):
    run_id: str = Field(alias="runId")
    status: AgentExperimentStatus
    agent_mode: AgentExperimentMode = Field(alias="agentMode")
    report: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_bundle: dict[str, Any] = Field(default_factory=dict, alias="evidenceBundle")
    selected_observations: list[dict[str, Any]] = Field(default_factory=list, alias="selectedObservations")
    research_steps: list[ResearchStep] = Field(default_factory=list, alias="researchSteps")
    metrics: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)


class AgentEvidenceGraphRequest(ApiModel):
    workspace_id: str = Field(alias="workspaceId")
    force_refresh: bool = Field(default=False, alias="forceRefresh")
    max_nodes: int = Field(default=80, ge=1, le=200, alias="maxNodes")
    include_authors: bool = Field(default=True, alias="includeAuthors")
    include_institutions: bool = Field(default=True, alias="includeInstitutions")
    include_citation_edges: bool = Field(default=True, alias="includeCitationEdges")


class AgentEvidenceGraphResponse(ApiModel):
    run_id: str = Field(alias="runId")
    status: Literal["completed", "unavailable"]
    graph: GraphResponse | None = None
    missing_report_entities: list[dict[str, Any]] = Field(default_factory=list, alias="missingReportEntities")
    warnings: list[str] = Field(default_factory=list)
