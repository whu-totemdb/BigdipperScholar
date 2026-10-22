import { FormEvent, useEffect, useState } from "react";
import { Clock3, Loader2, LogOut, MessageSquareText, Network, Search, Send, Trash2, UserRound } from "lucide-react";
import type { AuthSession, WorkspaceDetail, WorkspaceEntryType, WorkspaceSummary } from "../types";
import { createWorkspace, deleteWorkspace, listWorkspaces, logoutUser } from "../api/authHomeClient";

type HomePageProps = {
  session: AuthSession;
  onLogout: () => void;
  onOpenWorkspace: (workspaceId: string) => void;
  onWorkspaceCreated: (detail: WorkspaceDetail) => void;
};

function entryLabel(entryType: WorkspaceEntryType) {
  return entryType === "paper_search" ? "Paper search" : "Agent chat";
}

export function HomePage({ session, onLogout, onOpenWorkspace, onWorkspaceCreated }: HomePageProps) {
  const [mode, setMode] = useState<WorkspaceEntryType>("paper_search");
  const [input, setInput] = useState("");
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const refreshWorkspaces = async () => {
    try {
      const response = await listWorkspaces(session.accessToken);
      setWorkspaces(response.items);
    } catch (workspaceError) {
      setError(workspaceError instanceof Error ? workspaceError.message : "Failed to load workspaces");
    }
  };

  useEffect(() => {
    void refreshWorkspaces();
  }, [session.accessToken]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || isLoading) return;

    setIsLoading(true);
    setError(null);
    try {
      const detail = await createWorkspace(session.accessToken, mode === "paper_search"
        ? { entryType: "paper_search", query: trimmed, limit: 10 }
        : { entryType: "agent_chat", message: trimmed });
      onWorkspaceCreated(detail);
    } catch (workspaceError) {
      setError(workspaceError instanceof Error ? workspaceError.message : "Failed to create workspace");
    } finally {
      setIsLoading(false);
    }
  };

  const logout = async () => {
    await logoutUser(session.accessToken);
    onLogout();
  };

  const removeWorkspace = async (workspaceId: string) => {
    if (deletingId) return;

    setDeletingId(workspaceId);
    setError(null);
    try {
      await deleteWorkspace(session.accessToken, workspaceId);
      setWorkspaces((current) => current.filter((workspace) => workspace.id !== workspaceId));
    } catch (workspaceError) {
      setError(workspaceError instanceof Error ? workspaceError.message : "Failed to delete workspace");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <main className="home-page">
      <header className="home-topbar">
        <div>
          <strong>BigDipper Scholar</strong>
          <span>Research workspaces</span>
        </div>
        <div className="home-user">
          <UserRound size={17} />
          <span>{session.user.displayName}</span>
          <button type="button" onClick={logout} aria-label="Logout"><LogOut size={16} /></button>
        </div>
      </header>

      <section className="home-search-zone">
        <div className="home-search-copy">
          <h1>BigDipper Scholar</h1>
          <p>Search papers or start an Agent conversation, then continue from your recent workspaces.</p>
        </div>

        <form className="home-search" onSubmit={submit}>
          <div className="home-mode-toggle" aria-label="Home entry mode">
            <button className={mode === "paper_search" ? "active" : ""} type="button" onClick={() => setMode("paper_search")}>
              <Search size={16} /> Paper title
            </button>
            <button className={mode === "agent_chat" ? "active" : ""} type="button" onClick={() => setMode("agent_chat")}>
              <MessageSquareText size={16} /> Chat with Agent
            </button>
          </div>
          <div className="home-input-row">
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={mode === "paper_search" ? "Search by paper title" : "Ask the Scholar Agent"}
            />
            <button type="submit" disabled={isLoading} aria-busy={isLoading}>
              {isLoading ? <Loader2 className="spin" size={18} /> : mode === "paper_search" ? <Search size={18} /> : <Send size={18} />}
            </button>
          </div>
        </form>

        {error ? <p className="home-error">{error}</p> : null}
      </section>

      <section className="workspace-history">
        <div className="history-header">
          <div>
            <h2>Recent workspaces</h2>
            <p>Resume paper searches and Agent conversations.</p>
          </div>
          <Clock3 size={20} />
        </div>

        {workspaces.length ? (
          <div className="history-grid">
            {workspaces.map((workspace) => (
              <article className="history-card" key={workspace.id}>
                <button className="history-card-main" type="button" onClick={() => onOpenWorkspace(workspace.id)}>
                  <span>{entryLabel(workspace.entryType)}</span>
                  <strong>{workspace.title}</strong>
                  <p>{workspace.preview}</p>
                  <small>{new Date(workspace.updatedAt).toLocaleString()}</small>
                </button>
                <button
                  className="history-delete"
                  type="button"
                  onClick={() => removeWorkspace(workspace.id)}
                  disabled={deletingId === workspace.id}
                  aria-label={`Delete ${workspace.title}`}
                >
                  <Trash2 size={15} />
                </button>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-history">
            <Network size={28} />
            <p>No workspaces yet. Start with a paper title or an Agent question.</p>
          </div>
        )}
      </section>
    </main>
  );
}
