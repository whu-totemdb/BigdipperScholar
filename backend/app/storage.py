from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    AuthUser,
    ChatSeedMessage,
    ConversationResponse,
    GraphNode,
    GraphResponse,
    GraphSnapshot,
    GraphStateSummary,
    PaperSearchItem,
    MessageOut,
    StoredToolCall,
    ToolCall,
    WorkspaceSummary,
)
from app.resource_usage import finish_resource_span, start_resource_span


DEFAULT_DB_PATH = "bigdipper_scholar.db"


class _InstrumentedSQLiteConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        span = start_resource_span(_sqlite_operation_name(sql), "database", backend="sqlite")
        try:
            cursor = super().execute(sql, parameters)
        except Exception:
            finish_resource_span(span, status="failed")
            raise
        finish_resource_span(span, status="success")
        return cursor

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> sqlite3.Cursor:
        span = start_resource_span(_sqlite_operation_name(sql), "database", backend="sqlite")
        try:
            cursor = super().executemany(sql, seq_of_parameters)
        except Exception:
            finish_resource_span(span, status="failed")
            raise
        finish_resource_span(span, status="success")
        return cursor

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        span = start_resource_span("SQLite script", "database", backend="sqlite")
        try:
            cursor = super().executescript(sql_script)
        except Exception:
            finish_resource_span(span, status="failed")
            raise
        finish_resource_span(span, status="success")
        return cursor


