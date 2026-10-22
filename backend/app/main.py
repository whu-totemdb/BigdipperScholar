from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.agents import create_agent
from app.config import get_env
from app.evidence_graph import EvidenceGraphBuilder
from app.global_semantic_graph import GlobalSemanticGraphService
from app.graph_demo import GraphDemoError, GraphDemoService
from app.llm import DeepSeekClient
from app.logging_config import get_logger, setup_logging
from app.models import (
    AgentEvidenceGraphRequest,
    AgentEvidenceGraphResponse,
    AuthResponse,
    AuthUser,
    CancelRequest,
    CancelResponse,
    ChatRequest,
    ChatResponse,
    ChatSeedMessage,
    ConversationResponse,
    DeleteWorkspaceResponse,
    GraphNode,
    GraphQueryRequest,
    GraphResponse,
    GraphSnapshot,
    GraphStateCreateRequest,
    GraphStateCreateResponse,
    GraphStateListResponse,
    GlobalSemanticDataset,
    GlobalSemanticEntitySearchResponse,
    GlobalSemanticGraphExpandRequest,
    GlobalSemanticGraphExpandResponse,
    GlobalSemanticGraphQueryRequest,
    GlobalSemanticGraphResponse,
    GlobalSemanticOverviewResponse,
    MessageOut,
    LoginRequest,
    LogoutResponse,
    MeResponse,
    PaperDetailResponse,
    PaperSearchItem,
    PaperSearchRequest,
    PaperSearchResponse,
    RegisterRequest,
    ToolSpec,
    WorkspaceCreateRequest,
    WorkspaceCreateResponse,
    WorkspaceDetailResponse,
    WorkspaceListResponse,
)
from app.prompts import PromptProvider
from app.resource_usage import (
    InstrumentedLLMClient,
    InstrumentedToolProvider,
    ResourceUsageCollector,
    resource_usage_session,
)
from app.run_registry import RunCancelledError, RunRegistry
from app.storage import SQLiteStorage
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider, ToolProvider


setup_logging()
logger = get_logger("bigdipper.api")


def _default_llm_client() -> DeepSeekClient:
    model = get_env("BIGDIPPER_AGENT_EXPERIMENT_MODEL") or get_env("DEEPSEEK_MODEL")
    raw_timeout = get_env("BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT", "120") or "120"
    try:
        timeout = max(1.0, float(raw_timeout))
    except ValueError:
        timeout = 120.0
    return DeepSeekClient(model=model, timeout=timeout)


