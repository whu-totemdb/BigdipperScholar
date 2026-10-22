from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent_experiments import AgentExperimentService, _extract_tool_calls
from app.config import get_env
from app.llm import DeepSeekClient
from app.models import AgentExperimentRequest, AgentExperimentResponse, ChatGraphArtifact, ChatResponse, ToolCall, TraceStep
from app.run_registry import RunCancelledError, RunRecord


RUN_RESEARCH_TOOL_NAME = "run_research_report"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_ROOT = REPO_ROOT / "prompts" / "agent_research_report_flow"
DEFAULT_PROMPT_DIR = PROMPT_ROOT / "runtime_prompts"
DEFAULT_DOCS_DIR = PROMPT_ROOT / "runtime_docs"
DEFAULT_LOG_ROOT = REPO_ROOT / "logs" / "research_report_chat"


class ResearchReportChatAgent:
    """Online conversation-agent facade for the research/report pipeline."""

    def __init__(
        self,
        *,
        llm_client: Any | None = None,
        experiment_service: AgentExperimentService | None = None,
        prompt_dir: str | Path | None = None,
        prompt_docs_dir: str | Path | None = None,
        log_root: str | Path | None = None,
    ) -> None:
        model = get_env("BIGDIPPER_AGENT_EXPERIMENT_MODEL") or get_env("DEEPSEEK_MODEL")
        timeout = float(get_env("BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT", "120") or "120")
        self.llm_client = llm_client or DeepSeekClient(model=model, timeout=timeout)
        self.experiment_service = experiment_service
        self.prompt_dir = Path(prompt_dir or get_env("BIGDIPPER_AGENT_PROMPTS_DIR") or DEFAULT_PROMPT_DIR)
        self.prompt_docs_dir = Path(prompt_docs_dir or get_env("BIGDIPPER_AGENT_PROMPT_DOCS_DIR") or DEFAULT_DOCS_DIR)
        self.log_root = Path(log_root or get_env("BIGDIPPER_RESEARCH_REPORT_CHAT_LOG_DIR") or DEFAULT_LOG_ROOT)

    def run(
        self,
        conversation_id: str,
        user_message: str,
        history: list[Any],
        *,
        run_id: str = "",
        include_trace: bool = False,
        run_record: RunRecord | None = None,
    ) -> ChatResponse:
        run_id = run_id or f"chat_rr_{secrets.token_hex(8)}"
        run_dir = self.log_root / run_id
        events_log = run_dir / "events.jsonl"
        trace: list[dict[str, Any]] = []
        tool_calls: list[ToolCall] = []

        self._log(
            events_log,
            run_id,
            "conversation_run_start",
            query=user_message,
            historyTurns=len(history) // 2 if history else 0,
            model=getattr(self.llm_client, "model", None),
        )
        try:
            if not self.llm_client.is_configured:
                raise RuntimeError("Configure LLM_BASE_URL, LLM_API_KEY and LLM_MODEL before submitting research queries.")
            if run_record is not None:
                run_record.raise_if_cancelled()

            conversation_prompt = _read_prompt(self.prompt_dir / "conversation_agent.md")
            route_result = self._route_conversation(
                run_id,
                events_log,
                conversation_prompt,
                user_message,
                history,
            )
            trace.extend(route_result.trace)

            if route_result.route == "direct":
                self._log(events_log, run_id, "assistant_final", answer=route_result.answer, source="conversation_agent")
                self._log(events_log, run_id, "conversation_run_end", status="completed")
                _write_online_artifacts(run_dir, final_answer=route_result.answer, response_payload={"route": "direct", "status": "completed"})
                return ChatResponse(
                    conversationId=conversation_id,
                    runId=run_id,
                    status="completed",
                    message=route_result.answer,
                    toolCalls=[],
                    trace=_trace_steps(trace) if include_trace else None,
                    frontendPayload=None,
                    internalArtifact={
                        "route": "direct",
                        "status": "completed",
                        "eventsLogPath": str(events_log),
                        "graphArtifact": {"status": "none", "runId": run_id, "title": "Evidence Graph", "reason": "direct conversation", "nodePreviewCount": 0, "edgePreviewCount": 0, "warnings": []},
                    },
                )

            if run_record is not None:
                run_record.raise_if_cancelled()

            research_query = _research_query_from_args(user_message, history, route_result.research_arguments)
            self._log(events_log, run_id, "research_request", query=research_query, maxRounds=_max_rounds())
            service = self._service_for_run(events_log)
            request = AgentExperimentRequest(
                query=research_query,
                agentMode="mmdb_scholar",
                runId=run_id,
                maxRounds=_max_rounds(),
                topK=20,
                task_family=None,
                includeTrace=True,
            )
            response = service.run(request, run_record=run_record)
            if response.status != "completed":
                raise RuntimeError(response.report)
            trace.extend(response.trace)
            tool_calls.extend(_tool_calls_from_experiment_trace(response.trace))
            self._log(events_log, run_id, "research_response", status=response.status, metrics=response.metrics, report=response.report)

            if run_record is not None:
                run_record.raise_if_cancelled()

            # Return the grounded report verbatim: a later conversational rewrite
            # would no longer share the evidence agent's exact input report.
            final_answer = response.report
            graph_artifact = _graph_artifact_from_experiment_response(run_id, response)
            if graph_artifact and graph_artifact.status == "available":
                self._log(
                    events_log,
                    run_id,
                    "graph_artifact_available",
                    nodePreviewCount=graph_artifact.node_preview_count,
                    edgePreviewCount=graph_artifact.edge_preview_count,
                    warnings=graph_artifact.warnings,
                )
            self._log(events_log, run_id, "assistant_final", answer=final_answer, source="conversation_agent_after_research")
            status = "completed" if response.status == "completed" else "failed"
            self._log(events_log, run_id, "conversation_run_end", status=status)
            _write_online_artifacts(
                run_dir,
                final_answer=final_answer,
                response_payload={
                    "route": "research_report",
                    "status": status,
                    "researchReport": response.report,
                    "evidenceBundle": response.evidence_bundle,
                    "selectedObservations": response.selected_observations,
                    "researchResponse": response.model_dump(by_alias=True),
                    "graphArtifact": graph_artifact.model_dump(by_alias=True) if graph_artifact else None,
                },
                report=response.report,
                metrics=response.metrics,
            )
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status=status,
                message=final_answer,
                toolCalls=tool_calls,
                trace=_trace_steps(trace) if include_trace else None,
                researchSteps=response.research_steps,
                frontendPayload=None,
                graphArtifact=graph_artifact,
                internalArtifact={
                    "route": "research_report",
                    "status": status,
                    "researchReport": response.report,
                    "evidenceBundle": response.evidence_bundle,
                    "researchResponse": response.model_dump(by_alias=True),
                    "selectedObservations": response.selected_observations,
                    "eventsLogPath": str(events_log),
                    "graphArtifact": graph_artifact.model_dump(by_alias=True) if graph_artifact else {},
                },
            )
        except RunCancelledError:
            self._log(events_log, run_id, "conversation_run_cancelled", status="cancelled")
            raise
        except Exception as exc:
            self._log(events_log, run_id, "conversation_run_failed", status="failed", error=str(exc))
            self._log(events_log, run_id, "conversation_run_end", status="failed")
            message = f"Research-report agent failed: {exc}"
            _write_online_artifacts(run_dir, final_answer=message, response_payload={"route": "failed", "status": "failed", "error": str(exc)})
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status="failed",
                message=message,
                toolCalls=tool_calls,
                trace=_trace_steps(trace) if include_trace else None,
                frontendPayload=None,
                internalArtifact={
                    "route": "failed",
                    "status": "failed",
                    "eventsLogPath": str(events_log),
                    "error": str(exc),
                    "graphArtifact": {"status": "unavailable", "runId": run_id, "title": "Evidence Graph", "reason": "research-report failed", "nodePreviewCount": 0, "edgePreviewCount": 0, "warnings": [str(exc)]},
                },
            )

    def _route_conversation(
        self,
        run_id: str,
        events_log: Path,
        conversation_prompt: str,
        user_message: str,
        history: list[Any],
    ) -> "ConversationRouteResult":
        if not self.llm_client.is_configured:
            if _looks_like_research_query(user_message):
                arguments = {
                    "researchQuery": user_message,
                    "contextSummary": _history_summary(history),
                    "reason": "missing_llm_fallback_research_query",
                }
                self._log(events_log, run_id, "conversation_route", route="research_report", **arguments)
                return ConversationRouteResult(route="research_report", research_arguments=arguments)
            answer = "我是BigDipper Scholar。你好，我可以帮你做学术论文、作者、机构、引用关系和研究趋势分析。"
            self._log(events_log, run_id, "conversation_route", route="direct", reason="missing_llm_fallback_simple_query")
            return ConversationRouteResult(route="direct", answer=answer)

        result = self.llm_client.complete(
            [
                {"role": "system", "content": conversation_prompt},
                *_history_messages(history),
                {"role": "user", "content": user_message},
            ],
            tools=[_run_research_tool_definition()],
            tool_choice="auto",
            thinking=False,
        )
        tool_calls = _extract_tool_calls(result.message)
        self._log(
            events_log,
            run_id,
            "conversation_llm_response",
            content=result.content,
            toolCalls=[{"id": call["id"], "name": call["name"], "arguments": call["arguments"]} for call in tool_calls],
            usedLlm=result.used_llm,
            error=result.error,
        )
        trace = [
            {
                "event": "conversation_llm_response",
                "round": 0,
                "content": result.content,
                "toolCalls": [{"id": call["id"], "name": call["name"], "arguments": call["arguments"]} for call in tool_calls],
                "usedLlm": result.used_llm,
                "error": result.error,
            }
        ]
        research_call = next((call for call in tool_calls if call["name"] == RUN_RESEARCH_TOOL_NAME), None)
        if research_call is None:
            answer = result.content.strip() or "我是BigDipper Scholar。请告诉我你想研究的论文、作者、机构或主题。"
            self._log(events_log, run_id, "conversation_route", route="direct", reason="conversation_agent_answered_without_research")
            return ConversationRouteResult(route="direct", answer=answer, trace=trace)

        arguments = research_call["arguments"]
        self._log(
            events_log,
            run_id,
            "conversation_route",
            route="research_report",
            reason=str(arguments.get("reason") or "conversation_agent_requested_research"),
            researchQuery=str(arguments.get("researchQuery") or user_message),
            contextSummary=str(arguments.get("contextSummary") or ""),
        )
        return ConversationRouteResult(route="research_report", research_arguments=arguments, trace=trace)

    def _synthesize_final_answer(
        self,
        run_id: str,
        events_log: Path,
        conversation_prompt: str,
        user_message: str,
        history: list[Any],
        response: AgentExperimentResponse,
    ) -> str:
        if not self.llm_client.is_configured:
            return response.report
        payload = {
            "instruction": "请作为 conversation-agent，基于 research-report 返回的报告和当前对话上下文，给用户最终回复。不要编造报告中没有的数据。",
            "userQuery": user_message,
            "researchStatus": response.status,
            "researchReport": response.report,
            "evidence": response.evidence,
            "evidenceBundle": response.evidence_bundle,
            "metrics": response.metrics,
        }
        result = self.llm_client.complete(
            [
                {"role": "system", "content": conversation_prompt},
                *_history_messages(history),
                {"role": "user", "content": user_message},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            thinking=False,
        )
        self._log(
            events_log,
            run_id,
            "conversation_final_llm_response",
            content=result.content,
            usedLlm=result.used_llm,
            error=result.error,
        )
        return result.content.strip() if result.used_llm and result.content.strip() else response.report

    def _service_for_run(self, events_log: Path) -> AgentExperimentService:
        if self.experiment_service is not None:
            return self.experiment_service
        return AgentExperimentService(
            llm_client=self.llm_client,
            log_path=events_log,
            prompt_dir=self.prompt_dir,
            prompt_docs_dir=self.prompt_docs_dir,
        )

    def _log(self, events_log: Path, run_id: str, event_name: str, **payload: Any) -> None:
        events_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "runId": run_id,
            **payload,
            "event": event_name,
        }
        with events_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class ConversationRouteResult:
    def __init__(
        self,
        *,
        route: str,
        answer: str = "",
        research_arguments: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        self.route = route
        self.answer = answer
        self.research_arguments = research_arguments or {}
        self.trace = trace or []


def _graph_artifact_from_experiment_response(run_id: str, response: AgentExperimentResponse) -> ChatGraphArtifact | None:
    if response.status != "completed":
        return None
    bundle = response.evidence_bundle or {}
    graph_intent = bundle.get("graphIntent") if isinstance(bundle.get("graphIntent"), dict) else {}
    nodes = [item for item in graph_intent.get("nodes") or [] if isinstance(item, dict) and str(item.get("nodeType") or "") in {"paper", "author", "institution", "field"}]
    edges = [item for item in graph_intent.get("edges") or [] if isinstance(item, dict)]
    if not bundle.get("shouldRenderGraph") or not nodes:
        return None
    return ChatGraphArtifact(
        status="available",
        runId=run_id,
        title=str(graph_intent.get("title") or "Evidence Graph"),
        reason=str(bundle.get("reason") or "citation-agent found graphable evidence in selected observations"),
        nodePreviewCount=len(nodes),
        edgePreviewCount=len(edges),
        warnings=[str(item) for item in (bundle.get("warnings") or []) if item],
    )


def _run_research_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": RUN_RESEARCH_TOOL_NAME,
            "description": "Start the research-agent -> report-agent flow when the user needs real scholarly data lookup, statistics, paper/entity retrieval, citation/graph analysis, or a research report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "researchQuery": {
                        "type": "string",
                        "description": "A self-contained research task for research-agent. Preserve the user's real question and include constraints such as years, fields, authors, institutions, and requested output.",
                    },
                    "contextSummary": {
                        "type": "string",
                        "description": "Only the relevant previous conversation context needed to interpret this query. Empty string if not needed.",
                    },
                    "reason": {"type": "string", "description": "Brief reason why database/tool research is needed."},
                },
                "required": ["researchQuery", "contextSummary", "reason"],
                "additionalProperties": False,
            },
        },
    }