class SQLiteStorage:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path or os.getenv("BIGDIPPER_DB_PATH", DEFAULT_DB_PATH))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, factory=_InstrumentedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    entry_type TEXT NOT NULL CHECK(entry_type IN ('paper_search', 'agent_chat')),
                    title TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    conversation_id TEXT,
                    active_graph_state_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS graph_states (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    query_context TEXT NOT NULL,
                    seed_node_id TEXT,
                    nodes_json TEXT NOT NULL,
                    edges_json TEXT NOT NULL,
                    papers_json TEXT NOT NULL DEFAULT '[]',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    default_view_mode TEXT NOT NULL DEFAULT 'graph',
                    seed_paper_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );

                CREATE TABLE IF NOT EXISTS workspace_seed_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    pending_send INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );


                CREATE TABLE IF NOT EXISTS agent_run_artifacts (
                    run_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    workspace_id TEXT,
                    route TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_answer TEXT NOT NULL,
                    research_report TEXT,
                    evidence_bundle_json TEXT NOT NULL DEFAULT '{}',
                    research_response_json TEXT NOT NULL DEFAULT '{}',
                    selected_observations_json TEXT NOT NULL DEFAULT '[]',
                    events_log_path TEXT,
                    graph_artifact_json TEXT NOT NULL DEFAULT '{}',
                    graph_response_json TEXT,
                    graph_state_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS paper_kg_sources (
                    paperid TEXT NOT NULL,
                    corpus_id TEXT NOT NULL UNIQUE,
                    dataset TEXT NOT NULL,
                    venue TEXT,
                    source_path TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    raw_record TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS paper_semantic_graphs (
                    paperid TEXT PRIMARY KEY,
                    corpus_id TEXT NOT NULL,
                    annotation_type TEXT,
                    language TEXT,
                    schema_source TEXT,
                    evidence_sentence TEXT,
                    raw_annotation TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_semantic_entities (
                    paperid TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    entity_name TEXT NOT NULL,
                    entity_type TEXT,
                    entity_comment TEXT,
                    entity_source TEXT,
                    raw TEXT NOT NULL,
                    PRIMARY KEY (paperid, entity_id)
                );

                CREATE TABLE IF NOT EXISTS paper_semantic_relations (
                    paperid TEXT NOT NULL,
                    relation_id TEXT NOT NULL,
                    relation_name TEXT NOT NULL,
                    head_entity_id TEXT NOT NULL,
                    tail_entity_id TEXT NOT NULL,
                    raw TEXT NOT NULL,
                    PRIMARY KEY (paperid, relation_id)
                );

                CREATE TABLE IF NOT EXISTS paper_semantic_properties (
                    paperid TEXT NOT NULL,
                    property_id TEXT NOT NULL,
                    property_name TEXT NOT NULL,
                    property_type TEXT,
                    property_value TEXT,
                    entity_id TEXT,
                    raw TEXT NOT NULL,
                    PRIMARY KEY (paperid, property_id)
                );

                CREATE TABLE IF NOT EXISTS global_semantic_graph_builds (
                    build_id TEXT PRIMARY KEY,
                    dataset_scope TEXT NOT NULL,
                    normalization_version TEXT NOT NULL,
                    min_paper_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    stats TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS global_semantic_entities (
                    dataset_scope TEXT NOT NULL,
                    global_entity_id TEXT NOT NULL,
                    name_group_id TEXT NOT NULL,
                    normalization_version TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    canonical_entity_type TEXT NOT NULL,
                    normalized_entity_type TEXT NOT NULL,
                    type_distribution TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (dataset_scope, global_entity_id),
                    UNIQUE (dataset_scope, normalization_version, normalized_name, normalized_entity_type)
                );

                CREATE INDEX IF NOT EXISTS idx_global_semantic_entities_name
                    ON global_semantic_entities (dataset_scope, normalized_name);

                CREATE INDEX IF NOT EXISTS idx_global_semantic_entities_type
                    ON global_semantic_entities (dataset_scope, normalized_entity_type);

                CREATE TABLE IF NOT EXISTS global_semantic_entity_mentions (
                    dataset_scope TEXT NOT NULL,
                    paperid TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    global_entity_id TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    dataset_group TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    original_entity_type TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    PRIMARY KEY (dataset_scope, paperid, entity_id)
                );

                CREATE INDEX IF NOT EXISTS idx_global_semantic_mentions_entity
                    ON global_semantic_entity_mentions (dataset_scope, global_entity_id, paperid);

                CREATE TABLE IF NOT EXISTS global_semantic_entity_stats (
                    dataset_scope TEXT NOT NULL,
                    global_entity_id TEXT NOT NULL,
                    paper_count INTEGER NOT NULL,
                    mention_count INTEGER NOT NULL,
                    datasets TEXT NOT NULL,
                    example_papers TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    build_id TEXT NOT NULL,
                    PRIMARY KEY (dataset_scope, global_entity_id)
                );

                CREATE INDEX IF NOT EXISTS idx_global_semantic_entity_stats_rank
                    ON global_semantic_entity_stats (dataset_scope, paper_count, global_entity_id);

                CREATE TABLE IF NOT EXISTS global_semantic_edges (
                    dataset_scope TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    source_global_entity_id TEXT NOT NULL,
                    target_global_entity_id TEXT NOT NULL,
                    relation_name TEXT,
                    normalized_relation_name TEXT,
                    directed INTEGER NOT NULL,
                    paper_count INTEGER NOT NULL,
                    mention_count INTEGER NOT NULL,
                    evidence_papers TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    PRIMARY KEY (dataset_scope, edge_id)
                );

                CREATE INDEX IF NOT EXISTS idx_global_semantic_edges_source
                    ON global_semantic_edges
                    (dataset_scope, source_global_entity_id, edge_type, paper_count);

                CREATE INDEX IF NOT EXISTS idx_global_semantic_edges_target
                    ON global_semantic_edges
                    (dataset_scope, target_global_entity_id, edge_type, paper_count);
                """
            )
            _ensure_column(connection, "graph_states", "papers_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(connection, "graph_states", "warnings_json", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(connection, "graph_states", "default_view_mode", "TEXT NOT NULL DEFAULT 'graph'")
            _ensure_column(connection, "graph_states", "seed_paper_id", "TEXT")
            self._backfill_workspace_conversations(connection)

    def _backfill_workspace_conversations(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, created_at, updated_at
            FROM workspaces
            WHERE conversation_id IS NULL
            """
        ).fetchall()
        for row in rows:
            conversation_id = str(uuid.uuid4())
            created_at = row["created_at"] or _utc_now()
            updated_at = row["updated_at"] or created_at
            connection.execute(
                """
                INSERT INTO conversations (id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (conversation_id, created_at, updated_at),
            )
            connection.execute(
                "UPDATE workspaces SET conversation_id = ? WHERE id = ?",
                (conversation_id, row["id"]),
            )

    def ensure_conversation(self, conversation_id: str | None = None) -> str:
        now = _utc_now()
        resolved_id = conversation_id or str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (resolved_id, now, now),
            )
        return resolved_id

    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (conversation_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, role, content, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def add_tool_calls(self, conversation_id: str, tool_calls: list[ToolCall]) -> None:
        if not tool_calls:
            return

        now = _utc_now()
        rows = [
            (
                conversation_id,
                tool_call.name,
                json.dumps(tool_call.arguments, ensure_ascii=True),
                tool_call.summary,
                now,
            )
            for tool_call in tool_calls
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO tool_calls
                    (conversation_id, name, arguments_json, summary, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def get_messages(self, conversation_id: str) -> list[MessageOut]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()

        return [
            MessageOut(
                id=row["id"],
                role=row["role"],
                content=row["content"],
                createdAt=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def get_conversation(self, conversation_id: str) -> ConversationResponse | None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            tool_rows = connection.execute(
                """
                SELECT id, name, arguments_json, summary, created_at
                FROM tool_calls
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()

        if not exists:
            return None

        tool_calls = [
            StoredToolCall(
                id=row["id"],
                name=row["name"],
                arguments=json.loads(row["arguments_json"]),
                summary=row["summary"],
                createdAt=_parse_datetime(row["created_at"]),
            )
            for row in tool_rows
        ]
        return ConversationResponse(
            conversationId=conversation_id,
            messages=self.get_messages(conversation_id),
            toolCalls=tool_calls,
        )


    def create_user(self, email: str, password_hash: str, display_name: str) -> AuthUser:
        now = _utc_now()
        user_id = f"user_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, email, password_hash, display_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, _normalize_email(email), password_hash, display_name.strip(), now),
            )
        return AuthUser(id=user_id, email=_normalize_email(email), displayName=display_name.strip(), createdAt=_parse_datetime(now))

    def get_user_auth_record_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, email, password_hash, display_name, created_at
                FROM users
                WHERE email = ?
                """,
                (_normalize_email(email),),
            ).fetchone()
        return dict(row) if row else None

    def create_session(self, user_id: str, token: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (token, user_id, created_at, revoked_at)
                VALUES (?, ?, ?, NULL)
                """,
                (token, user_id, now),
            )

    def get_user_by_token(self, token: str) -> AuthUser | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.id, u.email, u.display_name, u.created_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.revoked_at IS NULL
                """,
                (token,),
            ).fetchone()
        return _user_from_row(row) if row else None

    def revoke_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token = ?",
                (_utc_now(), token),
            )

    def list_workspaces(self, user_id: str, limit: int = 20, cursor: str | None = None) -> tuple[list[WorkspaceSummary], str | None]:
        safe_limit = min(max(limit, 1), 50)
        params: list[Any] = [user_id]
        cursor_clause = ""
        if cursor:
            cursor_clause = "AND updated_at < ?"
            params.append(cursor)
        params.append(safe_limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, entry_type, title, preview, conversation_id, active_graph_state_id, created_at, updated_at
                FROM workspaces
                WHERE user_id = ? {cursor_clause}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        visible_rows = rows[:safe_limit]
        next_cursor = visible_rows[-1]["updated_at"] if len(rows) > safe_limit and visible_rows else None
        return [_workspace_from_row(row) for row in visible_rows], next_cursor

    def create_paper_workspace(
        self,
        user_id: str,
        query: str,
        nodes: list[GraphNode],
        edges: list[dict[str, Any]] | None = None,
        paper_count: int | None = None,
        papers: list[Any] | None = None,
        warnings: list[str] | None = None,
        default_view_mode: str = "list",
        seed_paper_id: str | None = None,
    ) -> tuple[WorkspaceSummary, GraphSnapshot]:
        now = _utc_now()
        workspace_id = f"ws_{uuid.uuid4().hex}"
        graph_id = f"gs_{uuid.uuid4().hex}"
        conversation_id = str(uuid.uuid4())
        title = _short_title(query)
        graph_edges = edges or []
        paper_payload = [paper.model_dump(by_alias=True) if hasattr(paper, "model_dump") else paper for paper in (papers or [])]
        result_count = paper_count if paper_count is not None else len([node for node in nodes if node.node_type == "paper"])
        preview = f"{result_count} paper candidates"
        graph = GraphSnapshot(
            graphId=graph_id,
            title=f"Search: {title}",
            queryContext="Paper title search",
            seedNodeId=None,
            seedPaperId=seed_paper_id,
            defaultViewMode=default_view_mode,
            nodes=nodes,
            papers=[PaperSearchItem.model_validate(item) for item in paper_payload],
            edges=graph_edges,
            warnings=warnings or [],
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (conversation_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO workspaces
                    (id, user_id, entry_type, title, preview, conversation_id, active_graph_state_id, created_at, updated_at)
                VALUES (?, ?, 'paper_search', ?, ?, ?, ?, ?, ?)
                """,
                (workspace_id, user_id, title, preview, conversation_id, graph_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO graph_states
                    (id, workspace_id, title, query_context, seed_node_id, nodes_json, edges_json,
                     papers_json, warnings_json, default_view_mode, seed_paper_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    workspace_id,
                    graph.title,
                    graph.query_context,
                    graph.seed_node_id,
                    json.dumps([node.model_dump(by_alias=True) for node in nodes], ensure_ascii=False),
                    json.dumps(graph_edges, ensure_ascii=False),
                    json.dumps(paper_payload, ensure_ascii=False),
                    json.dumps(warnings or [], ensure_ascii=False),
                    default_view_mode,
                    seed_paper_id,
                    now,
                    now,
                ),
            )
        workspace = WorkspaceSummary(
            id=workspace_id,
            entryType="paper_search",
            title=title,
            preview=preview,
            conversationId=conversation_id,
            activeGraphStateId=graph_id,
            createdAt=_parse_datetime(now),
            updatedAt=_parse_datetime(now),
        )
        return workspace, graph

    def create_agent_workspace(self, user_id: str, message: str) -> tuple[WorkspaceSummary, GraphSnapshot, list[ChatSeedMessage]]:
        now = _utc_now()
        workspace_id = f"ws_{uuid.uuid4().hex}"
        conversation_id = self.ensure_conversation()
        title = _short_title(message)
        graph = GraphSnapshot(
            graphId=None,
            title="Empty graph",
            queryContext="Waiting for Agent graph result",
            seedNodeId=None,
            nodes=[],
            edges=[],
        )
        seed_message = ChatSeedMessage(
            role="user",
            content=message,
            createdAt=_parse_datetime(now),
            pendingSend=True,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspaces
                    (id, user_id, entry_type, title, preview, conversation_id, active_graph_state_id, created_at, updated_at)
                VALUES (?, ?, 'agent_chat', ?, 'Agent conversation', ?, NULL, ?, ?)
                """,
                (workspace_id, user_id, title, conversation_id, now, now),
            )
        workspace = WorkspaceSummary(
            id=workspace_id,
            entryType="agent_chat",
            title=title,
            preview="Agent conversation",
            conversationId=conversation_id,
            activeGraphStateId=None,
            createdAt=_parse_datetime(now),
            updatedAt=_parse_datetime(now),
        )
        return workspace, graph, [seed_message]

    def get_workspace(self, user_id: str, workspace_id: str) -> WorkspaceSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, entry_type, title, preview, conversation_id, active_graph_state_id, created_at, updated_at
                FROM workspaces
                WHERE id = ? AND user_id = ?
                """,
                (workspace_id, user_id),
            ).fetchone()
        if row and not row["conversation_id"]:
            self.ensure_workspace_conversation(workspace_id)
            return self.get_workspace(user_id, workspace_id)
        return _workspace_from_row(row) if row else None

    def get_workspace_by_id(self, workspace_id: str) -> WorkspaceSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, entry_type, title, preview, conversation_id, active_graph_state_id, created_at, updated_at
                FROM workspaces
                WHERE id = ?
                """,
                (workspace_id,),
            ).fetchone()
        if row and not row["conversation_id"]:
            self.ensure_workspace_conversation(workspace_id)
            return self.get_workspace_by_id(workspace_id)
        return _workspace_from_row(row) if row else None

    def ensure_workspace_conversation(self, workspace_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                return None
            if row["conversation_id"]:
                return row["conversation_id"]
            now = _utc_now()
            conversation_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO conversations (id, created_at, updated_at) VALUES (?, ?, ?)",
                (conversation_id, now, now),
            )
            connection.execute(
                "UPDATE workspaces SET conversation_id = ?, updated_at = ? WHERE id = ?",
                (conversation_id, now, workspace_id),
            )
        return conversation_id

    def touch_workspace(self, workspace_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE workspaces SET updated_at = ? WHERE id = ?",
                (_utc_now(), workspace_id),
            )

    def get_active_graph(self, workspace_id: str, graph_id: str | None) -> GraphSnapshot | None:
        if not graph_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, query_context, seed_node_id, nodes_json, edges_json,
                       papers_json, warnings_json, default_view_mode, seed_paper_id
                FROM graph_states
                WHERE id = ? AND workspace_id = ?
                """,
                (graph_id, workspace_id),
            ).fetchone()
        if not row:
            return None
        return GraphSnapshot(
            graphId=row["id"],
            title=row["title"],
            queryContext=row["query_context"],
            seedNodeId=row["seed_node_id"],
            seedPaperId=row["seed_paper_id"] or row["seed_node_id"],
            defaultViewMode=row["default_view_mode"] or "graph",
            nodes=[GraphNode.model_validate(item) for item in json.loads(row["nodes_json"])],
            papers=[PaperSearchItem.model_validate(item) for item in json.loads(row["papers_json"] or "[]")],
            edges=json.loads(row["edges_json"]),
            warnings=json.loads(row["warnings_json"] or "[]"),
        )

    def create_graph_state(
        self,
        workspace_id: str,
        title: str,
        query_context: str,
        seed_node_id: str | None,
        nodes: list[GraphNode],
        edges: list[dict[str, Any]],
        papers: list[Any] | None = None,
        warnings: list[str] | None = None,
        default_view_mode: str = "graph",
        seed_paper_id: str | None = None,
    ) -> str:
        now = _utc_now()
        graph_id = f"graph_{uuid.uuid4().hex}"
        paper_payload = [paper.model_dump(by_alias=True) if hasattr(paper, "model_dump") else paper for paper in (papers or [])]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO graph_states
                    (id, workspace_id, title, query_context, seed_node_id, nodes_json, edges_json,
                     papers_json, warnings_json, default_view_mode, seed_paper_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    workspace_id,
                    title,
                    query_context,
                    seed_node_id,
                    json.dumps([node.model_dump(by_alias=True) for node in nodes], ensure_ascii=False),
                    json.dumps(edges, ensure_ascii=False),
                    json.dumps(paper_payload, ensure_ascii=False),
                    json.dumps(warnings or [], ensure_ascii=False),
                    default_view_mode,
                    seed_paper_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE workspaces
                SET active_graph_state_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (graph_id, now, workspace_id),
            )
        return graph_id

    def list_graph_states(self, workspace_id: str) -> list[GraphStateSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, query_context, seed_node_id, created_at
                FROM graph_states
                WHERE workspace_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (workspace_id,),
            ).fetchall()
        return [
            GraphStateSummary(
                id=row["id"],
                title=row["title"],
                queryContext=row["query_context"],
                seedNodeId=row["seed_node_id"],
                createdAt=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def get_graph_response(self, workspace_id: str, graph_id: str) -> GraphResponse | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, query_context, seed_node_id, nodes_json, edges_json,
                       papers_json, warnings_json, default_view_mode, seed_paper_id
                FROM graph_states
                WHERE id = ? AND workspace_id = ?
                """,
                (graph_id, workspace_id),
            ).fetchone()
        if not row:
            return None
        return _graph_response_from_row(workspace_id, row)

    def get_workspace_seed_messages(self, workspace_id: str) -> list[ChatSeedMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content, pending_send, created_at
                FROM workspace_seed_messages
                WHERE workspace_id = ?
                ORDER BY id ASC
                """,
                (workspace_id,),
            ).fetchall()
        return [
            ChatSeedMessage(
                role=row["role"],
                content=row["content"],
                createdAt=_parse_datetime(row["created_at"]),
                pendingSend=bool(row["pending_send"]),
            )
            for row in rows
        ]

    def save_agent_run_artifact(
        self,
        *,
        run_id: str,
        conversation_id: str,
        workspace_id: str | None,
        route: str,
        status: str,
        final_answer: str,
        research_report: str | None = None,
        evidence_bundle: dict[str, Any] | None = None,
        research_response: dict[str, Any] | None = None,
        selected_observations: list[dict[str, Any]] | None = None,
        events_log_path: str | None = None,
        graph_artifact: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_run_artifacts
                    (run_id, conversation_id, workspace_id, route, status, final_answer, research_report,
                     evidence_bundle_json, research_response_json, selected_observations_json,
                     events_log_path, graph_artifact_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    workspace_id = excluded.workspace_id,
                    route = excluded.route,
                    status = excluded.status,
                    final_answer = excluded.final_answer,
                    research_report = excluded.research_report,
                    evidence_bundle_json = excluded.evidence_bundle_json,
                    research_response_json = excluded.research_response_json,
                    selected_observations_json = excluded.selected_observations_json,
                    events_log_path = excluded.events_log_path,
                    graph_artifact_json = excluded.graph_artifact_json,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    conversation_id,
                    workspace_id,
                    route,
                    status,
                    final_answer,
                    research_report,
                    json.dumps(evidence_bundle or {}, ensure_ascii=False),
                    json.dumps(research_response or {}, ensure_ascii=False),
                    json.dumps(selected_observations or [], ensure_ascii=False),
                    events_log_path,
                    json.dumps(graph_artifact or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def get_agent_run_artifact(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, conversation_id, workspace_id, route, status, final_answer, research_report,
                       evidence_bundle_json, research_response_json, selected_observations_json,
                       events_log_path, graph_artifact_json, graph_response_json, graph_state_id,
                       created_at, updated_at
                FROM agent_run_artifacts
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "workspace_id": row["workspace_id"],
            "route": row["route"],
            "status": row["status"],
            "final_answer": row["final_answer"],
            "research_report": row["research_report"],
            "evidence_bundle": _json_loads(row["evidence_bundle_json"], {}),
            "research_response": _json_loads(row["research_response_json"], {}),
            "selected_observations": _json_loads(row["selected_observations_json"], []),
            "events_log_path": row["events_log_path"],
            "graph_artifact": _json_loads(row["graph_artifact_json"], {}),
            "graph_response": _json_loads(row["graph_response_json"], None) if row["graph_response_json"] else None,
            "graph_state_id": row["graph_state_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_agent_run_artifacts_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, final_answer, graph_artifact_json, research_response_json,
                       created_at, updated_at
                FROM agent_run_artifacts
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "final_answer": row["final_answer"],
                "graph_artifact": _json_loads(row["graph_artifact_json"], {}),
                "research_response": _json_loads(row["research_response_json"], {}),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def update_agent_run_graph(
        self,
        run_id: str,
        *,
        graph_response: dict[str, Any],
        graph_state_id: str,
        graph_artifact: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_run_artifacts
                SET graph_response_json = ?, graph_state_id = ?,
                    graph_artifact_json = COALESCE(?, graph_artifact_json),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    json.dumps(graph_response, ensure_ascii=False),
                    graph_state_id,
                    json.dumps(graph_artifact, ensure_ascii=False) if graph_artifact is not None else None,
                    now,
                    run_id,
                ),
            )

    def delete_workspace(self, user_id: str, workspace_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM workspace_seed_messages WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM agent_run_artifacts WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM graph_states WHERE workspace_id = ?", (workspace_id,))
            connection.execute("DELETE FROM workspaces WHERE id = ? AND user_id = ?", (workspace_id, user_id))
        return True


def _json_loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _sqlite_operation_name(sql: str) -> str:
    normalized = " ".join(sql.strip().split())
    operation = normalized.split(" ", 1)[0].upper() if normalized else "QUERY"
    return f"SQLite {operation}"


def _graph_response_from_row(workspace_id: str, row: sqlite3.Row) -> GraphResponse:
    from app.models import PaperSearchItem

    nodes = [GraphNode.model_validate(item) for item in json.loads(row["nodes_json"])]
    papers_payload = json.loads(row["papers_json"] or "[]")
    if not papers_payload:
        papers_payload = [node.model_dump(by_alias=True) for node in nodes if node.node_type == "paper"]
    return GraphResponse(
        graphId=row["id"],
        workspaceId=workspace_id,
        title=row["title"],
        queryContext=row["query_context"],
        seedNodeId=row["seed_node_id"],
        seedPaperId=row["seed_paper_id"] or row["seed_node_id"],
        defaultViewMode=row["default_view_mode"] or "graph",
        nodes=nodes,
        papers=[PaperSearchItem.model_validate(item) for item in papers_payload],
        edges=json.loads(row["edges_json"]),
        warnings=json.loads(row["warnings_json"] or "[]"),
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _short_title(value: str, max_chars: int = 80) -> str:
    title = " ".join(value.split()).strip()
    if not title:
        return "Untitled workspace"
    return title[:max_chars]


def _user_from_row(row: sqlite3.Row) -> AuthUser:
    return AuthUser(
        id=row["id"],
        email=row["email"],
        displayName=row["display_name"],
        createdAt=_parse_datetime(row["created_at"]),
    )


def _workspace_from_row(row: sqlite3.Row) -> WorkspaceSummary:
    return WorkspaceSummary(
        id=row["id"],
        entryType=row["entry_type"],
        title=row["title"],
        preview=row["preview"],
        conversationId=row["conversation_id"],
        activeGraphStateId=row["active_graph_state_id"],
        createdAt=_parse_datetime(row["created_at"]),
        updatedAt=_parse_datetime(row["updated_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
