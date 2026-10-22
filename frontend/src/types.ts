export type Paper = {
  id: string;
  /** Backend paper identity. Unlike `id`, this must not contain the graph-only `paper_` prefix. */
  paperId?: string;
  title: string;
  year: number;
  authors: string[];
  venue: string;
  abstract: string;
  doi: string;
  citationCount: number;
  topic: string;
  position: {
    x: number;
    y: number;
  };
  data?: Record<string, unknown>;
};

export type NodeType =
  | "paper"
  | "author"
  | "institution"
  | "field"
  | "semantic_entity"
  | "global_semantic_entity"
  | "unknown";

export type CanonicalNodeType = NodeType | "source";

export type NodeTypeFilter = "all" | NodeType;

export type BaseGraphNode = {
  id: string;
  nodeType: NodeType;
  label: string;
  summary: string;
  metric: number;
  position: {
    x: number;
    y: number;
  };
  data?: Record<string, unknown>;
};

export type PaperNode = BaseGraphNode & Paper & {
  nodeType: "paper";
};

export type AuthorNode = BaseGraphNode & {
  nodeType: "author";
  institutionId: string;
  institution: string;
  paperIds: string[];
  publicationCount: number;
  authorId?: string;
  displayName?: string;
  authorName?: string;
  orcid?: string;
  worksCount?: number;
  citedByCount?: number;
  hIndex?: number;
  currentGraphPaperCount?: number;
  affiliations?: {
    institutionId?: string;
    affiliationName?: string;
    rawAffiliationString?: string;
    authorPosition?: string;
  }[];
};

export type InstitutionNode = BaseGraphNode & {
  nodeType: "institution";
  country: string;
  authorIds: string[];
  paperCount: number;
  institutionId?: string;
  affiliationName?: string;
  countryCode?: string;
  type?: string;
  homepageUrl?: string;
  ror?: string;
  hIndex?: number;
  productivity?: number;
  currentGraphAuthorCount?: number;
  currentGraphPaperCount?: number;
};

export type FieldNode = BaseGraphNode & {
  nodeType: "field";
  fieldId?: string;
  level?: number;
  worksCount?: number;
  citedByCount?: number;
};

export type SemanticEntityNode = BaseGraphNode & {
  nodeType: "semantic_entity";
  entityId?: string;
  entityName?: string;
  entityType?: string;
  comment?: string;
  properties?: Record<string, unknown>;
};

export type GlobalSemanticDataset = "dblp" | "astro";

export type GlobalSemanticExamplePaper = {
  paperId: string;
  title?: string;
  year?: number;
};

export type GlobalSemanticEntityNode = BaseGraphNode & {
  nodeType: "global_semantic_entity";
  globalEntityId: string;
  canonicalName: string;
  entityType?: string;
  paperCount: number;
  mentionCount: number;
  datasets: string[];
  datasetScope?: GlobalSemanticDataset;
  examplePapers?: string[];
  examplePaperDetails?: GlobalSemanticExamplePaper[];
  typeDistribution?: Record<string, number>;
};

export type UnknownNode = BaseGraphNode & {
  nodeType: "unknown";
  originalNodeType?: string;
};

export type GraphNode =
  | PaperNode
  | AuthorNode
  | InstitutionNode
  | FieldNode
  | SemanticEntityNode
  | GlobalSemanticEntityNode
  | UnknownNode;

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  type: "reference" | "cites" | "cited_by" | "similar" | "authored_by" | "affiliated_with" | "coauthored_with" | "institution_collaboration" | "has_field" | "published_in" | "local_to_global" | "entity_mentioned_in_paper" | "co_occurrence" | "semantic_relation";
  label?: string;
  relationName?: string;
  weight?: number;
  paperCount?: number;
  data?: Record<string, unknown>;
};

export type PaperEdge = GraphEdge & {
  type: "reference" | "cites" | "cited_by" | "similar";
};

export type GraphResponse = {
  graphId?: string | null;
  workspaceId?: string | null;
  title?: string;
  nodes: GraphNode[];
  papers: Paper[];
  edges: GraphEdge[];
  seedPaperId?: string;
  seedNodeId?: string;
  queryContext?: string;
  defaultViewMode?: ViewMode;
  warnings?: string[];
};

