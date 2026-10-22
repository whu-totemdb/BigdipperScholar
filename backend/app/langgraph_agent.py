from __future__ import annotations

import time
import asyncio
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_openai import ChatOpenAI

from app.agent_logging import AgentTraceLogger
from app.config import get_env
from app.langchain_tools import build_scholar_tools
from app.llm import DeepSeekClient
from app.logging_config import get_logger
from app.mcp_client import load_configured_mcp_tools
from app.models import ChatResponse, FrontendPayload, Paper, PaperEdge, ToolCall, TraceStep
from app.prompts import PromptProvider
from app.run_registry import RunCancelledError, RunRecord
from app.tools.base import ToolProvider


logger = get_logger("bigdipper.langgraph")


class LangGraphScholarAgent:
    def __init__(
        self,
        tool_provider: ToolProvider | None = None,
        llm_client: DeepSeekClient | None = None,
        prompt_provider: PromptProvider | None = None,
    ) -> None:
        del tool_provider
        self.llm_client = llm_client or DeepSeekClient()
        self.prompt_provider = prompt_provider or PromptProvider()
        self.tool_mode = (get_env("BIGDIPPER_TOOL_MODE", "mcp") or "mcp").lower()
        self._tools = self._load_tools()
        self._agent = self._create_agent()

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
        started_at = time.perf_counter()
        logger.info("conversation=%s run=%s agent=start user_question=%s", conversation_id, run_id, user_message)
        collect_trace = _should_collect_trace(include_trace)
        trace_logger = AgentTraceLogger(
            conversation_id,
            run_record=run_record,
            collect_trace=collect_trace,
        )
        if not self.llm_client.is_configured:
            message = "DeepSeek is not configured. Set DEEPSEEK_API_KEY before using the LangGraph agent."
            trace_logger.add_final_step("failed", message)
            logger.error("conversation=%s run=%s agent=error reason=missing_deepseek_api_key", conversation_id, run_id)
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status="failed",
                message=message,
                toolCalls=[],
                trace=_trace_or_none(trace_logger),
            )

        try:
            if run_record is not None:
                run_record.raise_if_cancelled()
            input_messages = _messages_for_agent(history, user_message)
            if run_record is not None:
                run_record.raise_if_cancelled()
            result = asyncio.run(
                self._agent.ainvoke(
                    {"messages": input_messages},
                    config={"callbacks": [trace_logger]},
                )
            )
            if run_record is not None:
                run_record.raise_if_cancelled()
        except RunCancelledError:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            trace_logger.add_cancel_step()
            logger.info("conversation=%s run=%s agent=cancelled elapsed_ms=%.2f", conversation_id, run_id, elapsed_ms)
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status="cancelled",
                message="",
                toolCalls=[],
                trace=_trace_or_none(trace_logger),
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            trace_logger.add_final_step("failed", f"Agent execution failed: {exc}")
            logger.exception("conversation=%s run=%s agent=error elapsed_ms=%.2f error=%s", conversation_id, run_id, elapsed_ms, exc)
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status="failed",
                message=f"Agent execution failed: {exc}",
                toolCalls=[],
                trace=_trace_or_none(trace_logger),
            )

        messages = result.get("messages", [])
        answer = _last_answer(messages) or "I could not produce a final answer."
        tool_calls = _tool_calls_from_messages(messages)
        frontend_payload = _frontend_payload_from_messages(messages)
        trace_logger.add_final_step("completed", "The agent produced the final answer.")
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "conversation=%s run=%s agent=end elapsed_ms=%.2f tool_calls=%s answer_preview=%s",
            conversation_id,
            run_id,
            elapsed_ms,
            [call.name for call in tool_calls],
            answer[:500],
        )
        return ChatResponse(
            conversationId=conversation_id,
            runId=run_id,
            status="completed",
            message=answer,
            toolCalls=tool_calls,
            trace=_trace_or_none(trace_logger),
            frontendPayload=frontend_payload,
        )

    def _create_agent(self):
        model = ChatOpenAI(
            api_key=self.llm_client.api_key,
            base_url=self.llm_client.base_url,
            model=self.llm_client.model,
            temperature=0.2,
            timeout=self.llm_client.timeout,
            http_client=httpx.Client(trust_env=False, timeout=self.llm_client.timeout),
        )
        return create_agent(
            model=model,
            tools=self._tools,
            system_prompt=self.prompt_provider.main_agent_prompt(),
            name="BigDipperScholarAgent",
        )

    def _load_tools(self):
        if self.tool_mode == "mcp":
            return load_configured_mcp_tools()
        tools = build_scholar_tools()
        logger.info("langchain_tools=loaded count=%d names=%s", len(tools), [tool.name for tool in tools])
        return tools


def _messages_for_agent(history: list[Any], user_message: str) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for item in history[-16:] if history else []:
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content:
            messages.append((role, content))
    if not messages or messages[-1] != ("user", user_message):
        messages.append(("user", user_message))
    return messages


def _last_answer(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content.strip():
            return message.content.strip()
    return ""


def _tool_calls_from_messages(messages: list[BaseMessage]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    tool_summaries = {
        message.tool_call_id: _tool_summary(message)
        for message in messages
        if isinstance(message, ToolMessage)
    }
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for raw_call in message.tool_calls:
            name = raw_call.get("name")
            if not name:
                continue
            args = raw_call.get("args") or {}
            call_id = raw_call.get("id")
            calls.append(
                ToolCall(
                    name=name,
                    arguments=args if isinstance(args, dict) else {},
                    summary=tool_summaries.get(call_id, f"{name} completed"),
                )
            )
    return calls


def _tool_summary(message: ToolMessage) -> str:
    content = message.content
    text = content if isinstance(content, str) else str(content)
    parsed = _parse_tool_json(text)
    if isinstance(parsed, dict) and parsed.get("summary"):
        return str(parsed["summary"])
    return text[:500]


def _frontend_payload_from_messages(messages: list[BaseMessage]) -> FrontendPayload | None:
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        if message.name not in {"get_mock_graph", "search_papers", "get_related_papers"}:
            continue
        parsed = _parse_tool_json(message.content)
        if not isinstance(parsed, dict):
            continue
        papers = _parse_papers(parsed.get("papers", []))
        edges = _parse_edges(parsed.get("edges", []))
        return FrontendPayload(
            papers=papers,
            edges=edges,
            seedPaperId=parsed.get("seedPaperId") or (papers[0].id if papers else None),
            queryContext=message.name,
        )
    return None


def _parse_tool_json(content: str) -> Any:
    try:
        return __import__("json").loads(content)
    except Exception:
        return None


def _parse_papers(raw_papers: list[dict[str, Any]]) -> list[Paper]:
    return [Paper.model_validate(paper) for paper in raw_papers]


def _parse_edges(raw_edges: list[dict[str, Any]]) -> list[PaperEdge]:
    return [PaperEdge.model_validate(edge) for edge in raw_edges]


def _should_collect_trace(include_trace: bool) -> bool:
    mode = (get_env("BIGDIPPER_TRACE_MODE", "summary") or "summary").lower()
    return include_trace and mode in {"summary", "debug"}


def _trace_or_none(trace_logger: AgentTraceLogger) -> list[TraceStep] | None:
    if not trace_logger.collect_trace:
        return None
    return trace_logger.trace_steps
