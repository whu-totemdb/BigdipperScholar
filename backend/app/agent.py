from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any

from app.llm import DeepSeekClient
from app.models import ChatResponse, FrontendPayload, Paper, PaperEdge, ToolCall
from app.prompts import PromptProvider
from app.tools.base import ToolProvider


@dataclass(frozen=True)
class AgentResult:
    message: str
    tool_calls: list[ToolCall]
    frontend_payload: FrontendPayload | None = None


class ScholarAgent:
    def __init__(
        self,
        tool_provider: ToolProvider,
        llm_client: DeepSeekClient | None = None,
        prompt_provider: PromptProvider | None = None,
    ) -> None:
        self.tool_provider = tool_provider
        self.llm_client = llm_client or DeepSeekClient()
        self.prompt_provider = prompt_provider or PromptProvider()

    def run(
        self,
        conversation_id: str,
        user_message: str,
        history: list[Any],
        *,
        run_id: str = "",
        include_trace: bool = False,
        run_record: Any = None,
    ) -> ChatResponse:
        del history
        del include_trace
        if run_record is not None:
            run_record.raise_if_cancelled()
        result = self._run_rules(user_message)
        if run_record is not None:
            run_record.raise_if_cancelled()
        return ChatResponse(
            conversationId=conversation_id,
            runId=run_id,
            status="completed",
            message=result.message,
            toolCalls=result.tool_calls,
            frontendPayload=result.frontend_payload,
        )

    def _run_rules(self, user_message: str) -> AgentResult:
        db_plan = _plan_database_tools(user_message)
        if db_plan:
            tool_calls, tool_results = self._execute_tool_plan(db_plan)
            message = self._answer_from_tool_results(user_message, tool_calls, tool_results)
            return AgentResult(message=message, tool_calls=tool_calls)

        if _should_use_local_literature_tools(user_message):
            return self._run_mock_rules(user_message)

        direct_llm = self._answer_directly_with_llm(user_message)
        if direct_llm:
            return AgentResult(message=direct_llm, tool_calls=[])

        return self._run_mock_rules(user_message)

    def _execute_tool_plan(
        self,
        plan: list[tuple[str, dict[str, Any]]],
    ) -> tuple[list[ToolCall], list[dict[str, Any]]]:
        tool_calls: list[ToolCall] = []
        tool_results: list[dict[str, Any]] = []
        for name, arguments in plan:
            try:
                result = self.tool_provider.call_tool(name, arguments)
            except Exception as exc:
                result = {
                    "status": "error",
                    "tool": name,
                    "error": str(exc),
                    "data": [],
                    "summary": f"{name} failed",
                }
            tool_results.append(result)
            tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    summary=str(result.get("summary", f"{name} completed")),
                )
            )
        return tool_calls, tool_results

    def _answer_from_tool_results(
        self,
        user_message: str,
        tool_calls: list[ToolCall],
        tool_results: list[dict[str, Any]],
    ) -> str:
        if self.llm_client.is_configured:
            llm_result = self.llm_client.complete(
                [
                    {"role": "system", "content": self.prompt_provider.main_agent_prompt()},
                    {
                        "role": "user",
                        "content": (
                            f"User question:\n{user_message}\n\n"
                            "Database tool calls:\n"
                            f"{_json_for_prompt([call.model_dump(by_alias=True) for call in tool_calls])}\n\n"
                            "Tool results:\n"
                            f"{_json_for_prompt(_summarize_tool_results_for_prompt(tool_results))}"
                        ),
                    },
                ]
            )
            if llm_result.used_llm and llm_result.content:
                return _sanitize_llm_answer(user_message, llm_result.content)

        return _fallback_database_answer(user_message, tool_calls, tool_results)

    def _answer_directly_with_llm(self, user_message: str) -> str | None:
        if not self.llm_client.is_configured:
            return None

        llm_result = self.llm_client.complete(
            [
                {"role": "system", "content": self.prompt_provider.main_agent_prompt()},
                {
                    "role": "user",
                    "content": (
                        f"{user_message}\n\n"
                        "No database tool was called for this question. "
                        "Answer directly, and briefly state that no database-backed query was used."
                    ),
                },
            ]
        )
        if llm_result.used_llm and llm_result.content:
            return llm_result.content
        return None

    def _run_mock_rules(self, user_message: str) -> AgentResult:
        normalized = user_message.lower()
        graph_request = _looks_like_graph_request(normalized)
        related_request = _looks_like_related_request(normalized)
        detail_request = _looks_like_detail_request(normalized)
        explicit_search = _looks_like_search_request(normalized) or (
            _looks_like_paper_title(user_message) and not (graph_request or related_request or detail_request)
        )

        if graph_request and not explicit_search:
            seed_paper_id = _extract_paper_id(user_message)
            arguments = {"seed_paper_id": seed_paper_id} if seed_paper_id else {}
            result = self.tool_provider.call_tool("get_mock_graph", arguments)
            papers = _parse_papers(result.get("papers", []))
            edges = _parse_edges(result.get("edges", []))
            payload = FrontendPayload(
                papers=papers,
                edges=edges,
                seedPaperId=result.get("seedPaperId"),
                queryContext="mock paper graph",
            )
            tool_call = ToolCall(
                name="get_mock_graph",
                arguments=arguments,
                summary=result["summary"],
            )
            return AgentResult(
                message=_summarize_graph(papers, edges),
                tool_calls=[tool_call],
                frontend_payload=payload,
            )

        if related_request and not explicit_search:
            paper_id = _extract_paper_id(user_message) or "p-graph-rag-2025"
            mode = _related_mode(normalized)
            arguments = {"paper_id": paper_id, "mode": mode}
            result = self.tool_provider.call_tool("get_related_papers", arguments)
            papers = _parse_papers(result.get("papers", []))
            edges = _parse_edges(result.get("edges", []))
            payload = FrontendPayload(
                papers=papers,
                edges=edges,
                seedPaperId=paper_id,
                queryContext=f"{mode} papers",
            )
            tool_call = ToolCall(
                name="get_related_papers",
                arguments=arguments,
                summary=result["summary"],
            )
            return AgentResult(
                message=_summarize_papers(
                    papers,
                    empty_message=(
                        f"I could not find related mock papers for `{paper_id}`. "
                        "Try asking for a graph or searching by topic."
                    ),
                ),
                tool_calls=[tool_call],
                frontend_payload=payload,
            )

        if detail_request and not explicit_search:
            paper_id = _extract_paper_id(user_message) or "p-data-agents-2024"
            arguments = {"paper_id": paper_id}
            result = self.tool_provider.call_tool("get_paper_detail", arguments)
            tool_call = ToolCall(
                name="get_paper_detail",
                arguments=arguments,
                summary=result["summary"],
            )
            paper_data = result.get("paper")
            if not paper_data:
                return AgentResult(
                    message=f"I could not find `{paper_id}` in the mock literature set.",
                    tool_calls=[tool_call],
                )
            paper = Paper.model_validate(paper_data)
            return AgentResult(message=_summarize_detail(paper), tool_calls=[tool_call])

        query = _clean_search_query(user_message)
        arguments = {"query": query, "limit": 5}
        result = self.tool_provider.call_tool("search_papers", arguments)
        papers = _parse_papers(result.get("papers", []))
        tool_call = ToolCall(
            name="search_papers",
            arguments=arguments,
            summary=result["summary"],
        )
        payload = FrontendPayload(
            papers=papers,
            edges=[],
            seedPaperId=papers[0].id if papers else None,
            queryContext=query,
        )
        return AgentResult(
            message=_summarize_papers(
                papers,
                empty_message=(
                    "I did not find a close match in the mock paper set. "
                    "Try topics like data agents, graph RAG, citation maps, or polystore."
                ),
            ),
            tool_calls=[tool_call],
            frontend_payload=payload,
        )