export type GlobalSemanticGraphQueryRequest = {
  seedPaperId: string;
  seedEntityId: string;
  dataset: GlobalSemanticDataset;
  minPaperCount: number;
  edgeMinWeight: number;
  edgeTypes: Array<"co_occurrence" | "semantic_relation">;
  depth: 1 | 2;
  limit: number;
};

export type GlobalSemanticGraphExpandRequest = {
  seedPaperId: string;
  seedEntityId: string;
  dataset: "dblp" | "astro";
  minPaperCount: number;
  edgeMinWeight: number;
  edgeTypes: Array<"co_occurrence" | "semantic_relation">;
  depth: 1 | 2;
  limit: number;
  evidenceLimit: number;
};

export type GlobalSemanticGraphStats = {
  dataset: GlobalSemanticDataset;
  minPaperCount: number;
  edgeMinWeight: number;
  depth: number;
  returnedNodeCount: number;
  returnedEdgeCount: number;
  availableNeighborCount: number;
  truncated: boolean;
  buildId?: string | null;
  builtAt?: string | null;
};

export type GlobalSemanticGraphResponse = GraphResponse & {
  nodes: GlobalSemanticEntityNode[];
  stats: GlobalSemanticGraphStats;
};

export type GlobalSemanticGraphExpandSeed = {
  paperId: string;
  localEntityId: string;
  globalEntityId: string;
  canonicalName: string;
  entityType: string;
};

export type GlobalSemanticGraphExpandStats = {
  dataset: "dblp" | "astro";
  minPaperCount: number;
  edgeMinWeight: number;
  depth: number;
  evidenceLimit: number;
  nodeCount: number;
  edgeCount: number;
  paperNodeCount: number;
  globalEntityNodeCount: number;
  availableNeighborCount: number;
  truncated: boolean;
  buildId?: string | null;
  builtAt?: string | null;
};

export type GlobalSemanticGraphExpandResponse = {
  seed: GlobalSemanticGraphExpandSeed;
  nodes: GraphNode[];
  papers: Paper[];
  edges: GraphEdge[];
  stats: GlobalSemanticGraphExpandStats;
  warnings: string[];
};

export type ExploreMode = "similar" | "refs" | "cited_by" | "connections" | "semantic_graph";

export type ViewMode = "default" | "list" | "graph";

export type GraphQueryType =
  | "paper_search"
  | "paper_references"
  | "paper_citations"
  | "paper_neighborhood"
  | "paper_similar"
  | "paper_connections"
  | "paper_semantic_graph";

export type GraphQueryRequest = {
  queryType: GraphQueryType;
  workspaceId?: string;
  query?: string;
  seedPaperId?: string;
  limit?: number;
  includeReferences?: boolean;
  includeCitations?: boolean;
  includeAuthors?: boolean;
  includeInstitutions?: boolean;
  includeFields?: boolean;
};

export type GraphStateSummary = {
  id: string;
  title: string;
  queryContext: string;
  seedNodeId: string | null;
  createdAt: string;
};


export type AuthUser = {
  id: string;
  email: string;
  displayName: string;
  createdAt: string;
};

export type AuthSession = {
  user: AuthUser;
  accessToken: string;
};

export type WorkspaceEntryType = "paper_search" | "agent_chat";

export type WorkspaceSummary = {
  id: string;
  entryType: WorkspaceEntryType;
  title: string;
  preview: string;
  conversationId: string | null;
  activeGraphStateId: string | null;
  createdAt: string;
  updatedAt: string;
};

export type WorkspaceGraph = {
  graphId: string | null;
  title: string;
  queryContext: string;
  seedNodeId: string | null;
  seedPaperId?: string | null;
  defaultViewMode?: ViewMode;
  papers?: Paper[];
  warnings?: string[];
  nodes: GraphNode[];
  edges: GraphEdge[];
};

export type WorkspaceChatMessage = {
  runId?: string | null;
  graphArtifact?: ChatGraphArtifact | null;
  researchSteps?: ResearchStep[];
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  pendingSend?: boolean;
};

