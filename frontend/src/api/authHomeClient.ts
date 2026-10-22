import { API_BASE_URL } from "./chatClient";
import { normalizePaperId, paperCanonicalId } from "../utils/graph";
import type {
  AuthSession,
  AuthUser,
  GraphEdge,
  GraphNode,
  InstitutionNode,
  Paper,
  PaperNode,
  WorkspaceChatMessage,
  WorkspaceDetail,
  WorkspaceGraph,
  WorkspaceSummary,
} from "../types";

const AUTH_STORAGE_KEY = "bigdipper.auth.session.v2";
const MOCK_USERS_KEY = "bigdipper.mock.users.v2";
const MOCK_WORKSPACES_KEY = "bigdipper.mock.workspaces.v2";

type StoredUser = AuthUser & { passwordHash: string };

type CreateWorkspaceRequest =
  | { entryType: "paper_search"; query: string; limit?: number }
  | { entryType: "agent_chat"; message: string };

function nowIso() {
  return new Date().toISOString();
}

function createId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}_${crypto.randomUUID()}`;
  }

  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson<T>(key: string, value: T) {
  localStorage.setItem(key, JSON.stringify(value));
}

function hashPassword(password: string) {
  let hash = 0;
  for (let index = 0; index < password.length; index += 1) {
    hash = (hash * 31 + password.charCodeAt(index)) >>> 0;
  }

  return `mock_hash_${hash.toString(16)}`;
}

function authHeaders(token?: string) {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 310000);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      signal: options.signal ?? controller.signal,
    });
    if (!response.ok) {
      throw new Error(`${path} failed with ${response.status}`);
    }

    return response.json() as Promise<T>;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function isHttpResponseError(error: unknown) {
  return error instanceof Error && / failed with \d{3}$/.test(error.message);
}

function isAuthError(error: unknown) {
  return error instanceof Error && error.message.includes("401");
}

function handleAuthError(error: unknown) {
  if (isAuthError(error)) {
    clearStoredSession();
    throw error;
  }
}

function preventHttpFallback(error: unknown) {
  if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
  handleAuthError(error);
  if (isHttpResponseError(error)) {
    throw error;
  }
}

export function loadStoredSession() {
  return readJson<AuthSession | null>(AUTH_STORAGE_KEY, null);
}

export function saveStoredSession(session: AuthSession) {
  writeJson(AUTH_STORAGE_KEY, session);
}

export function clearStoredSession() {
  localStorage.removeItem(AUTH_STORAGE_KEY);
}

function mockAuthSession(email: string, displayName: string) {
  const createdAt = nowIso();
  const user: AuthUser = {
    id: createId("user"),
    email,
    displayName,
    createdAt,
  };
  const session = { user, accessToken: `mock_token_${user.id}` };
  saveStoredSession(session);
  return session;
}

export async function registerUser(email: string, password: string, displayName: string) {
  try {
    const session = await requestJson<AuthSession>("/api/auth/register", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ email, password, displayName }),
    });
    saveStoredSession(session);
    return session;
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    const users = readJson<StoredUser[]>(MOCK_USERS_KEY, []);
    if (users.some((user) => user.email === email)) {
      throw new Error("Email already registered in local mock auth");
    }

    const session = mockAuthSession(email, displayName || email.split("@")[0]);
    writeJson(MOCK_USERS_KEY, [...users, { ...session.user, passwordHash: hashPassword(password) }]);
    return session;
  }
}

export async function loginUser(email: string, password: string) {
  try {
    const session = await requestJson<AuthSession>("/api/auth/login", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ email, password }),
    });
    saveStoredSession(session);
    return session;
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    const users = readJson<StoredUser[]>(MOCK_USERS_KEY, []);
    const user = users.find((candidate) => candidate.email === email && candidate.passwordHash === hashPassword(password));
    if (!user) {
      throw new Error("Invalid credentials");
    }

    const session = {
      user: {
        id: user.id,
        email: user.email,
        displayName: user.displayName,
        createdAt: user.createdAt,
      },
      accessToken: `mock_token_${user.id}`,
    };
    saveStoredSession(session);
    return session;
  }
}

export async function getCurrentUser(token: string) {
  try {
    return await requestJson<{ user: AuthUser }>("/api/auth/me", {
      headers: authHeaders(token),
    });
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    handleAuthError(error);

    const session = loadStoredSession();
    if (!session || session.accessToken !== token) {
      throw new Error("No stored session");
    }
    return { user: session.user };
  }
}

export async function logoutUser(token?: string) {
  try {
    if (token) {
      await requestJson<{ ok: boolean }>("/api/auth/logout", {
        method: "POST",
        headers: authHeaders(token),
      });
    }
  } finally {
    clearStoredSession();
  }
}

function normalizeGraphEdgeType(edgeType: string): GraphEdge["type"] {
  if (edgeType === "cites" || edgeType === "references") return "reference";
  if (edgeType === "cited_by") return "cited_by";
  if (edgeType === "similar" || edgeType === "extends" || edgeType === "uses_method") return "similar";
  if (edgeType === "authored_by") return "authored_by";
  if (edgeType === "affiliated_with") return "affiliated_with";
  if (edgeType === "coauthored_with") return "coauthored_with";
  if (edgeType === "institution_collaboration") return "institution_collaboration";
  if (edgeType === "has_field") return "has_field";
  if (edgeType === "published_in") return "published_in";
  if (edgeType === "local_to_global") return "local_to_global";
  if (edgeType === "entity_mentioned_in_paper") return "entity_mentioned_in_paper";
  if (edgeType === "co_occurrence" || edgeType === "co-occurrence") return "co_occurrence";
  if (edgeType === "semantic_relation" || edgeType === "entity_relation" || edgeType === "related_to") return "semantic_relation";
  return "similar";
}

function normalizeGraphEdges(edges: GraphEdge[]): GraphEdge[] {
  return edges.map((edge) => ({
    ...edge,
    type: normalizeGraphEdgeType(edge.type),
  }));
}

function withCanonicalPaperIdentity<T extends Paper>(paper: T) {
  const paperId = paperCanonicalId(paper);
  return {
    ...paper,
    paperId,
    data: { ...(paper.data ?? {}), paperId },
  };
}

function normalizeGraphNodeFields(graph: WorkspaceGraph): WorkspaceGraph {
  const institutionLabels = new Map(
    graph.nodes
      .filter((node): node is InstitutionNode => node.nodeType === "institution")
      .map((node) => [node.id, node.label]),
  );

  return {
    ...graph,
    papers: graph.papers?.map(withCanonicalPaperIdentity),
    edges: normalizeGraphEdges(graph.edges),
    nodes: graph.nodes.map((node) => {
      if (node.nodeType === "author") {
        return {
          ...node,
          institution: node.institution ?? institutionLabels.get(node.institutionId) ?? "",
          paperIds: node.paperIds ?? [],
          publicationCount: node.publicationCount ?? node.paperIds?.length ?? 0,
          metric: node.metric ?? node.publicationCount ?? node.paperIds?.length ?? 0,
        };
      }

      if (node.nodeType === "institution") {
        return {
          ...node,
          country: node.country ?? "",
          authorIds: node.authorIds ?? [],
          paperCount: node.paperCount ?? 0,
          metric: node.metric ?? node.paperCount ?? 0,
        };
      }

      if (node.nodeType === "paper") {
        const paper = withCanonicalPaperIdentity(node);
        return {
          ...paper,
          summary: node.summary ?? node.abstract ?? node.title,
          metric: node.metric ?? node.citationCount,
        };
      }

      return node;
    }),
  };
}

function searchMockPaperNodes(query: string, limit = 10): PaperNode[] {
  void limit;
  const title = query.trim() || "Offline fallback paper";

  return [
    {
      id: "offline-fallback-paper",
      nodeType: "paper",
      label: "Offline fallback",
      title: `Offline fallback: ${title}`,
      year: 2026,
      authors: ["Local Frontend Only"],
      venue: "Frontend fallback data",
      abstract: "This single paper appears only when the frontend cannot reach the backend API.",
      doi: "",
      citationCount: 0,
      topic: "offline fallback",
      summary: "Frontend-only fallback paper. Backend is not connected for this request.",
      metric: 0,
      position: { x: 560, y: 360 },
    },
  ];
}

function emptyGraph(title: string, queryContext: string): WorkspaceGraph {
  return {
    graphId: null,
    title,
    queryContext,
    seedNodeId: null,
    nodes: [],
    edges: [],
  };
}

function readMockWorkspaces() {
  return readJson<WorkspaceDetail[]>(MOCK_WORKSPACES_KEY, []);
}

function writeMockWorkspaces(workspaces: WorkspaceDetail[]) {
  writeJson(MOCK_WORKSPACES_KEY, workspaces);
}

function createMockWorkspace(request: CreateWorkspaceRequest): WorkspaceDetail {
  const createdAt = nowIso();
  const workspaceId = createId("ws");

  if (request.entryType === "paper_search") {
    const nodes = searchMockPaperNodes(request.query, request.limit ?? 10);
    const graphId = createId("gs");
    const workspace: WorkspaceSummary = {
      id: workspaceId,
      entryType: "paper_search",
      title: request.query,
      preview: `${nodes.length} paper candidates`,
      conversationId: createId("conv"),
      activeGraphStateId: graphId,
      createdAt,
      updatedAt: createdAt,
    };

    return {
      workspace,
      initialViewMode: "list",
      initialGraph: normalizeGraphNodeFields({
        graphId,
        title: `Offline fallback: ${request.query}`,
        queryContext: "Frontend offline fallback",
        seedNodeId: null,
        seedPaperId: null,
        defaultViewMode: "list",
        papers: nodes,
        warnings: ["Using frontend offline fallback data."],
        nodes,
        edges: [],
      }),
      initialChatMessages: [],
    };
  }

  const workspace: WorkspaceSummary = {
    id: workspaceId,
    entryType: "agent_chat",
    title: request.message,
    preview: "Agent conversation",
    conversationId: createId("conv"),
    activeGraphStateId: null,
    createdAt,
    updatedAt: createdAt,
  };
  const message: WorkspaceChatMessage = {
    role: "user",
    content: request.message,
    createdAt,
    pendingSend: true,
  };

  return {
    workspace,
    initialViewMode: "default",
    initialGraph: emptyGraph("Empty graph", "Waiting for Agent graph result"),
    initialChatMessages: [message],
  };
}

function normalizeWorkspaceDetail(
  payload: Partial<WorkspaceDetail> & { activeGraph?: WorkspaceGraph },
  fallbackRequest?: CreateWorkspaceRequest,
): WorkspaceDetail {
  const graph = payload.initialGraph ?? payload.activeGraph;
  if (!payload.workspace || !graph) {
    if (!fallbackRequest || import.meta.env.VITE_ENABLE_MOCK !== "true") throw new Error("Workspace response missing workspace or graph");
    return createMockWorkspace(fallbackRequest);
  }

  const normalizedGraph = normalizeGraphNodeFields({
    graphId: graph.graphId ?? null,
    title: graph.title ?? payload.workspace.title,
    queryContext: graph.queryContext ?? payload.workspace.title,
    seedNodeId: graph.seedNodeId ?? null,
    seedPaperId: normalizePaperId(graph.seedPaperId) ?? null,
    defaultViewMode: graph.defaultViewMode,
    papers: graph.papers ?? [],
    warnings: graph.warnings ?? [],
    nodes: graph.nodes ?? [],
    edges: graph.edges ?? [],
  });

  return {
    workspace: payload.workspace,
    initialViewMode: payload.initialViewMode ?? normalizedGraph.defaultViewMode ?? "default",
    initialGraph: normalizedGraph,
    initialChatMessages: payload.initialChatMessages ?? [],
  };
}

export async function listWorkspaces(token: string, limit = 20) {
  try {
    return await requestJson<{ items: WorkspaceSummary[]; nextCursor: string | null }>(`/api/workspaces?limit=${limit}`, {
      headers: authHeaders(token),
    });
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    const items = readMockWorkspaces()
      .map((detail) => detail.workspace)
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
      .slice(0, limit);
    return { items, nextCursor: null };
  }
}

export async function searchPapers(token: string, query: string, limit = 10) {
  try {
    return await requestJson<{ query: string; items: PaperNode[] }>("/api/papers/search", {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify({ query, limit }),
    });
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    return { query, items: searchMockPaperNodes(query, limit) };
  }
}

export async function createWorkspace(token: string, request: CreateWorkspaceRequest) {
  try {
    const payload = await requestJson<WorkspaceDetail>("/api/workspaces", {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify(request),
    });
    const detail = normalizeWorkspaceDetail(payload, request);
    const workspaces = readMockWorkspaces().filter((item) => item.workspace.id !== detail.workspace.id);
    writeMockWorkspaces([detail, ...workspaces]);
    return detail;
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    const detail = createMockWorkspace(request);
    writeMockWorkspaces([detail, ...readMockWorkspaces()]);
    return detail;
  }
}

export async function getWorkspace(token: string, workspaceId: string) {
  try {
    const payload = await requestJson<WorkspaceDetail & { activeGraph?: WorkspaceGraph }>(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      headers: authHeaders(token),
    });
    return normalizeWorkspaceDetail(payload);
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    const detail = readMockWorkspaces().find((item) => item.workspace.id === workspaceId);
    if (!detail) throw new Error("Workspace not found");
    return normalizeWorkspaceDetail(detail);
  }
}

export async function deleteWorkspace(token: string, workspaceId: string) {
  const localWorkspaces = readMockWorkspaces();
  const hasLocalWorkspace = localWorkspaces.some((item) => item.workspace.id === workspaceId);

  try {
    await requestJson<{ ok: boolean }>(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "DELETE",
      headers: authHeaders(token),
    });
  } catch (error) {
    if (import.meta.env.VITE_ENABLE_MOCK !== "true") throw error;
    preventHttpFallback(error);
    if (!hasLocalWorkspace) {
      throw error;
    }
  } finally {
    writeMockWorkspaces(localWorkspaces.filter((item) => item.workspace.id !== workspaceId));
  }
}

export function graphToWorkspaceGraph(graph: WorkspaceGraph): { nodes: GraphNode[]; edges: GraphEdge[] } {
  return { nodes: graph.nodes, edges: graph.edges as GraphEdge[] };
}