class ReactScholarAgent:
    def __init__(
        self,
        tool_provider: ToolProvider,
        llm_client: DeepSeekClient | None = None,
        prompt_provider: PromptProvider | None = None,
        max_steps: int = 6,
    ) -> None:
        self.tool_provider = tool_provider
        self.llm_client = llm_client or DeepSeekClient()
        self.prompt_provider = prompt_provider or PromptProvider()
        self.max_steps = max_steps
        self.fallback_agent = ScholarAgent(
            tool_provider,
            llm_client=self.llm_client,
            prompt_provider=self.prompt_provider,
        )

    def run(
        self,
        conversation_id: str,
        user_message: str,
        history: list[Any],
        *,
        run_id: str = "",
        include_trace: bool = False,
        run_record: Any = None,
    ) -> ChatResponse:
        if not self.llm_client.is_configured:
            return self.fallback_agent.run(
                conversation_id,
                user_message,
                history,
                run_id=run_id,
                include_trace=include_trace,
                run_record=run_record,
            )

        tools = _tools_for_llm(self.tool_provider)
        if not tools:
            return self.fallback_agent.run(
                conversation_id,
                user_message,
                history,
                run_id=run_id,
                include_trace=include_trace,
                run_record=run_record,
            )

        messages = _messages_for_llm(self.prompt_provider.main_agent_prompt(), user_message, history)
        executed_tool_calls: list[ToolCall] = []
        frontend_payload: FrontendPayload | None = None

        for _ in range(self.max_steps):
            if run_record is not None:
                run_record.raise_if_cancelled()
            try:
                llm_result = self.llm_client.complete(messages, tools=tools, tool_choice="auto")
            except TypeError:
                return self.fallback_agent.run(
                    conversation_id,
                    user_message,
                    history,
                    run_id=run_id,
                    include_trace=include_trace,
                    run_record=run_record,
                )
            if run_record is not None:
                run_record.raise_if_cancelled()

            if not llm_result.used_llm or not llm_result.message:
                return self.fallback_agent.run(
                    conversation_id,
                    user_message,
                    history,
                    run_id=run_id,
                    include_trace=include_trace,
                    run_record=run_record,
                )

            assistant_message = _assistant_message_for_history(llm_result.message)
            messages.append(assistant_message)
            tool_calls = _extract_llm_tool_calls(llm_result.message)

            if not tool_calls:
                final_message = llm_result.content.strip()
                if final_message:
                    return ChatResponse(
                        conversationId=conversation_id,
                        runId=run_id,
                        status="completed",
                        message=final_message,
                        toolCalls=executed_tool_calls,
                        frontendPayload=frontend_payload,
                    )
                return self.fallback_agent.run(
                    conversation_id,
                    user_message,
                    history,
                    run_id=run_id,
                    include_trace=include_trace,
                    run_record=run_record,
                )

            for tool_call in tool_calls:
                name = tool_call["name"]
                arguments = tool_call["arguments"]
                tool_call_id = tool_call["id"]
                if run_record is not None:
                    run_record.raise_if_cancelled()
                result = self._execute_tool(name, arguments)
                if run_record is not None:
                    run_record.raise_if_cancelled()
                executed_tool_calls.append(
                    ToolCall(
                        name=name,
                        arguments=arguments,
                        summary=str(result.get("summary", f"{name} completed")),
                    )
                )
                frontend_payload = frontend_payload or _frontend_payload_from_tool_result(
                    name,
                    arguments,
                    result,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "content": _json_for_prompt(_summarize_tool_result_for_prompt(result), max_chars=6000),
                    }
                )

        messages.append(
            {
                "role": "user",
                "content": "Stop calling tools and produce the best final answer from the observations above.",
            }
        )
        final_result = self.llm_client.complete(messages)
        if run_record is not None:
            run_record.raise_if_cancelled()
        if final_result.used_llm and final_result.content:
            return ChatResponse(
                conversationId=conversation_id,
                runId=run_id,
                status="completed",
                message=final_result.content,
                toolCalls=executed_tool_calls,
                frontendPayload=frontend_payload,
            )
        return self.fallback_agent.run(
            conversation_id,
            user_message,
            history,
            run_id=run_id,
            include_trace=include_trace,
            run_record=run_record,
        )

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        known_tool_names = {tool.name for tool in self.tool_provider.list_tools()}
        if name not in known_tool_names:
            return {
                "status": "error",
                "tool": name,
                "error": f"Unknown tool: {name}",
                "data": [],
                "summary": f"{name} is not available",
            }
        try:
            return self.tool_provider.call_tool(name, arguments)
        except Exception as exc:
            return {
                "status": "error",
                "tool": name,
                "error": str(exc),
                "data": [],
                "summary": f"{name} failed",
            }


