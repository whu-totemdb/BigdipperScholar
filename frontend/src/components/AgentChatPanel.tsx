import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronDown,
  Copy,
  Database,
  Loader2,
  Network,
  RefreshCcw,
  Send,
  Sparkles,
  UserRound,
  XCircle,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE_URL,
  DISABLE_CHAT_MOCK_FALLBACK,
  cancelChatRun,
  createMockChatResponse,
  createRunId,
  fetchRunEvidenceGraph,
  sendChatMessage,
} from "../api/chatClient";
import type {
  ChatFrontendPayload,
  ChatGraphArtifact,
  GraphResponse,
  ResearchStep,
  ResourceUsage,
  RunStatus,
  WorkspaceChatMessage,
} from "../types";
import { MarkdownContent } from "./MarkdownContent";
import { ResourceUsagePanel } from "./ResourceUsagePanel";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  runId?: string;
  status?: RunStatus;
  researchSteps?: ResearchStep[];
  frontendPayload?: ChatFrontendPayload;
  graphArtifact?: ChatGraphArtifact;
  resourceUsage?: ResourceUsage;
  isMock?: boolean;
};

type AgentChatPanelProps = {
  workspaceId: string;
  accessToken: string;
  initialConversationId?: string | null;
  initialMessages?: WorkspaceChatMessage[];
  onConversationResolved?: (conversationId: string) => void;
  onApplyEvidenceGraph?: (graph: GraphResponse, sourceRunId: string) => void;
  onApplyGraphPayload?: (payload: ChatFrontendPayload) => void;
};