def _research_query_from_args(user_message: str, history: list[Any], arguments: dict[str, Any]) -> str:
    research_query = str(arguments.get("researchQuery") or user_message).strip()
    context = str(arguments.get("contextSummary") or "").strip() or _history_summary(history)
    reason = str(arguments.get("reason") or "").strip()
    parts = [
        "这是 conversation-agent 转交给 research-agent 的学术研究任务。",
        "",
        f"用户最新问题:\n{user_message}",
        "",
        f"research-agent 执行任务:\n{research_query}",
    ]
    if context:
        parts.extend(["", f"相关历史上下文:\n{context}"])
    if reason:
        parts.extend(["", f"conversation-agent 路由原因:\n{reason}"])
    return "\n".join(parts)


def _history_messages(history: list[Any], limit: int = 12) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in history[-limit:] if history else []:
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content.strip()})
    return messages


def _history_summary(history: list[Any], limit: int = 8) -> str:
    parts: list[str] = []
    for item in history[-limit:] if history else []:
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            label = "用户" if role == "user" else "助手"
            parts.append(f"{label}: {content.strip()}")
    return "\n".join(parts)


def _looks_like_research_query(query: str) -> bool:
    lowered = query.lower()
    markers = ["论文", "作者", "机构", "引用", "研究", "报告", "趋势", "进展", "paper", "author", "institution", "citation", "gnn", "graph neural"]
    return any(marker in lowered for marker in markers)