def _tools_for_llm(tool_provider: ToolProvider) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _normalize_tool_parameters(tool.parameters),
            },
        }
        for tool in tool_provider.list_tools()
    ]


def _normalize_tool_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parameters)
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    normalized.setdefault("additionalProperties", False)
    return normalized


def _messages_for_llm(system_prompt: str, user_message: str, history: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    history_messages = history[-16:] if history else []
    for item in history_messages:
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    if not any(message.get("role") == "user" and message.get("content") == user_message for message in messages):
        messages.append({"role": "user", "content": user_message})
    return messages


def _assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        assistant_message["tool_calls"] = message["tool_calls"]
    return assistant_message


def _extract_llm_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    parsed_tool_calls: list[dict[str, Any]] = []
    raw_tool_calls = message.get("tool_calls") or []
    for index, raw_call in enumerate(raw_tool_calls):
        function = raw_call.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
        except (TypeError, ValueError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        parsed_tool_calls.append(
            {
                "id": raw_call.get("id") or f"tool_call_{index}",
                "name": name,
                "arguments": arguments,
            }
        )
    return parsed_tool_calls


def _summarize_tool_result_for_prompt(result: dict[str, Any]) -> dict[str, Any]:
    rows = result.get("data", [])
    compact: dict[str, Any] = {
        "status": result.get("status"),
        "tool": result.get("tool"),
        "summary": result.get("summary"),
        "error": result.get("error"),
        "rowCount": result.get("rowCount", len(rows) if isinstance(rows, list) else None),
    }
    if isinstance(rows, list):
        compact["data"] = rows[:10]
    elif rows is not None:
        compact["data"] = rows
    for key in ["papers", "edges", "paper", "seedPaperId"]:
        if key in result:
            compact[key] = result[key]
    return compact


def _frontend_payload_from_tool_result(
    name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> FrontendPayload | None:
    if name == "get_mock_graph":
        papers = _parse_papers(result.get("papers", []))
        edges = _parse_edges(result.get("edges", []))
        return FrontendPayload(
            papers=papers,
            edges=edges,
            seedPaperId=result.get("seedPaperId"),
            queryContext="mock paper graph",
        )
    if name in {"search_papers", "get_related_papers"}:
        papers = _parse_papers(result.get("papers", []))
        edges = _parse_edges(result.get("edges", []))
        return FrontendPayload(
            papers=papers,
            edges=edges,
            seedPaperId=arguments.get("paper_id") or (papers[0].id if papers else None),
            queryContext=str(arguments.get("query") or arguments.get("mode") or name),
        )
    return None


SYSTEM_PROMPT = """You are BigDipper Scholar, a research analysis assistant for scholarly literature and institutions.
Use the provided tool results as your factual grounding.
Do not invent paper counts, author rankings, institution statistics, citations, or database fields.
Do not suggest tools, filters, or parameters that are not present in the provided tool calls.
If tool results are empty or uncertain, say so and suggest a narrower query.
Answer in the user's language.
For database-backed answers, briefly mention which tool result was used.
Keep the answer concise but useful for a research workflow."""

KNOWN_FIELDS = [
    "Machine learning",
    "Computer science",
    "Database",
    "Artificial intelligence",
    "Data mining",
    "Information retrieval",
]


def _plan_database_tools(message: str) -> list[tuple[str, dict[str, Any]]]:
    normalized = message.lower()
    field_name = _extract_field_name(message)
    institution_name = _extract_institution_name(message)
    paper_id = _extract_openalex_work_id(message)

    if _asks_all_fields(normalized):
        return [("get_all_fields", {"limit": 30})]

    if field_name and _asks_field_authors(normalized):
        return [("get_field_top_authors", {"field_name": field_name, "limit": 20})]

    if paper_id and _asks_paper_references(normalized):
        return [("get_paper_references", {"paper_id": paper_id, "limit": 20})]

    if _asks_paper_title_lookup(normalized):
        return [("paper_title_to_id", {"paper_title": _extract_paper_title_query(message), "limit": 10})]

    if field_name and _asks_institution_field_stats(normalized):
        plan: list[tuple[str, dict[str, Any]]] = []
        if institution_name:
            plan.append(("search_institutions", {"institution_name": institution_name, "limit": 5}))
        plan.append(("get_institution_field_stats", {"field_name": field_name, "limit": 20}))
        return plan

    if institution_name or _asks_institution_search(normalized):
        return [
            (
                "search_institutions",
                {"institution_name": institution_name or _extract_loose_institution_query(message), "limit": 10},
            )
        ]

    return []


def _asks_all_fields(message: str) -> bool:
    return any(
        phrase in message
        for phrase in [
            "有哪些学科",
            "有哪些领域",
            "哪些学科",
            "哪些领域",
            "学科领域",
            "all fields",
            "available fields",
            "fields can",
        ]
    )


def _asks_field_authors(message: str) -> bool:
    return any(keyword in message for keyword in ["作者", "学者", "author", "authors", "scholar", "scholars"])


def _asks_institution_field_stats(message: str) -> bool:
    return any(
        keyword in message
        for keyword in ["机构", "大学", "institution", "university", "ranking", "rank", "排名", "统计", "情况"]
    )


def _asks_institution_search(message: str) -> bool:
    return any(keyword in message for keyword in ["机构", "大学", "institution", "university"])


def _asks_paper_references(message: str) -> bool:
    return any(keyword in message for keyword in ["reference", "references", "citation", "citations", "引用", "被引"])


def _asks_paper_title_lookup(message: str) -> bool:
    return any(keyword in message for keyword in ["paper title", "论文标题", "论文题名", "题名", "标题"])


def _extract_field_name(message: str) -> str | None:
    lowered = message.lower()
    for field in KNOWN_FIELDS:
        if field.lower() in lowered:
            return field

    patterns = [
        r"在\s+([A-Za-z][A-Za-z\s-]{1,80}?)\s*领域",
        r"([A-Za-z][A-Za-z\s-]{1,80}?)\s*领域",
        r"in\s+([A-Za-z][A-Za-z\s-]{1,80}?)\s+(?:field|area)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.I)
        if match:
            return " ".join(match.group(1).split())
    return None


def _extract_institution_name(message: str) -> str | None:
    match = re.search(
        r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+){0,6}\s+"
        r"(?:University|Institute|Academy|College))",
        message,
    )
    if match:
        return " ".join(match.group(1).split())
    return None


def _extract_loose_institution_query(message: str) -> str:
    cleaned = re.sub(r"(查询|搜索|查找|机构|大学|institution|university)", " ", message, flags=re.I)
    return " ".join(cleaned.split()) or message


def _extract_openalex_work_id(message: str) -> str | None:
    match = re.search(r"\bW\d{5,}\b", message)
    return match.group(0) if match else None


def _extract_paper_title_query(message: str) -> str:
    cleaned = re.sub(r"(paper title|论文标题|论文题名|题名|标题|查询|搜索|查找)", " ", message, flags=re.I)
    return " ".join(cleaned.split()) or message


def _summarize_tool_results_for_prompt(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized = []
    for result in tool_results:
        rows = result.get("data", [])
        summarized.append(
            {
                "status": result.get("status"),
                "tool": result.get("tool"),
                "summary": result.get("summary"),
                "error": result.get("error"),
                "rowCount": result.get("rowCount", len(rows) if isinstance(rows, list) else 0),
                "data": rows[:10] if isinstance(rows, list) else rows,
            }
        )
    return summarized


def _json_for_prompt(value: Any, max_chars: int = 7000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def _fallback_database_answer(
    user_message: str,
    tool_calls: list[ToolCall],
    tool_results: list[dict[str, Any]],
) -> str:
    chinese = _is_probably_chinese(user_message)
    successful = [result for result in tool_results if result.get("status") == "success"]
    if not successful:
        errors = "; ".join(str(result.get("error", result.get("summary"))) for result in tool_results)
        if chinese:
            return f"我尝试调用 HelmDB 工具，但查询没有成功：{errors}。当前已回退到可解释的规则式回答。"
        return f"I tried to call HelmDB tools, but the query did not succeed: {errors}. I used a deterministic fallback."

    lines: list[str]
    if chinese:
        lines = [f"我调用了 HelmDB 工具：{', '.join(call.name for call in tool_calls)}。"]
    else:
        lines = [f"I used HelmDB tools: {', '.join(call.name for call in tool_calls)}."]

    for result in successful:
        rows = result.get("data", [])
        row_count = result.get("rowCount", len(rows) if isinstance(rows, list) else 0)
        if chinese:
            lines.append(f"- `{result.get('tool')}` 返回 {row_count} 行。")
        else:
            lines.append(f"- `{result.get('tool')}` returned {row_count} rows.")
        if isinstance(rows, list):
            for row in rows[:5]:
                lines.append(f"  - {_compact_row(row)}")
    return "\n".join(lines)


def _sanitize_llm_answer(user_message: str, content: str) -> str:
    cutoff_patterns = [
        r"\n\s*##\s*\d*\.?\s*结论与建议\b",
        r"\n\s*##\s*建议\b",
        r"\n\s*##\s*Suggestions\b",
        r"\n\s*###\s*建议\b",
        r"\n\s*\*\*建议\*\*\s*[:：]",
        r"\n\s*\*\*Suggestions\*\*\s*[:：]",
    ]
    sanitized = content.strip()
    for pattern in cutoff_patterns:
        match = re.search(pattern, sanitized, flags=re.I)
        if match:
            sanitized = sanitized[: match.start()].rstrip()
            break

    if sanitized != content.strip():
        if _is_probably_chinese(user_message):
            sanitized += (
                "\n\n限制：当前真实工具只支持本次已调用的受控 HelmDB 查询；"
                "不支持自由 SQL、按任意 institution_id 过滤领域统计，或真实论文图谱联动。"
            )
        else:
            sanitized += (
                "\n\nLimitation: the current real tools only support the controlled HelmDB "
                "queries already called here; free-form SQL, arbitrary institution_id filters, "
                "and real graph payloads are not enabled."
            )
    return sanitized


def _compact_row(row: dict[str, Any]) -> str:
    preferred_keys = [
        "display_name",
        "institution_name",
        "affiliation_name",
        "author_name",
        "title",
        "paper_count",
        "total_citations",
        "cited_by_count",
        "works_count",
    ]
    parts = [f"{key}: {row[key]}" for key in preferred_keys if key in row and row[key] is not None]
    if not parts:
        parts = [f"{key}: {value}" for key, value in list(row.items())[:5]]
    return "; ".join(parts)


def _is_probably_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _should_use_local_literature_tools(message: str) -> bool:
    normalized = message.lower().strip()
    if not normalized:
        return False
    if (
        _looks_like_search_request(normalized)
        or _looks_like_graph_request(normalized)
        or _looks_like_related_request(normalized)
        or _looks_like_detail_request(normalized)
    ):
        return True
    return _looks_like_paper_title(message)


def _looks_like_paper_title(message: str) -> bool:
    stripped = message.strip()
    if not stripped or len(stripped) > 180:
        return False
    if any(mark in stripped for mark in ["?", "？"]):
        return False
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", stripped)
    if len(words) >= 3:
        return True
    return _is_probably_chinese(stripped) and len(stripped) >= 4


def _looks_like_graph_request(message: str) -> bool:
    return any(keyword in message for keyword in ["graph", "map", "network", "图谱", "关系图"])


def _looks_like_search_request(message: str) -> bool:
    return any(keyword in message for keyword in ["search", "find", "look for", "论文", "搜索", "查找"])


def _looks_like_related_request(message: str) -> bool:
    return any(
        keyword in message
        for keyword in [
            "similar",
            "related",
            "references",
            "cited by",
            "citation",
            "相关",
            "相似",
            "引用",
        ]
    )


def _looks_like_detail_request(message: str) -> bool:
    return any(keyword in message for keyword in ["detail", "abstract", "doi", "详情", "摘要"])


def _related_mode(message: str) -> str:
    if "cited by" in message or "被引" in message:
        return "cited_by"
    if "references" in message or "参考" in message:
        return "references"
    return "similar"


def _extract_paper_id(message: str) -> str | None:
    match = re.search(r"p-[a-z0-9-]+", message.lower())
    return match.group(0) if match else None


def _clean_search_query(message: str) -> str:
    query = re.sub(r"\b(search|find|papers?|about|论文|搜索|查找)\b", " ", message, flags=re.I)
    query = " ".join(query.split())
    return query or message


def _parse_papers(raw_papers: list[dict[str, Any]]) -> list[Paper]:
    return [Paper.model_validate(paper) for paper in raw_papers]


def _parse_edges(raw_edges: list[dict[str, Any]]) -> list[PaperEdge]:
    return [PaperEdge.model_validate(edge) for edge in raw_edges]


def _summarize_papers(papers: list[Paper], empty_message: str) -> str:
    if not papers:
        return empty_message

    lines = ["I found these mock papers:"]
    for paper in papers:
        authors = ", ".join(paper.authors[:2])
        suffix = " et al." if len(paper.authors) > 2 else ""
        lines.append(
            f"- {paper.title} ({paper.year}, {paper.venue}) by {authors}{suffix}; "
            f"{paper.citation_count} citations. Topic: {paper.topic}."
        )
    return "\n".join(lines)


def _summarize_detail(paper: Paper) -> str:
    authors = ", ".join(paper.authors)
    return (
        f"{paper.title} ({paper.year}) was published in {paper.venue} by {authors}. "
        f"It has {paper.citation_count} mock citations. DOI: {paper.doi or 'not available'}. "
        f"Abstract: {paper.abstract}"
    )


def _summarize_graph(papers: list[Paper], edges: list[PaperEdge]) -> str:
    if not papers:
        return "I could not build a mock graph because no papers were available."
    top_titles = ", ".join(paper.title for paper in papers[:3])
    return (
        f"I built a mock literature graph with {len(papers)} papers and {len(edges)} edges. "
        f"Representative papers: {top_titles}."
    )