export type WorkspaceDetail = {
  workspace: WorkspaceSummary;
  initialViewMode: ViewMode;
  initialGraph: WorkspaceGraph;
  initialChatMessages: WorkspaceChatMessage[];
};

export type ToolCallSummary = {
  name: string;
  arguments?: Record<string, unknown>;
  summary?: string;
};

export type ChatFrontendPayload = {
  type?: string;
  nodes?: GraphNode[];
  papers?: Paper[];
  edges?: GraphEdge[];
  seedPaperId?: string;
  seedNodeId?: string;
  queryContext?: string;
};

export type ChatGraphArtifact = {
  status: "available" | "none" | "failed";
  runId?: string;
  title?: string;
  reason?: string;
  nodePreviewCount?: number;
  edgePreviewCount?: number;
  warnings?: string[];
};

export type AgentEvidenceGraphRequest = {
  workspaceId: string;
  forceRefresh?: boolean;
  maxNodes?: number;
  includeAuthors?: boolean;
  includeInstitutions?: boolean;
  includeCitationEdges?: boolean;
};

export type AgentEvidenceGraphResponse = {
  runId: string;
  status: "completed" | "unavailable" | "failed";
  graph: GraphResponse | null;
  missingReportEntities?: string[];
  warnings?: string[];
};

export type RunStatus = "completed" | "cancelled" | "failed";

export type TraceStepKind = "plan" | "tool" | "observation" | "llm" | "final" | "error" | "cancel";

export type TraceStepStatus = "pending" | "running" | "completed" | "cancelled" | "failed";

export type TraceStep = {
  id: string;
  round: number;
  kind: TraceStepKind;
  status: TraceStepStatus;
  title: string;
  summary: string;
  toolName?: string;
  arguments?: Record<string, unknown>;
  resultSummary?: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
};

export type ResourceUsageSummary = {
  llmCalls?: number | null;
  toolCalls?: number | null;
  dbQueries?: number | null;
  postgresQueries?: number | null;
  sqliteQueries?: number | null;
  graphQueries?: number | null;
  vectorQueries?: number | null;
  embeddingCalls?: number | null;
  rerankCalls?: number | null;
  pdfCalls?: number | null;
  visionCalls?: number | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
  totalTokens?: number | null;
  cpuPercent?: number | null;
  memoryMb?: number | null;
  peakMemoryMb?: number | null;
};

export type ResourceUsageModule = {
  name: string;
  type: string;
  calls?: number | null;
  latencyMs?: number | null;
  status?: string | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
  totalTokens?: number | null;
};

export type ResourceUsageTraceStep = {
  stepId: string;
  name: string;
  type: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  latencyMs?: number | null;
  status?: string | null;
  inputPreview?: string | null;
  model?: string | null;
  backend?: string | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
  totalTokens?: number | null;
};

export type ResourceUsage = {
  shouldDisplay?: boolean;
  requestId?: string | null;
  conversationId?: string | null;
  messageId?: string | null;
  status?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  latencyMs?: number | null;
  summary?: ResourceUsageSummary | null;
  modules?: ResourceUsageModule[] | null;
  trace?: ResourceUsageTraceStep[] | null;
};

export type ResearchStep = {
  id: string;
  round: number;
  publicName: string;
  status: "success" | "failed";
  parameters?: Record<string, unknown>;
  queryText?: string | null;
  queryLanguage?: "sql" | "mmsql" | null;
  toolLatencyMs?: number | null;
  databaseLatencyMs?: number | null;
  rowCount?: number | null;
  resultSummary?: string | null;
  selectedForReport?: boolean;
};

export type ChatRequest = {
  workspaceId?: string;
  conversationId?: string;
  runId: string;
  message: string;
  includeTrace?: boolean;
};

export type ChatResponse = {
  conversationId: string;
  runId: string;
  status: RunStatus;
  message: string;
  toolCalls?: ToolCallSummary[];
  trace?: TraceStep[];
  researchSteps?: ResearchStep[];
  frontendPayload?: ChatFrontendPayload;
  graphArtifact?: ChatGraphArtifact;
  resourceUsage?: ResourceUsage;
  isMock?: boolean;
};

export type CancelResponse = {
  runId: string;
  status: "cancelled" | "completed" | "not_found";
  cancelledAt?: string;
};