function formatStepDuration(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Not reported";
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 1 : 2)} s`;
  return `${Math.round(value)} ms`;
}

function formatResearchParameters(parameters?: Record<string, unknown>) {
  return parameters && Object.keys(parameters).length ? JSON.stringify(parameters, null, 2) : "";
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

export function AgentChatPanel({
  workspaceId,
  accessToken,
  initialConversationId,
  initialMessages = [],
  onConversationResolved,
  onApplyEvidenceGraph,
  onApplyGraphPayload,
}: AgentChatPanelProps) {
  const pendingInitialMessageRef = useRef(
    initialMessages.find((message) => message.role === "user" && message.pendingSend)?.content ?? null,
  );
  const initialVisibleMessages = initialMessages.filter((message) => !message.pendingSend);
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    const restoredMessages = initialVisibleMessages.map((message, index) => ({
      id: `initial_${index}`,
      role: message.role,
      content: message.content,
      runId: message.runId ?? message.graphArtifact?.runId,
      graphArtifact: message.graphArtifact ?? undefined,
      researchSteps: message.researchSteps ?? [],
    }));

    if (restoredMessages.length) {
      return restoredMessages;
    }

    return [
      {
        id: "welcome",
        role: "assistant",
        content:
          "Ask me to search, compare, or explain papers in this graph. I can summarize tool calls while graph operations use the dedicated graph API.",
      },
    ];
  });
  const [conversationId, setConversationId] = useState<string | undefined>(initialConversationId ?? undefined);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUserMessage, setLastUserMessage] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const activeRunRef = useRef<{ runId: string; controller: AbortController } | null>(null);
  const cancelRequestedRef = useRef(false);
  const [isPinnedToBottom, setIsPinnedToBottom] = useState(true);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [evidenceGraphLoadingRunId, setEvidenceGraphLoadingRunId] = useState<string | null>(null);
  const [evidenceGraphFeedback, setEvidenceGraphFeedback] = useState<Record<string, string>>({});

  useEffect(() => {
    const pendingMessage = pendingInitialMessageRef.current;
    if (!pendingMessage) return;

    pendingInitialMessageRef.current = null;
    void submitMessage(pendingMessage);
  }, []);

  const hasGraphPayload = useMemo(
    () =>
      Boolean(onApplyGraphPayload) &&
      messages.some(
        (message) =>
          message.frontendPayload?.type === "graph" &&
          message.frontendPayload.papers?.length &&
          message.frontendPayload.edges?.length,
      ),
    [messages, onApplyGraphPayload],
  );

  const scrollToBottom = (behavior: ScrollBehavior = "smooth") => {
    messagesEndRef.current?.scrollIntoView({ behavior, block: "end" });
    setIsPinnedToBottom(true);
  };

  useEffect(() => {
    if (isPinnedToBottom) {
      scrollToBottom("smooth");
    }
  }, [messages, isLoading, isPinnedToBottom]);

  const updateScrollPin = () => {
    const element = messagesRef.current;
    if (!element) return;

    const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    setIsPinnedToBottom(distanceToBottom < 80);
  };

  const cancelActiveRun = async () => {
    const activeRun = activeRunRef.current;
    if (!activeRun) return;

    cancelRequestedRef.current = true;
    activeRun.controller.abort();
    setActiveRunId(null);
    setIsLoading(false);
    setMessages((current) =>
      current.map((message) =>
        message.runId === activeRun.runId && message.role === "user"
          ? { ...message, status: "cancelled" }
          : message,
      ),
    );

    try {
      const cancelResponse = await cancelChatRun(activeRun.runId);
      if (cancelResponse.status === "completed") {
        setError("Run already completed before the cancel request reached the backend.");
      } else if (cancelResponse.status === "not_found") {
        setError("Run was cancelled locally, but the backend did not find that run.");
      }
    } catch (cancelError) {
      setError(
        cancelError instanceof Error
          ? `${cancelError.message}; run cancelled locally`
          : "Run cancelled locally; backend cancel request failed",
      );
    } finally {
      if (activeRunRef.current?.runId === activeRun.runId) {
        activeRunRef.current = null;
      }
      inputRef.current?.focus();
    }
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && activeRunRef.current) {
        event.preventDefault();
        void cancelActiveRun();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const submitMessage = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || isLoading) return;

    const runId = createRunId();
    const controller = new AbortController();
    activeRunRef.current = { runId, controller };
    cancelRequestedRef.current = false;
    setIsLoading(true);
    setError(null);
    setLastUserMessage(trimmed);
    setActiveRunId(runId);
    setIsPinnedToBottom(true);

    const userMessage: ChatMessage = {
      id: `user_${runId}`,
      role: "user",
      content: trimmed,
      runId,
    };

    setMessages((current) => [...current, userMessage]);

    try {
      const response = await sendChatMessage({
        workspaceId,
        conversationId,
        runId,
        message: trimmed,
        includeTrace: true,
      }, controller.signal, accessToken);

      if (cancelRequestedRef.current || response.status === "cancelled") {
        setMessages((current) =>
          current.map((message) =>
            message.runId === runId && message.role === "user"
              ? { ...message, status: "cancelled" }
              : message,
          ),
        );
        return;
      }

      if (response.conversationId !== conversationId) {
        onConversationResolved?.(response.conversationId);
      }
      setConversationId(response.conversationId);
      setMessages((current) => [
        ...current,
        {
          id: `assistant_${response.runId}`,
          role: "assistant",
          content: response.message,
          runId: response.runId,
          status: response.status,
          researchSteps: response.researchSteps,
          frontendPayload: response.frontendPayload,
          graphArtifact: response.graphArtifact,
          resourceUsage: response.resourceUsage,
          isMock: response.isMock,
        },
      ]);
    } catch (requestError) {
      if (isAbortError(requestError) || cancelRequestedRef.current) {
        return;
      }

      const requestMessage = requestError instanceof Error ? requestError.message : "Backend unavailable";

      if (DISABLE_CHAT_MOCK_FALLBACK) {
        setError(`${requestMessage}. Mock fallback disabled by VITE_DISABLE_CHAT_MOCK_FALLBACK=true.`);
        return;
      }

      const mockResponse = createMockChatResponse(trimmed, conversationId, runId);
      setConversationId(mockResponse.conversationId);
      setError(`${requestMessage}; showing local mock fallback`);
      setMessages((current) => [
        ...current,
        {
          id: `assistant_mock_${Date.now()}`,
          role: "assistant",
          content: mockResponse.message,
          runId: mockResponse.runId,
          status: mockResponse.status,
          researchSteps: mockResponse.researchSteps,
          frontendPayload: mockResponse.frontendPayload,
          isMock: true,
        },
      ]);
    } finally {
      if (activeRunRef.current?.runId === runId) {
        activeRunRef.current = null;
      }
      setActiveRunId((current) => (current === runId ? null : current));
      setIsLoading(false);
      inputRef.current?.focus();
    }
  };

  const sendCurrentInput = () => {
    if (isLoading || !input.trim()) {
      return;
    }

    const text = input;
    setInput("");
    void submitMessage(text);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    sendCurrentInput();
  };

  const retryLastMessage = () => {
    if (!lastUserMessage) return;
    setError(null);
    void submitMessage(lastUserMessage);
  };

  const applyEvidenceGraph = async (message: ChatMessage) => {
    if (!message.runId || evidenceGraphLoadingRunId || !onApplyEvidenceGraph) return;

    const runId = message.graphArtifact?.runId ?? message.runId;
    const controller = new AbortController();
    setEvidenceGraphLoadingRunId(runId);
    setEvidenceGraphFeedback((current) => {
      const next = { ...current };
      delete next[runId];
      return next;
    });

    try {
      const response = await fetchRunEvidenceGraph(accessToken, runId, {
        workspaceId,
        forceRefresh: false,
        maxNodes: 80,
        // Keep the rich evidence graph: the backend now supplies a layered
        // retrieval overview, while authors and institutions remain available
        // as entity evidence around the representative papers.
        includeAuthors: true,
        includeInstitutions: true,
        includeCitationEdges: true,
      }, controller.signal);

      if (response.status === "completed" && response.graph) {
        const warningText = [
          ...(response.warnings ?? []),
          ...(response.missingReportEntities?.length
            ? [`Missing report entities: ${response.missingReportEntities.join(", ")}`]
            : []),
        ].join(" ");

        if (warningText) {
          setEvidenceGraphFeedback((current) => ({ ...current, [runId]: warningText }));
        }
        onApplyEvidenceGraph(response.graph, runId);
        return;
      }

      const feedback = [
        response.status === "unavailable"
          ? "Evidence graph is unavailable for this report."
          : "Evidence graph generation failed.",
        ...(response.warnings ?? []),
        ...(response.missingReportEntities?.length
          ? [`Missing report entities: ${response.missingReportEntities.join(", ")}`]
          : []),
      ].join(" ");
      setEvidenceGraphFeedback((current) => ({ ...current, [runId]: feedback }));
    } catch (graphError) {
      setEvidenceGraphFeedback((current) => ({
        ...current,
        [runId]: graphError instanceof Error ? graphError.message : "Evidence graph request failed",
      }));
    } finally {
      setEvidenceGraphLoadingRunId((current) => (current === runId ? null : current));
    }
  };

  const statusClass = error ? (DISABLE_CHAT_MOCK_FALLBACK ? "error" : "warning") : "ready";
  const statusLabel = error ? (DISABLE_CHAT_MOCK_FALLBACK ? "Error" : "Mock fallback") : "Ready";

  return (
    <aside className="agent-chat-panel" aria-label="Agent chat">
      <div className="agent-header">
        <div className="agent-title">
          <div className="agent-mark">
            <Sparkles size={18} />
          </div>
          <div>
            <h2>Scholar Agent</h2>
            <p>POST /api/chat via {API_BASE_URL}</p>
          </div>
        </div>
        <span className={`agent-status ${statusClass}`}>
          {error ? <AlertCircle size={14} /> : <CheckCircle2 size={14} />}
          {statusLabel}
        </span>
      </div>

      {error ? (
        <div className="chat-error" role="status">
          <span>{error}</span>
          <button type="button" onClick={retryLastMessage} disabled={!lastUserMessage || isLoading}>
            <RefreshCcw size={14} />
            Retry
          </button>
        </div>
      ) : null}

      <div
        className="chat-messages"
        aria-live="polite"
        ref={messagesRef}
        onScroll={updateScrollPin}
      >
        {messages.map((message) => (
          <article
            className={`chat-message ${message.role}${message.status === "cancelled" ? " cancelled" : ""}`}
            key={message.id}
          >
            <div className="message-avatar" aria-hidden="true">
              {message.role === "assistant" ? <Bot size={16} /> : <UserRound size={16} />}
            </div>
            <div className="message-body">
              <div className="message-meta">
                <strong>{message.role === "assistant" ? "Agent" : "You"}</strong>
                {message.isMock ? <span>mock</span> : null}
                {message.status === "cancelled" ? <span>cancelled</span> : null}
                {message.status === "failed" ? <span>failed</span> : null}
              </div>
              <MarkdownContent content={message.content} />


              {message.researchSteps?.length ? (
                <section className="research-process-panel" aria-label="Execution Timeline">
                  <div className="research-process-heading">
                    <div>
                      <strong>执行时间线</strong>
                      <span>Execution Timeline</span>
                    </div>
                    <span>{message.researchSteps.length} steps</span>
                  </div>
                  <div className="research-step-list">
                    {message.researchSteps.map((step) => {
                      const parameters = formatResearchParameters(step.parameters);
                      return (
                        <details className="research-step" key={step.id}>
                          <summary>
                            <span className="research-step-icon" aria-hidden="true">
                              <Database size={13} />
                            </span>
                            <span className="research-step-title">
                              <strong>{step.publicName}</strong>
                              <small>
                                Round {step.round} · {step.resultSummary ?? "Operation completed"}
                              </small>
                            </span>
                            <span
                              className={
                                "research-step-status research-step-status-" + step.status
                              }
                            >
                              {step.status}
                            </span>
                            <span className="research-step-duration">
                              {formatStepDuration(step.toolLatencyMs)}
                            </span>
                          </summary>
                          <div className="research-step-body">
                            {parameters ? (
                              <div className="research-step-detail">
                                <span>Parameters</span>
                                <pre><code>{parameters}</code></pre>
                              </div>
                            ) : null}
                            {step.queryText ? (
                              <div className="research-step-detail">
                                <div className="research-query-heading">
                                  <span>SQL</span>
                                  <button
                                    type="button"
                                    className="research-copy-button"
                                    title="Copy SQL"
                                    aria-label="Copy SQL"
                                    onClick={() => void copyText(step.queryText ?? "")}
                                  >
                                    <Copy size={13} />
                                  </button>
                                </div>
                                <pre className="research-query-text">
                                  <code>{step.queryText}</code>
                                </pre>
                              </div>
                            ) : null}
                          </div>
                        </details>
                      );
                    })}
                  </div>
                </section>
              ) : null}

              {message.resourceUsage?.shouldDisplay === true ? (
                <ResourceUsagePanel usage={message.resourceUsage} />
              ) : null}

              {message.role === "assistant" &&
              message.runId &&
              !message.isMock &&
              message.graphArtifact?.status === "available" &&
              onApplyEvidenceGraph ? (
                <div className="evidence-graph-card">
                  <div>
                    <strong>{message.graphArtifact.title ?? "Citation evidence graph"}</strong>
                    <span>
                      {(message.graphArtifact.nodePreviewCount ?? 0)} nodes · {" "}
                      {(message.graphArtifact.edgePreviewCount ?? 0)} edges
                    </span>
                    {message.graphArtifact.warnings?.length ? (
                      <p>{message.graphArtifact.warnings.join(" ")}</p>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => void applyEvidenceGraph(message)}
                    disabled={evidenceGraphLoadingRunId === (message.graphArtifact.runId ?? message.runId)}
                  >
                    {evidenceGraphLoadingRunId === (message.graphArtifact.runId ?? message.runId) ? (
                      <Loader2 size={14} />
                    ) : (
                      <Network size={14} />
                    )}
                    Evidence Graph
                  </button>
                  {evidenceGraphFeedback[message.graphArtifact.runId ?? message.runId] ? (
                    <p className="evidence-graph-feedback">
                      {evidenceGraphFeedback[message.graphArtifact.runId ?? message.runId]}
                    </p>
                  ) : null}
                </div>
              ) : null}

              {onApplyGraphPayload && message.frontendPayload?.type === "graph" ? (
                <div className="graph-payload-card">
                  <span>
                    Graph payload: {message.frontendPayload.papers?.length ?? 0} papers,{" "}
                    {message.frontendPayload.edges?.length ?? 0} edges
                  </span>
                  <button
                    type="button"
                    onClick={() => onApplyGraphPayload(message.frontendPayload!)}
                    disabled={
                      !message.frontendPayload.papers?.length ||
                      !message.frontendPayload.edges?.length
                    }
                  >
                    Apply to graph
                  </button>
                </div>
              ) : null}
            </div>
          </article>
        ))}

        {isLoading ? (
          <div className="chat-loading">
            <Loader2 size={16} />
            Thinking with tools{activeRunId ? ` · ${activeRunId.slice(0, 8)}` : ""}...
          </div>
        ) : null}
        <div ref={messagesEndRef} />
      </div>

      {!isPinnedToBottom ? (
        <button
          className="scroll-bottom-button"
          type="button"
          onClick={() => scrollToBottom("smooth")}
        >
          <ChevronDown size={16} />
          Latest
        </button>
      ) : null}

      <form className="chat-input-form" onSubmit={handleSubmit}>
        {hasGraphPayload ? <span className="payload-hint">Graph payload available above</span> : null}
        <textarea
          ref={inputRef}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Ask for related work, citation paths, or evidence..."
          rows={3}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              sendCurrentInput();
            }
          }}
        />
        {isLoading ? (
          <button className="cancel-run-button" type="button" onClick={() => void cancelActiveRun()}>
            <XCircle size={16} />
            Cancel
          </button>
        ) : null}
        <button type="submit" disabled={isLoading || !input.trim()}>
          {isLoading ? <Loader2 size={16} /> : <Send size={16} />}
          Send
        </button>
      </form>
    </aside>
  );
}

async function copyText(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}