def _max_rounds() -> int:
    return _int_env("BIGDIPPER_RESEARCH_REPORT_CHAT_MAX_ROUNDS", 8)


def _int_env(name: str, default: int) -> int:
    try:
        return int(get_env(name, str(default)) or default)
    except ValueError:
        return default


def _read_prompt(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Missing online agent prompt file: {path}") from exc
    if not content:
        raise RuntimeError(f"Online agent prompt file is empty: {path}")
    return content


def _tool_calls_from_experiment_trace(trace: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    seen: set[tuple[str, str]] = set()
    for event in trace:
        for raw_call in event.get("toolCalls") or []:
            name = raw_call.get("name")
            if not isinstance(name, str) or not name or name == RUN_RESEARCH_TOOL_NAME:
                continue
            arguments = raw_call.get("arguments") if isinstance(raw_call.get("arguments"), dict) else {}
            key = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            calls.append(ToolCall(name=name, arguments=arguments, summary="Called by research-agent during report generation."))
    return calls


def _trace_steps(trace: list[dict[str, Any]]) -> list[TraceStep]:
    steps: list[TraceStep] = []
    for index, event in enumerate(trace, start=1):
        round_number = int(event.get("round") or index)
        tool_names = [call.get("name") for call in event.get("toolCalls") or [] if isinstance(call, dict) and call.get("name")]
        title = "Conversation-agent round" if event.get("event") == "conversation_llm_response" else "Research-agent round"
        summary = "Conversation-agent returned a direct response or routed the query."
        if title == "Research-agent round":
            summary = "Research-agent returned a model response."
        if tool_names:
            summary = f"{title.replace(' round', '')} requested tools: " + ", ".join(str(name) for name in tool_names)
        if event.get("error"):
            summary = f"{summary} Error: {event['error']}"
        steps.append(
            TraceStep(
                id=f"research_report_step_{index}",
                round=round_number,
                kind="llm",
                status="completed" if not event.get("error") else "failed",
                title=title,
                summary=summary,
            )
        )
    return steps


def _write_online_artifacts(
    run_dir: Path,
    *,
    final_answer: str,
    response_payload: dict[str, Any],
    report: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "final_answer.md").write_text(final_answer, encoding="utf-8")
    (run_dir / "response.json").write_text(json.dumps(response_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if report:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics or {"status": response_payload.get("status"), "route": response_payload.get("route")}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