def create_app(
    storage: SQLiteStorage | None = None,
    tool_provider: ToolProvider | None = None,
    llm_client: DeepSeekClient | None = None,
    prompt_provider: PromptProvider | None = None,
    graph_service: GraphDemoService | None = None,
    global_semantic_graph_service: GlobalSemanticGraphService | None = None,
) -> FastAPI:
    resolved_tool_provider = tool_provider or HelmDBToolProvider()
    instrumented_tool_provider = InstrumentedToolProvider(resolved_tool_provider)
    resolved_storage = storage
    resolved_agent = None
    resolved_graph_service: GraphDemoService | None = graph_service
    resolved_global_semantic_graph_service = global_semantic_graph_service
    run_registry = RunRegistry()

    def get_storage() -> SQLiteStorage:
        nonlocal resolved_storage
        if resolved_storage is None:
            resolved_storage = SQLiteStorage()
        return resolved_storage

    def get_agent():
        nonlocal resolved_agent
        if resolved_agent is None:
            instrumented_llm_client = InstrumentedLLMClient(llm_client or _default_llm_client())
            resolved_agent = create_agent(
                instrumented_tool_provider,
                llm_client=instrumented_llm_client,
                prompt_provider=prompt_provider,
            )
        return resolved_agent

    def require_user(authorization: str | None = Header(default=None)) -> AuthUser:
        token = _bearer_token(authorization)
        user = get_storage().get_user_by_token(token)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user


    def get_graph_service() -> GraphDemoService:
        nonlocal resolved_graph_service
        if resolved_graph_service is None:
            resolved_graph_service = GraphDemoService(fallback_provider=resolved_tool_provider)
        return resolved_graph_service

    def get_global_semantic_graph_service() -> GlobalSemanticGraphService:
        nonlocal resolved_global_semantic_graph_service
        if resolved_global_semantic_graph_service is None:
            resolved_global_semantic_graph_service = GlobalSemanticGraphService()
        return resolved_global_semantic_graph_service

    def get_evidence_graph_builder() -> EvidenceGraphBuilder:
        return EvidenceGraphBuilder()

    def paper_search_result(
        query: str,
        limit: int,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> tuple[list[PaperSearchItem], list[str]]:
        try:
            return get_graph_service().search_papers(query, limit, year_from=year_from, year_to=year_to)
        except GraphDemoError:
            raise
        except Exception as exc:
            logger.exception("paper_search=fallback_error")
            raise GraphDemoError("database_unavailable", "Paper search failed", {"error": str(exc)}) from exc

    def paper_search_items(query: str, limit: int) -> list[PaperSearchItem]:
        items, _ = paper_search_result(query, limit)
        return items

    app = FastAPI(
        title="BigDipper Scholar Backend Agent",
        version="0.1.0",
        description="Scholarly analysis, research observations, and multimodal evidence.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_origin_regex=r"http://.*:5173",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tools", response_model=list[ToolSpec])
    def list_tools() -> list[ToolSpec]:
        return resolved_tool_provider.list_tools()

    @app.post("/api/auth/register", response_model=AuthResponse)
    def register(request: RegisterRequest) -> AuthResponse:
        email = request.email.strip().lower()
        display_name = request.display_name.strip()
        if "@" not in email or not display_name:
            raise HTTPException(status_code=400, detail="Invalid registration input")
        try:
            user = get_storage().create_user(email, _hash_password(request.password), display_name)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Email already registered") from None
        token = _new_access_token()
        get_storage().create_session(user.id, token)
        return AuthResponse(user=user, accessToken=token)

    @app.post("/api/auth/login", response_model=AuthResponse)
    def login(request: LoginRequest) -> AuthResponse:
        record = get_storage().get_user_auth_record_by_email(request.email)
        if record is None or not _verify_password(request.password, record["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user = AuthUser(
            id=record["id"],
            email=record["email"],
            displayName=record["display_name"],
            createdAt=record["created_at"],
        )
        token = _new_access_token()
        get_storage().create_session(user.id, token)
        return AuthResponse(user=user, accessToken=token)

    @app.get("/api/auth/me", response_model=MeResponse)
    def me(current_user: AuthUser = Depends(require_user)) -> MeResponse:
        return MeResponse(user=current_user)

    @app.post("/api/auth/logout", response_model=LogoutResponse)
    def logout(authorization: str | None = Header(default=None)) -> LogoutResponse:
        token = _bearer_token(authorization)
        get_storage().revoke_session(token)
        return LogoutResponse(ok=True)

    @app.post("/api/papers/search", response_model=PaperSearchResponse)
    def search_papers(
        request: PaperSearchRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> PaperSearchResponse:
        del current_user
        try:
            items, warnings = paper_search_result(
                request.query,
                request.limit,
                year_from=request.year_from,
                year_to=request.year_to,
            )
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc
        return PaperSearchResponse(query=request.query, items=items, warnings=warnings)

    @app.get("/api/papers/{paper_id}", response_model=PaperDetailResponse)
    def get_paper_detail(
        paper_id: str,
        current_user: AuthUser = Depends(require_user),
    ) -> PaperDetailResponse:
        del current_user
        try:
            paper, _warnings = get_graph_service().get_paper(paper_id)
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc
        return PaperDetailResponse(paper=paper)

    @app.post("/api/graphs/query", response_model=GraphResponse)
    def query_graph(
        request: GraphQueryRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> GraphResponse:
        if request.workspace_id and get_storage().get_workspace(current_user.id, request.workspace_id) is None:
            raise _graph_http_exception(
                GraphDemoError("workspace_not_found", "Workspace not found", {"workspaceId": request.workspace_id})
            )
        try:
            graph = get_graph_service().query_graph(
                request.query_type,
                request.workspace_id,
                query=request.query,
                seed_paper_id=request.seed_paper_id,
                limit=request.limit,
                include_references=request.include_references,
                include_citations=request.include_citations,
                include_authors=request.include_authors,
                include_institutions=request.include_institutions,
                include_fields=request.include_fields,
            )
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc
        if request.workspace_id:
            graph_id = get_storage().create_graph_state(
                request.workspace_id,
                graph.title,
                graph.query_context,
                graph.seed_node_id,
                graph.nodes,
                graph.edges,
                papers=graph.papers,
                warnings=graph.warnings,
                default_view_mode=graph.default_view_mode,
                seed_paper_id=graph.seed_paper_id,
            )
            graph.graph_id = graph_id
        return graph

    @app.get(
        "/api/semantic-graphs/global/overview",
        response_model=GlobalSemanticOverviewResponse,
    )
    def global_semantic_graph_overview(
        dataset: GlobalSemanticDataset = Query(default="all"),
        min_paper_count: int = Query(default=2, ge=1, alias="minPaperCount"),
        current_user: AuthUser = Depends(require_user),
    ) -> GlobalSemanticOverviewResponse:
        del current_user
        try:
            return get_global_semantic_graph_service().overview(dataset, min_paper_count)
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc

    @app.get(
        "/api/semantic-graphs/global/entities",
        response_model=GlobalSemanticEntitySearchResponse,
    )
    def search_global_semantic_entities(
        query: str = Query(min_length=1),
        dataset: GlobalSemanticDataset = Query(default="all"),
        min_paper_count: int = Query(default=2, ge=1, alias="minPaperCount"),
        entity_types: list[str] | None = Query(default=None, alias="entityTypes"),
        limit: int = Query(default=20, ge=1, le=200),
        current_user: AuthUser = Depends(require_user),
    ) -> GlobalSemanticEntitySearchResponse:
        del current_user
        try:
            return get_global_semantic_graph_service().search_entities(
                query,
                dataset,
                min_paper_count,
                entity_types or [],
                limit,
            )
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc

    @app.post(
        "/api/semantic-graphs/global/query",
        response_model=GlobalSemanticGraphResponse,
    )
    def query_global_semantic_graph(
        request: GlobalSemanticGraphQueryRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> GlobalSemanticGraphResponse:
        del current_user
        try:
            return get_global_semantic_graph_service().query_graph(request)
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc

    @app.post(
        "/api/semantic-graphs/global/expand",
        response_model=GlobalSemanticGraphExpandResponse,
    )
    def expand_global_semantic_graph(
        request: GlobalSemanticGraphExpandRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> GlobalSemanticGraphExpandResponse:
        del current_user
        try:
            return get_global_semantic_graph_service().expand_graph(request)
        except GraphDemoError as exc:
            raise _graph_http_exception(exc) from exc

    @app.get("/api/workspaces", response_model=WorkspaceListResponse)
    def list_workspaces(
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = None,
        current_user: AuthUser = Depends(require_user),
    ) -> WorkspaceListResponse:
        items, next_cursor = get_storage().list_workspaces(current_user.id, limit=limit, cursor=cursor)
        return WorkspaceListResponse(items=items, nextCursor=next_cursor)

    @app.post("/api/workspaces", response_model=WorkspaceCreateResponse)
    def create_workspace(
        request: WorkspaceCreateRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> WorkspaceCreateResponse:
        if request.entry_type == "paper_search":
            query = (request.query or "").strip()
            if not query:
                raise HTTPException(status_code=400, detail="query is required for paper_search")
            search_graph = get_graph_service().search_paper_graph(query, None, request.limit)
            nodes, edges = search_graph.nodes, search_graph.edges
            workspace, graph = get_storage().create_paper_workspace(
                current_user.id,
                query,
                nodes,
                edges,
                paper_count=len(search_graph.papers),
                papers=search_graph.papers,
                warnings=search_graph.warnings,
                default_view_mode=search_graph.default_view_mode,
                seed_paper_id=search_graph.seed_paper_id,
            )
            return WorkspaceCreateResponse(
                workspace=workspace,
                initialViewMode="list",
                initialGraph=graph,
                initialChatMessages=[],
            )

        message = (request.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required for agent_chat")
        workspace, graph, chat_messages = get_storage().create_agent_workspace(current_user.id, message)
        return WorkspaceCreateResponse(
            workspace=workspace,
            initialViewMode="default",
            initialGraph=graph,
            initialChatMessages=chat_messages,
        )

    @app.get("/api/workspaces/{workspace_id}/graph-states", response_model=GraphStateListResponse)
    def list_graph_states(
        workspace_id: str,
        current_user: AuthUser = Depends(require_user),
    ) -> GraphStateListResponse:
        workspace = get_storage().get_workspace(current_user.id, workspace_id)
        if workspace is None:
            raise _graph_http_exception(GraphDemoError("workspace_not_found", "Workspace not found", {"workspaceId": workspace_id}))
        return GraphStateListResponse(items=get_storage().list_graph_states(workspace_id))

    @app.get("/api/workspaces/{workspace_id}/graph-states/{graph_id}", response_model=GraphResponse)
    def get_graph_state(
        workspace_id: str,
        graph_id: str,
        current_user: AuthUser = Depends(require_user),
    ) -> GraphResponse:
        workspace = get_storage().get_workspace(current_user.id, workspace_id)
        if workspace is None:
            raise _graph_http_exception(GraphDemoError("workspace_not_found", "Workspace not found", {"workspaceId": workspace_id}))
        graph = get_storage().get_graph_response(workspace_id, graph_id)
        if graph is None:
            raise _graph_http_exception(GraphDemoError("graph_state_not_found", "Graph state not found", {"graphId": graph_id}))
        return graph

    @app.post("/api/workspaces/{workspace_id}/graph-states", response_model=GraphStateCreateResponse)
    def create_graph_state(
        workspace_id: str,
        request: GraphStateCreateRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> GraphStateCreateResponse:
        workspace = get_storage().get_workspace(current_user.id, workspace_id)
        if workspace is None:
            raise _graph_http_exception(GraphDemoError("workspace_not_found", "Workspace not found", {"workspaceId": workspace_id}))
        graph_id = get_storage().create_graph_state(
            workspace_id,
            request.title,
            request.query_context,
            request.seed_node_id,
            request.graph.nodes,
            request.graph.edges,
            papers=request.graph.papers,
            warnings=request.graph.warnings,
            default_view_mode=request.default_view_mode,
            seed_paper_id=request.seed_paper_id,
        )
        return GraphStateCreateResponse(graphId=graph_id)

    @app.delete("/api/workspaces/{workspace_id}", response_model=DeleteWorkspaceResponse)
    def delete_workspace(
        workspace_id: str,
        current_user: AuthUser = Depends(require_user),
    ) -> DeleteWorkspaceResponse:
        if not get_storage().delete_workspace(current_user.id, workspace_id):
            raise HTTPException(status_code=404, detail="Workspace not found")
        return DeleteWorkspaceResponse(ok=True)

    @app.get("/api/workspaces/{workspace_id}", response_model=WorkspaceDetailResponse)
    def get_workspace(
        workspace_id: str,
        current_user: AuthUser = Depends(require_user),
    ) -> WorkspaceDetailResponse:
        workspace = get_storage().get_workspace(current_user.id, workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        active_graph = get_storage().get_active_graph(workspace.id, workspace.active_graph_state_id)
        if active_graph is None and workspace.entry_type == "agent_chat":
            active_graph = GraphSnapshot(
                graphId=None,
                title="Empty graph",
                queryContext="Waiting for Agent graph result",
                seedNodeId=None,
                nodes=[],
                edges=[],
            )
        initial_chat_messages: list[ChatSeedMessage] = []
        if workspace.conversation_id:
            artifacts = get_storage().get_agent_run_artifacts_for_conversation(workspace.conversation_id)
            artifacts_by_answer = {
                str(item.get("final_answer") or ""): item
                for item in artifacts
                if item.get("final_answer")
            }
            initial_chat_messages = [
                _chat_seed_message_from_history(message, artifacts_by_answer.get(message.content))
                for message in get_storage().get_messages(workspace.conversation_id)
            ]
        return WorkspaceDetailResponse(
            workspace=workspace,
            activeGraph=active_graph,
            initialChatMessages=initial_chat_messages,
        )

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(
        request: ChatRequest,
        authorization: str | None = Header(default=None),
    ) -> ChatResponse:
        started_at = time.perf_counter()
        request_started_at = datetime.now(timezone.utc)
        started_cpu = time.process_time()
        active_storage = get_storage()
        workspace_id_for_touch: str | None = None
        if request.workspace_id:
            token = _bearer_token(authorization)
            user = active_storage.get_user_by_token(token)
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            workspace = active_storage.get_workspace(user.id, request.workspace_id)
            if workspace is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            conversation_id = active_storage.ensure_workspace_conversation(request.workspace_id)
            if conversation_id is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
            workspace_id_for_touch = request.workspace_id
        else:
            conversation_id = active_storage.ensure_conversation(request.conversation_id)
        run_record = run_registry.start(conversation_id, request.run_id)
        logger.info(
            "conversation=%s run=%s request=start include_trace=%s user_question=%s",
            conversation_id,
            run_record.run_id,
            request.include_trace,
            request.message,
        )
        usage_collector = ResourceUsageCollector(
            run_record.run_id,
            conversation_id,
            started_at=request_started_at,
            started_perf=started_at,
            started_cpu=started_cpu,
        )
        with resource_usage_session(usage_collector):
            history = active_storage.get_messages(conversation_id)
            try:
                response = get_agent().run(
                    conversation_id,
                    request.message,
                    history,
                    run_id=run_record.run_id,
                    include_trace=request.include_trace,
                    run_record=run_record,
                )
            except RunCancelledError:
                run_registry.cancel(run_record.run_id, reason="cancelled_during_agent_run")
                response = ChatResponse(
                    conversationId=conversation_id,
                    runId=run_record.run_id,
                    status="cancelled",
                    message="",
                    toolCalls=[],
                )

            if run_record.is_cancelled or response.status == "cancelled":
                run_registry.cancel(run_record.run_id, reason=run_record.reason or "cancelled_before_persistence")
                response.status = "cancelled"
                response.message = ""
                response.tool_calls = []
            elif response.status == "completed":
                active_storage.add_message(conversation_id, "user", request.message)
                active_storage.add_message(conversation_id, "assistant", response.message)
                active_storage.add_tool_calls(conversation_id, response.tool_calls)
                if workspace_id_for_touch:
                    active_storage.touch_workspace(workspace_id_for_touch)
                _save_chat_agent_run_artifact(active_storage, response, conversation_id, workspace_id_for_touch)
                run_registry.complete(run_record.run_id)
            else:
                _save_chat_agent_run_artifact(active_storage, response, conversation_id, workspace_id_for_touch)
                run_registry.fail(run_record.run_id)

            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "conversation=%s run=%s request=end status=%s elapsed_ms=%.2f tool_calls=%s",
                conversation_id,
                run_record.run_id,
                response.status,
                elapsed_ms,
                [tool_call.name for tool_call in response.tool_calls],
            )
            response.resource_usage = usage_collector.finish(response.status)
        return response

    @app.get("/api/chat/runs/{run_id}/evidence")
    def get_run_evidence(run_id: str, workspace_id: str, current_user: AuthUser = Depends(require_user)) -> dict:
        storage = get_storage()
        workspace = storage.get_workspace(current_user.id, workspace_id)
        artifact = storage.get_agent_run_artifact(run_id)
        if workspace is None or artifact is None or artifact.get("workspace_id") != workspace_id or artifact.get("conversation_id") != workspace.conversation_id:
            raise HTTPException(status_code=404, detail="Agent run artifact not found")
        return {"runId": run_id, "report": artifact.get("research_report"),
                "evidenceBundle": artifact.get("evidence_bundle"),
                "selectedObservations": artifact.get("selected_observations")}

    @app.post("/api/chat/runs/{run_id}/evidence-graph", response_model=AgentEvidenceGraphResponse)
    def build_evidence_graph(
        run_id: str,
        request: AgentEvidenceGraphRequest,
        current_user: AuthUser = Depends(require_user),
    ) -> AgentEvidenceGraphResponse:
        started = time.perf_counter()
        active_storage = get_storage()
        workspace = active_storage.get_workspace(current_user.id, request.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        artifact = active_storage.get_agent_run_artifact(run_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Agent run artifact not found")
        if artifact.get("workspace_id") != request.workspace_id or artifact.get("conversation_id") != workspace.conversation_id:
            raise HTTPException(status_code=404, detail="Agent run artifact not found")

        cached_graph = artifact.get("graph_response")
        if cached_graph and not request.force_refresh:
            graph = GraphResponse.model_validate(cached_graph)
            logger.info(
                "evidence_graph_cache_hit run_id=%s workspace_id=%s graph_state_id=%s nodes=%s edges=%s",
                run_id,
                request.workspace_id,
                artifact.get("graph_state_id"),
                len(graph.nodes),
                len(graph.edges),
            )
            return AgentEvidenceGraphResponse(runId=run_id, status="completed", graph=graph, missingReportEntities=[], warnings=graph.warnings)

        logger.info("evidence_graph_build_start run_id=%s workspace_id=%s", run_id, request.workspace_id)
        try:
            result = get_evidence_graph_builder().build(
                run_id=run_id,
                workspace_id=request.workspace_id,
                evidence_bundle=artifact.get("evidence_bundle") or {},
                selected_observations=artifact.get("selected_observations") or [],
                report=str(artifact.get("research_report") or artifact.get("final_answer") or ""),
                max_nodes=request.max_nodes,
                include_authors=request.include_authors,
                include_institutions=request.include_institutions,
                include_citation_edges=request.include_citation_edges,
            )
        except Exception as exc:
            logger.exception("evidence_graph_build_failed run_id=%s workspace_id=%s error=%s", run_id, request.workspace_id, exc)
            return AgentEvidenceGraphResponse(runId=run_id, status="unavailable", graph=None, missingReportEntities=[], warnings=[str(exc)])

        if result.graph is None:
            logger.info(
                "evidence_graph_build_end run_id=%s workspace_id=%s status=unavailable warnings=%s elapsed_ms=%.2f",
                run_id,
                request.workspace_id,
                result.warnings,
                (time.perf_counter() - started) * 1000,
            )
            return AgentEvidenceGraphResponse(runId=run_id, status="unavailable", graph=None, missingReportEntities=result.missing_report_entities, warnings=result.warnings)

        graph = result.graph
        graph_id = active_storage.create_graph_state(
            request.workspace_id,
            graph.title,
            graph.query_context,
            graph.seed_node_id,
            graph.nodes,
            graph.edges,
            papers=graph.papers,
            warnings=graph.warnings,
            default_view_mode=graph.default_view_mode,
            seed_paper_id=graph.seed_paper_id,
        )
        graph.graph_id = graph_id
        graph.workspace_id = request.workspace_id
        graph_artifact = {
            "status": "available",
            "runId": run_id,
            "title": graph.title,
            "reason": (artifact.get("graph_artifact") or {}).get("reason") or "Evidence graph built from selected observations.",
            "nodePreviewCount": len(graph.nodes),
            "edgePreviewCount": len(graph.edges),
            "warnings": graph.warnings,
        }
        active_storage.update_agent_run_graph(
            run_id,
            graph_response=graph.model_dump(by_alias=True),
            graph_state_id=graph_id,
            graph_artifact=graph_artifact,
        )
        logger.info(
            "evidence_graph_build_end run_id=%s workspace_id=%s status=completed graph_state_id=%s nodes=%s edges=%s warnings=%s elapsed_ms=%.2f",
            run_id,
            request.workspace_id,
            graph_id,
            len(graph.nodes),
            len(graph.edges),
            len(graph.warnings),
            (time.perf_counter() - started) * 1000,
        )
        return AgentEvidenceGraphResponse(runId=run_id, status="completed", graph=graph, missingReportEntities=result.missing_report_entities, warnings=graph.warnings)

    @app.post("/api/chat/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(run_id: str, request: CancelRequest | None = None) -> CancelResponse:
        reason = request.reason if request else None
        record = run_registry.cancel(run_id, reason=reason or "user_cancelled")
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        logger.info("run=%s cancel_requested status=%s reason=%s", run_id, record.status, reason)
        return CancelResponse(
            runId=run_id,
            status="completed" if record.status == "completed" else "cancelled",
            cancelledAt=record.cancelled_at,
        )

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
    def get_conversation(conversation_id: str) -> ConversationResponse:
        conversation = get_storage().get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation

    return app


def _save_chat_agent_run_artifact(
    storage: SQLiteStorage,
    response: ChatResponse,
    conversation_id: str,
    workspace_id: str | None,
) -> None:
    artifact = response.internal_artifact or {}
    if not artifact:
        return
    graph_artifact = artifact.get("graphArtifact") or (response.graph_artifact.model_dump(by_alias=True) if response.graph_artifact else {})
    storage.save_agent_run_artifact(
        run_id=response.run_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        route=str(artifact.get("route") or "unknown"),
        status=response.status,
        final_answer=response.message,
        research_report=artifact.get("researchReport"),
        evidence_bundle=artifact.get("evidenceBundle") if isinstance(artifact.get("evidenceBundle"), dict) else {},
        research_response=artifact.get("researchResponse") if isinstance(artifact.get("researchResponse"), dict) else {},
        selected_observations=artifact.get("selectedObservations") if isinstance(artifact.get("selectedObservations"), list) else [],
        events_log_path=str(artifact.get("eventsLogPath") or ""),
        graph_artifact=graph_artifact if isinstance(graph_artifact, dict) else {},
    )
    logger.info(
        "agent_run_artifact_saved run_id=%s conversation_id=%s workspace_id=%s route=%s status=%s graph_status=%s",
        response.run_id,
        conversation_id,
        workspace_id,
        artifact.get("route") or "unknown",
        response.status,
        graph_artifact.get("status") if isinstance(graph_artifact, dict) else None,
    )


def _chat_seed_message_from_history(message: MessageOut, artifact: dict[str, Any] | None) -> ChatSeedMessage:
    if message.role != "assistant" or not artifact:
        return ChatSeedMessage(
            role=message.role,
            content=message.content,
            createdAt=message.created_at,
            pendingSend=False,
        )
    graph_artifact = artifact.get("graph_artifact") if isinstance(artifact.get("graph_artifact"), dict) else None
    research_response = artifact.get("research_response") if isinstance(artifact.get("research_response"), dict) else {}
    research_steps = research_response.get("researchSteps") if isinstance(research_response.get("researchSteps"), list) else []
    return ChatSeedMessage(
        role=message.role,
        content=message.content,
        createdAt=message.created_at,
        pendingSend=False,
        runId=artifact.get("run_id"),
        graphArtifact=graph_artifact,
        researchSteps=research_steps,
    )


def _graph_http_exception(exc: GraphDemoError) -> HTTPException:
    status_by_code = {
        "paper_not_found": 404,
        "workspace_not_found": 404,
        "graph_state_not_found": 404,
        "global_entity_not_found": 404,
        "global_graph_not_ready": 503,
        "database_unavailable": 503,
        "invalid_query": 400,
    }
    return HTTPException(
        status_code=status_by_code.get(exc.code, 400),
        detail={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


_PASSWORD_ITERATIONS = 120_000


def _new_access_token() -> str:
    return f"bds_{secrets.token_urlsafe(32)}"


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = stored_hash.split("$", 3)
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return hmac.compare_digest(digest.hex(), expected)


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return token.strip()


def _paper_search_graph(items: list[PaperSearchItem]) -> tuple[list[GraphNode], list[dict]]:
    from app.graph_demo import paper_graph_nodes
    return paper_graph_nodes(items, include_institutions=False)




def _frontend_paper_edge_type(edge_type: str) -> str:
    if edge_type == "cites":
        return "reference"
    if edge_type == "similar":
        return "similar"
    return "similar"


def _refresh_search_graph_relations(graph: GraphSnapshot) -> GraphSnapshot:
    if any(node.node_type in {"author", "institution"} for node in graph.nodes):
        return graph

    paper_nodes = [node for node in graph.nodes if node.node_type == "paper"]
    if not paper_nodes:
        return graph

    items = [_paper_search_item(node.model_dump(by_alias=True), index) for index, node in enumerate(paper_nodes)]
    nodes, edges = _paper_search_graph(items)
    return GraphSnapshot(
        graphId=graph.graph_id,
        title=graph.title,
        queryContext=graph.query_context,
        seedNodeId=graph.seed_node_id,
        seedPaperId=graph.seed_paper_id,
        defaultViewMode=graph.default_view_mode,
        nodes=nodes,
        papers=graph.papers,
        edges=edges,
        warnings=graph.warnings,
    )


def _dedupe_edges(edges: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique_edges: list[dict] = []
    for edge in edges:
        edge_id = str(edge.get("id"))
        if edge_id in seen:
            continue
        seen.add(edge_id)
        unique_edges.append(edge)
    return unique_edges


def _stable_slug(value: str) -> str:
    normalized = "-".join(str(value).strip().lower().split()) or "unknown"
    safe = "".join(char if char.isalnum() or char == "-" else "-" for char in normalized).strip("-")
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]
    return f"{safe[:36] or 'unknown'}-{digest}"






def _paper_search_item(raw_paper: dict, index: int) -> PaperSearchItem:
    title = str(raw_paper.get("title") or "Untitled paper")
    year = int(raw_paper.get("year") or 0)
    authors = raw_paper.get("authors") if isinstance(raw_paper.get("authors"), list) else []
    first_author = str(authors[0]) if authors else "Unknown"
    label = f"{first_author}, {year}" if year else first_author
    return PaperSearchItem(
        id=str(raw_paper.get("id") or f"paper_{index}"),
        nodeType="paper",
        label=label,
        title=title,
        year=year,
        authors=[str(author) for author in authors],
        venue=str(raw_paper.get("venue") or ""),
        abstract=str(raw_paper.get("abstract") or ""),
        doi=raw_paper.get("doi"),
        citationCount=int(raw_paper.get("citationCount") or raw_paper.get("citation_count") or 0),
        topic=str(raw_paper.get("topic") or ""),
        summary=title,
        metric=float(raw_paper.get("citationCount") or raw_paper.get("citation_count") or 0),
        position={"x": index * 140, "y": 0},
    )


app = create_app()
