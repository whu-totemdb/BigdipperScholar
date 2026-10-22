import type {
  AgentEvidenceGraphRequest,
  AgentEvidenceGraphResponse,
  CancelResponse,
  ChatRequest,
  ChatResponse,
} from "../types";

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") || "";

export const DISABLE_CHAT_MOCK_FALLBACK =
  import.meta.env.VITE_DISABLE_CHAT_MOCK_FALLBACK !== "false";

const REQUEST_TIMEOUT_MS = 310_000;

function timeoutSignal(parentSignal?: AbortSignal) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const abortFromParent = () => controller.abort();
  parentSignal?.addEventListener("abort", abortFromParent, { once: true });

  return {
    signal: controller.signal,
    cleanup: () => {
      window.clearTimeout(timeoutId);
      parentSignal?.removeEventListener("abort", abortFromParent);
    },
  };
}

export function createRunId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }

  return `run_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

export async function sendChatMessage(
  request: ChatRequest,
  signal?: AbortSignal,
  accessToken?: string,
): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE_URL}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    signal,
    body: JSON.stringify({
      workspaceId: request.workspaceId,
      conversationId: request.conversationId || undefined,
      runId: request.runId,
      message: request.message,
      includeTrace: request.includeTrace ?? true,
    }),
  });

  if (!response.ok) {
    throw new Error(`Chat request failed with ${response.status}`);
  }

  const payload = (await response.json()) as Partial<ChatResponse>;

  return {
    conversationId: payload.conversationId ?? request.conversationId ?? `conv_${request.runId}`,
    runId: payload.runId ?? request.runId,
    status: payload.status ?? "completed",
    message: payload.message ?? "",
    toolCalls: payload.toolCalls ?? [],
    trace: payload.trace ?? [],
    researchSteps: payload.researchSteps ?? [],
    frontendPayload: payload.frontendPayload,
    graphArtifact: payload.graphArtifact,
    resourceUsage: payload.resourceUsage,
    isMock: payload.isMock,
  };
}

export async function fetchRunEvidenceGraph(
  accessToken: string,
  runId: string,
  request: AgentEvidenceGraphRequest,
  signal?: AbortSignal,
): Promise<AgentEvidenceGraphResponse> {
  const timeout = timeoutSignal(signal);

  try {
    const response = await fetch(`${API_BASE_URL}/api/chat/runs/${encodeURIComponent(runId)}/evidence-graph`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      signal: timeout.signal,
      body: JSON.stringify({
        workspaceId: request.workspaceId,
        forceRefresh: request.forceRefresh ?? false,
        maxNodes: request.maxNodes ?? 80,
        includeAuthors: request.includeAuthors ?? true,
        includeInstitutions: request.includeInstitutions ?? true,
        includeCitationEdges: request.includeCitationEdges ?? true,
      }),
    });

    if (!response.ok) {
      throw new Error(`Evidence graph request failed with ${response.status}`);
    }

    return response.json() as Promise<AgentEvidenceGraphResponse>;
  } finally {
    timeout.cleanup();
  }
}

export async function cancelChatRun(
  runId: string,
  reason = "user_cancelled",
): Promise<CancelResponse> {
  const response = await fetch(`${API_BASE_URL}/api/chat/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ reason }),
  });

  if (response.status === 404) {
    return { runId, status: "not_found" };
  }

  if (!response.ok) {
    throw new Error(`Cancel request failed with ${response.status}`);
  }

  return response.json() as Promise<CancelResponse>;
}

export function createMockChatResponse(
  message: string,
  conversationId?: string,
  runId = createRunId(),
): ChatResponse {
  const query = message.trim() || "current graph";

  return {
    conversationId: conversationId ?? `mock_${Date.now()}`,
    runId,
    status: "completed",
    message:
      `I am using the local mock fallback because the backend is unavailable. ` +
      `For "${query}", I would first search related papers, inspect citation paths, ` +
      `and summarize useful graph neighborhoods for your next step.`,
    isMock: true,
    toolCalls: [
      {
        name: "search_papers",
        arguments: { query, limit: 5 },
        summary: "mock fallback returned candidate papers from the local graph",
      },
      {
        name: "summarize_neighborhood",
        arguments: { scope: "selected or seed paper" },
        summary: "mock summary generated without a running backend",
      },
    ],
    trace: [
      {
        id: `${runId}_trace_1`,
        round: 1,
        kind: "plan",
        status: "completed",
        title: "Use local fallback",
        summary: "Backend chat request failed, so the frontend generated a local mock answer.",
        durationMs: 1,
      },
      {
        id: `${runId}_trace_2`,
        round: 1,
        kind: "tool",
        status: "completed",
        title: "Mock paper search",
        toolName: "search_papers",
        arguments: { query, limit: 5 },
        summary: "Simulated the search step without contacting the backend.",
        resultSummary: "Returned mock candidates from the local graph context.",
        durationMs: 1,
      },
    ],
  };
}
