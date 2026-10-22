from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import get_env
from app.llm import DeepSeekClient
from app.mcp_client import load_configured_mcp_tools
from app.models import AgentExperimentRequest, AgentExperimentResponse, ResearchStep
from app.resource_usage import finish_resource_span, start_resource_span
from app.evidence_provenance import ground_bundle, has_data
from app.run_registry import RunCancelledError
from app.research_observations import (
    ANALYSIS_TOOL_NAME, ObservationSelection, analysis_schema,
    analysis_tool_definition, record_analyses,
)


ToolLoader = Callable[[str], list[Any]]


REPORT_SELECTION_TOOL_NAME = "select_report_observations"


class AgentExperimentService:
    def __init__(
        self,
        llm_client: Any | None = None,
        tool_loader: ToolLoader | None = None,
        log_path: str | Path | None = None,
        prompt_dir: str | Path | None = None,
        prompt_docs_dir: str | Path | None = None,
    ) -> None:
        model = get_env("BIGDIPPER_AGENT_EXPERIMENT_MODEL") or get_env("DEEPSEEK_MODEL")
        timeout = float(get_env("BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT", "60") or "60")
        self.llm_client = llm_client or DeepSeekClient(model=model, timeout=timeout)
        self.tool_loader = tool_loader or _default_tool_loader
        self.log_path = Path(log_path or get_env("BIGDIPPER_AGENT_EXPERIMENT_LOG_FILE") or "logs/agent_experiments/exploration.jsonl")
        prompt_root = Path(__file__).resolve().parents[1] / "prompts/agent_research_report_flow"
        self.prompt_dir = Path(prompt_dir or get_env("BIGDIPPER_AGENT_PROMPTS_DIR") or prompt_root / "runtime_prompts")
        self.prompt_docs_dir = Path(prompt_docs_dir or get_env("BIGDIPPER_AGENT_PROMPT_DOCS_DIR") or prompt_root / "runtime_docs")

    def run(self, request: AgentExperimentRequest, run_record=None) -> AgentExperimentResponse:
        started = time.perf_counter()
        run_id = request.run_id or f"exp_{secrets.token_hex(8)}"
        trace: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        evidence_bundle: dict[str, Any] = {}
        selected_observation_ids: list[int] = []
        selected_report_observations: list[dict[str, Any]] = []
        metrics = {
            "rounds": 0,
            "toolCalls": 0,
            "failedToolCalls": 0,
            "totalLatencyMs": 0,
        }
        self._log(
            run_id,
            "run_start",
            agentMode=request.agent_mode,
            query=request.query,
            model=getattr(self.llm_client, "model", None),
            researchThinking=_stage_thinking("BIGDIPPER_RESEARCH_THINKING", default=True),
            reportThinking=_stage_thinking("BIGDIPPER_REPORT_THINKING", default=True),
        )

        try:
            tools = self.tool_loader(request.agent_mode)
            self._log(run_id, "tools_loaded", agentMode=request.agent_mode, tools=[getattr(tool, "name", "") for tool in tools])
        except Exception as exc:
            tools = []
            self._log(run_id, "tools_load_failed", status="failed", error=str(exc))

        try:
            if not tools:
                raise RuntimeError("No research tools available. Check the MCP configuration and database settings.")
            observations, research_trace, rounds, selected_observation_ids, selection_rationale = self._run_research(run_id, request, tools, run_record=run_record)
            if run_record is not None:
                run_record.raise_if_cancelled()
            metrics["rounds"] = rounds
            metrics["toolCalls"] = len([item for item in observations if item.get("event") == "tool_call"])
            metrics["failedToolCalls"] = len([item for item in observations if item.get("status") == "failed"])
            metrics["selectedToolCalls"] = len(selected_observation_ids)
            metrics["selectionFinalized"] = True
            metrics["selectionRepairCalls"] = sum(item.get("event") == "selection_repair" for item in research_trace)
            if request.include_trace:
                trace.extend(research_trace)
            selected_report_observations = _build_selected_report_observations(observations, research_trace, selected_observation_ids)
            report = self._generate_report(
                run_id,
                request,
                observations,
                research_trace,
                selected_observation_ids,
                selection_rationale,
            )
            evidence_bundle = self._generate_evidence_bundle(
                run_id,
                request,
                report,
                selected_report_observations,
                selected_observation_ids,
                selection_rationale,
            )
            citations = _parse_citations_from_bundle(evidence_bundle)
            metrics["citations"] = len(citations)
            metrics["evidence"] = len(evidence_bundle.get("evidence") or [])
            metrics["graphIntentNodes"] = len(((evidence_bundle.get("graphIntent") or {}).get("nodes") or [])) if isinstance(evidence_bundle.get("graphIntent"), dict) else 0
            status = "completed"
        except RunCancelledError:
            raise
        except Exception as exc:
            status = "failed"
            report = f"Agent experiment failed: {exc}"
            self._log(run_id, "run_failed", status="failed", error=str(exc))

        metrics["totalLatencyMs"] = round((time.perf_counter() - started) * 1000, 2)
        self._log(run_id, "run_end", status=status, metrics=metrics)
        return AgentExperimentResponse(
            runId=run_id,
            status=status,
            researchSteps=_build_research_steps(observations, selected_observation_ids),
            agentMode=request.agent_mode,
            report=report,
            evidence=evidence_bundle.get("evidence") or citations,
            citations=citations,
            evidenceBundle=evidence_bundle,
            selectedObservations=selected_report_observations,
            metrics=metrics,
            trace=trace if request.include_trace else [],
        )

    def _run_research(
        self,
        run_id: str,
        request: AgentExperimentRequest,
        tools: list[Any],
        run_record=None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[int], str]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _research_prompt(request.agent_mode, _load_prompt_docs(request.agent_mode, self.prompt_docs_dir), self.prompt_dir)},
        ]
        messages.append({"role": "user", "content": request.query})
        tool_defs = [_report_selection_tool_definition(), analysis_tool_definition(), *[_tool_definition(tool) for tool in tools]]
        tool_map = {getattr(tool, "name", ""): tool for tool in tools}
        observations: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        selection = ObservationSelection()

        # Schema comes from the configured MCP service, not a deployment-specific
        # static prompt. It is recorded but excluded from report evidence.
        if "get_database_schema" in tool_map:
            schema_call = {"name": "get_database_schema", "arguments": {}, "id": "schema-bootstrap"}
            schema = self._execute_tool(run_id, 0, 0, tool_map, schema_call)
            observations.append(schema)
            if schema.get("status") != "success":
                raise RuntimeError("MCP schema discovery failed; cannot safely plan database queries.")
            messages.append({"role": "user", "content": "Database schema observation (not report evidence):\n" + _tool_observation_content(schema)})

        for round_number in range(1, request.max_rounds + 1):
            if run_record is not None:
                run_record.raise_if_cancelled()
            result = self.llm_client.complete(
                messages,
                tools=tool_defs or None,
                tool_choice="auto" if tool_defs else None,
                thinking=_stage_thinking("BIGDIPPER_RESEARCH_THINKING", default=True),
                reasoning_effort="high",
            )
            if result.error and not result.used_llm:
                raise RuntimeError(f"Research model failed: {result.error}")
            tool_calls = _extract_tool_calls(result.message)
            trace_event = {
                "round": round_number,
                "event": "llm_response",
                "content": result.content,
                "toolCalls": [{"id": call["id"], "name": call["name"], "arguments": call["arguments"]} for call in tool_calls],
                "usedLlm": result.used_llm,
                "error": result.error,
            }
            trace.append(trace_event)
            self._log(run_id, "llm_response", **trace_event)
            if not tool_calls:
                trace.append({"event": "selection_repair", "reason": "stopped_without_finalization"})
                selected_observation_ids, rationale = self._repair_report_selection(
                    run_id, request, observations, reason="stopped_without_finalization", review_round=round_number + 1,
                )
                return observations, trace, round_number, selected_observation_ids, rationale

            messages.append(result.message or {"role": "assistant", "content": result.content, "tool_calls": []})
            pending_calls: list[tuple[int, dict[str, Any]]] = []
            has_queries = any(call["name"] not in {REPORT_SELECTION_TOOL_NAME, ANALYSIS_TOOL_NAME} for call in tool_calls)
            final_calls = sum(call["name"] == REPORT_SELECTION_TOOL_NAME and call["arguments"].get("mode") == "finalize" for call in tool_calls)
            for call in tool_calls:
                if call["name"] == ANALYSIS_TOOL_NAME:
                    try:
                        analyses = record_analyses(call["arguments"].get("analyses"), observations, round_number)
                        payload = {"status": "success", "analyses": analyses}
                    except ValueError as exc:
                        payload = {"status": "failed", "error": str(exc)}
                    self._log(run_id, "observation_analysis", round=round_number, **payload)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": json.dumps(payload, ensure_ascii=False)})
                    continue
                if call["name"] == REPORT_SELECTION_TOOL_NAME:
                    selected_ids, invalid_ids = _normalize_selected_observation_ids(call["arguments"], observations)
                    rationale = _selection_rationale(call["arguments"])
                    mode = call["arguments"].get("mode", "append")
                    try:
                        if mode not in {"append", "remove", "replace", "finalize"} or selection.finalized:
                            raise ValueError("Invalid mode or selection already finalized")
                        if mode == "finalize" and (has_queries or final_calls != 1 or invalid_ids):
                            raise ValueError("Finalize once, after all queries returned, using only valid data observation IDs")
                        if mode == "finalize":
                            _validate_final_id_list(call["arguments"])
                        analyses = record_analyses(call["arguments"].get("analyses", []), observations, round_number)
                        selection.update(selected_ids, mode, rationale)
                        payload = {"status": "success", "selectedObservationIds": selection.ids,
                                   "invalidObservationIds": invalid_ids, "finalized": selection.finalized}
                    except ValueError as exc:
                        analyses = []
                        payload = {"status": "failed", "error": str(exc), "finalized": False}
                    self._log(
                        run_id,
                        "report_observations_selected",
                        round=round_number,
                        observationIds=selected_ids,
                        cumulativeObservationIds=selection.ids,
                        invalidObservationIds=invalid_ids,
                        rationale=rationale,
                        mode=mode, finalized=selection.finalized, analyses=analyses,
                        status=payload["status"],
                        source="research",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": REPORT_SELECTION_TOOL_NAME,
                            "content": json.dumps(payload, ensure_ascii=False),
                        }
                    )
                else:
                    pending_calls.append((len(observations) + len(pending_calls), call))

            if selection.finalized:
                trace.append({"event": "selection_finalized", "observationIds": selection.ids})
                return observations, trace, round_number, selection.ids, selection.rationale

            if len(pending_calls) > 1:
                # Tool calls emitted in one model response have no ordering
                # dependency. Execute them concurrently, then append results
                # in model order so observation IDs and trace output remain
                # deterministic.
                worker_count = min(
                    len(pending_calls),
                    max(1, int(get_env("BIGDIPPER_AGENT_TOOL_WORKERS", "4") or "4")),
                )
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="agent-tool") as executor:
                    futures = [
                        executor.submit(self._execute_tool, run_id, round_number, observation_id, tool_map, call)
                        for observation_id, call in pending_calls
                    ]
                    results = [future.result() for future in futures]
            else:
                results = [
                    self._execute_tool(run_id, round_number, observation_id, tool_map, call)
                    for observation_id, call in pending_calls
                ]

            for (_, call), metrics in zip(pending_calls, results):
                metrics["actionRationale"] = result.content
                self._log(run_id, "observation_action_context", observationId=metrics["observationId"],
                          round=round_number, actionRationale=result.content)
                observations.append(metrics)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": _tool_observation_content(metrics),
                    }
                )
        trace.append({"event": "selection_repair", "reason": "max_rounds_without_finalization"})
        selected_observation_ids, rationale = self._repair_report_selection(
            run_id, request, observations, reason="max_rounds_without_finalization", review_round=request.max_rounds + 1,
        )
        return observations, trace, request.max_rounds, selected_observation_ids, rationale

    def _execute_tool(
        self,
        run_id: str,
        round_number: int,
        observation_id: int,
        tool_map: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = call["name"]
        arguments = call["arguments"]
        started = time.perf_counter()
        resource_span = start_resource_span(tool_name, "tool", input_value=arguments)
        self._log(run_id, "tool_call_start", round=round_number, observationId=observation_id, toolName=tool_name, arguments=arguments)
        if tool_name not in tool_map:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            event = {
                "event": "tool_call",
                "observationId": observation_id,
                "round": round_number,
                "toolName": tool_name,
                "arguments": arguments,
                "status": "failed",
                "error": f"Unknown tool: {tool_name}",
                "latencyMs": elapsed_ms,
            }
            finish_resource_span(resource_span, status="failed")
            self._log(run_id, "tool_call_end", **event)
            return event
        try:
            output = _invoke_tool(tool_map[tool_name], arguments)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            output_failed = _tool_output_failed(output)
            event = {
                "event": "tool_call",
                "observationId": observation_id,
                "round": round_number,
                "toolName": tool_name,
                "arguments": arguments,
                "status": "failed" if output_failed else "success",
                "latencyMs": elapsed_ms,
            }
            execution = _extract_execution_metadata(output)
            if execution:
                event["execution"] = execution
            if output_failed:
                event["error"] = _tool_error_message(output)
            else:
                event["result"] = _strip_execution_metadata(output)
            finish_resource_span(
                resource_span,
                status="failed" if output_failed else "success",
            )
            self._log(run_id, "tool_call_end", **event)
            return event
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            event = {
                "event": "tool_call",
                "observationId": observation_id,
                "round": round_number,
                "toolName": tool_name,
                "arguments": arguments,
                "status": "failed",
                "error": str(exc),
                "latencyMs": elapsed_ms,
            }
            finish_resource_span(resource_span, status="failed")
            self._log(run_id, "tool_call_end", **event)
            return event

    def _fallback_report_selection(
        self,
        run_id: str,
        observations: list[dict[str, Any]],
        *,
        reason: str,
    ) -> tuple[list[int], str]:
        selected_ids: list[int] = []
        rationale = f"No observations selected because research-agent did not call {REPORT_SELECTION_TOOL_NAME}: {reason}."
        self._log(run_id, "report_observation_selection_fallback", reason=reason, observationIds=selected_ids, rationale=rationale)
        return selected_ids, rationale

    def _repair_report_selection(
        self,
        run_id: str,
        request: AgentExperimentRequest,
        observations: list[dict[str, Any]],
        *,
        reason: str,
        review_round: int,
    ) -> tuple[list[int], str]:
        if not observations or not self.llm_client.is_configured:
            raise RuntimeError("Research selection could not be finalized")

        self._log(
            run_id,
            "report_observation_selection_repair_start",
            reason=reason,
            observationCount=len(observations),
        )
        result = self.llm_client.complete(
            [
                {"role": "system", "content": _selection_repair_prompt(self.prompt_dir)},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "query": request.query,
                            "agentMode": request.agent_mode,
                            "reason": reason,
                            "observations": observations,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            tools=[_report_selection_tool_definition()],
            tool_choice="auto",
            thinking=False,
        )
        tool_calls = _extract_tool_calls(result.message)
        self._log(
            run_id,
            "report_observation_selection_repair_response",
            reason=reason,
            content=result.content,
            toolCalls=[{"id": call["id"], "name": call["name"], "arguments": call["arguments"]} for call in tool_calls],
            usedLlm=result.used_llm,
            error=result.error,
        )
        if len(tool_calls) == 1 and tool_calls[0]["name"] == REPORT_SELECTION_TOOL_NAME:
            arguments = tool_calls[0]["arguments"]
            selected_ids, invalid_ids = _normalize_selected_observation_ids(arguments, observations)
            if arguments.get("mode") == "finalize" and not invalid_ids:
                _validate_final_id_list(arguments)
                analyses = record_analyses(arguments.get("analyses", []), observations, review_round)
                rationale = _selection_rationale(arguments)
                self._log(run_id, "report_observations_selected", source="selection_repair",
                          observationIds=selected_ids, rationale=rationale, mode="finalize",
                          finalized=True, analyses=analyses)
                return selected_ids, rationale
        raise RuntimeError("Research selection was not finalized after one review attempt; draft observations were not used")

    def _generate_report(
        self,
        run_id: str,
        request: AgentExperimentRequest,
        observations: list[dict[str, Any]],
        research_trace: list[dict[str, Any]],
        selected_observation_ids: list[int],
        selection_rationale: str,
    ) -> str:
        selected = _generation_observations(_build_selected_report_observations(observations, research_trace, selected_observation_ids))
        successful_count = len([item for item in observations if item.get("status") == "success"])
        failed_count = len([item for item in observations if item.get("status") == "failed"])
        if not self.llm_client.is_configured:
            report = _fallback_report(request.query, selected)
            self._log(run_id, "report", report=report, usedLlm=False, selectedObservationIds=selected_observation_ids)
            return report
        prompt = {
            "query": request.query,
            "agentMode": request.agent_mode,
            "selection": {
                "observationIds": selected_observation_ids,
                "rationale": selection_rationale,
            },
            "selectedExplorations": selected,
            "observationCounts": {
                "total": len(observations),
                "selected": len(selected),
                "successful": successful_count,
                "failed": failed_count,
            },
        }
        result = self.llm_client.complete(
            [
                {"role": "system", "content": _report_prompt(self.prompt_dir)},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            thinking=_stage_thinking("BIGDIPPER_REPORT_THINKING", default=True),
            reasoning_effort="high",
        )
        if result.used_llm and result.content.strip():
            report = result.content
        else:
            raise RuntimeError(f"Report generation failed: {result.error or 'empty model response'}")
        self._log(
            run_id,
            "report",
            report=report,
            usedLlm=result.used_llm,
            error=result.error,
            selectedObservationIds=selected_observation_ids,
        )
        return report

    def _generate_evidence_bundle(
        self,
        run_id: str,
        request: AgentExperimentRequest,
        report: str,
        selected: list[dict[str, Any]],
        selected_observation_ids: list[int],
        selection_rationale: str,
    ) -> dict[str, Any]:
        # Both generators receive the same full selected observations. Stable
        # evidence IDs let the evidence model annotate facts without changing
        # their values, and deterministic grounding handles model failure.
        evidence_selected = _generation_observations(selected)
        prompt = {
            "query": request.query,
            "agentMode": request.agent_mode,
            "report": report,
            "selection": {
                "observationIds": selected_observation_ids,
                "rationale": selection_rationale,
            },
            "selectedExplorations": evidence_selected,
            "observedEvidence": ground_bundle(selected)["evidence"],
        }
        if not self.llm_client.is_configured:
            bundle = _fallback_evidence_bundle(selected)
            self._log(
                run_id,
                "citation_evidence_bundle_generated",
                shouldRenderGraph=bundle.get("shouldRenderGraph"),
                evidenceCount=len(bundle.get("evidence") or []),
                citationCount=len(bundle.get("citations") or []),
                graphIntentNodeCount=len((bundle.get("graphIntent") or {}).get("nodes") or []) if isinstance(bundle.get("graphIntent"), dict) else 0,
                usedLlm=False,
            )
            return ground_bundle(selected, bundle)

        result = self.llm_client.complete(
            [
                {"role": "system", "content": _citation_prompt(self.prompt_dir)},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            thinking=False,
        )
        bundle = _parse_evidence_bundle(result.content, selected_observation_ids) if result.used_llm else _empty_evidence_bundle()
        if _bundle_is_empty(bundle) and (result.error or selected):
            bundle = _fallback_evidence_bundle(selected)
        self._log(
            run_id,
            "citation_evidence_bundle_generated",
            shouldRenderGraph=bundle.get("shouldRenderGraph"),
            evidenceCount=len(bundle.get("evidence") or []),
            citationCount=len(bundle.get("citations") or []),
            graphIntentNodeCount=len((bundle.get("graphIntent") or {}).get("nodes") or []) if isinstance(bundle.get("graphIntent"), dict) else 0,
            usedLlm=result.used_llm,
            error=result.error,
        )
        return ground_bundle(selected, bundle)


    def _log(self, run_id: str, event_name: str, **payload: Any) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "runId": run_id,
            **payload,
            "event": event_name,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")



def _report_selection_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": REPORT_SELECTION_TOOL_NAME,
            "description": (
                "Manage draft observations with append/remove/replace. At the end, use mode=finalize with the COMPLETE final ID list (including [] if nothing qualifies). "
                "Review validity, task relevance, provenance and new information; exclude superseded/duplicate results. "
                "Finalize only after all queries returned, without queries in the same response. It ends exploration immediately. "
                "Include concise post-result analyses with their explicit observationIds; no separate analysis call is needed at finalization."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["append", "remove", "replace", "finalize"], "default": "append"},
                    "observationIds": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "IDs of useful tool observations to send to the report agent.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Short reason explaining why these observations are useful for the report.",
                    },
                    "analyses": analysis_schema(),
                },
                "required": ["observationIds", "mode"],
                "additionalProperties": False,
            },
        },
    }


def _tool_observation_content(metrics: dict[str, Any]) -> str:
    payload = {
        "observationId": metrics.get("observationId"),
        "round": metrics.get("round"),
        "toolName": metrics.get("toolName"),
        "status": metrics.get("status"),
    }
    if metrics.get("execution"):
        payload["execution"] = metrics["execution"]
    if metrics.get("status") == "success":
        payload["result"] = metrics.get("result", "")
    else:
        payload["error"] = metrics.get("error", "")
    return json.dumps(payload, ensure_ascii=False, default=str)


def _normalize_selected_observation_ids(
    arguments: dict[str, Any],
    observations: list[dict[str, Any]],
) -> tuple[list[int], list[Any]]:
    available_ids = {int(item["observationId"]) for item in observations if "observationId" in item and has_data(item)}
    raw_ids = arguments.get("observationIds", arguments.get("observation_ids", []))
    if raw_ids is None:
        raw_ids = []
    elif isinstance(raw_ids, str):
        parsed = _parse_json_if_possible(raw_ids)
        if isinstance(parsed, list):
            raw_ids = parsed
        elif raw_ids.strip():
            raw_ids = [part.strip() for part in raw_ids.split(",")]
        else:
            raw_ids = []
    elif isinstance(raw_ids, int) and not isinstance(raw_ids, bool):
        raw_ids = [raw_ids]
    elif not isinstance(raw_ids, list):
        raw_ids = []

    selected: list[int] = []
    invalid: list[Any] = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            invalid.append(raw_id)
            continue
        try:
            observation_id = int(raw_id)
        except (TypeError, ValueError):
            invalid.append(raw_id)
            continue
        if observation_id in available_ids and observation_id not in selected:
            selected.append(observation_id)
        else:
            invalid.append(raw_id)
    return selected, invalid


def _selection_rationale(arguments: dict[str, Any]) -> str:
    return str(arguments.get("rationale") or arguments.get("reason") or "")


def _validate_final_id_list(arguments: dict[str, Any]) -> None:
    ids = arguments.get("observationIds")
    if not isinstance(ids, list) or any(type(oid) is not int for oid in ids):
        raise ValueError("Final observationIds must be an explicit integer list, including [] for no evidence")


def _accumulate_selected_observations(
    selected_observation_ids: list[int],
    selection_rationales: dict[int, str],
    new_ids: list[int],
    rationale: str,
) -> None:
    for observation_id in new_ids:
        if observation_id not in selected_observation_ids:
            selected_observation_ids.append(observation_id)
        if rationale:
            selection_rationales[observation_id] = rationale


def _selection_summary(selected_observation_ids: list[int], selection_rationales: dict[int, str]) -> str:
    if len(selected_observation_ids) == 1:
        return selection_rationales.get(selected_observation_ids[0], "")
    parts: list[str] = []
    for observation_id in selected_observation_ids:
        rationale = selection_rationales.get(observation_id, "")
        if rationale:
            parts.append(f"observation {observation_id}: {rationale}")
    return "; ".join(parts)


def _build_observation_previews(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for observation in observations:
        preview = {
            "observationId": observation.get("observationId"),
            "round": observation.get("round"),
            "toolName": observation.get("toolName"),
            "arguments": observation.get("arguments", {}),
            "status": observation.get("status"),
            "latencyMs": observation.get("latencyMs"),
        }
        if observation.get("status") == "success":
            preview["resultPreview"] = _compact_tool_result(observation.get("result"), row_limit=3, max_chars=800)
        else:
            preview["error"] = _truncate_text(str(observation.get("error", "")), 800)
        previews.append(preview)
    return previews


def _selection_repair_prompt(prompt_dir: Path) -> str:
    return _read_prompt_file(prompt_dir / "selection_review.md")


def _public_tool_name(tool_name: str) -> str:
    names = {
        "execute_sciscinet_sql": "Run analytical query",
        "search_papers_by_keywords": "Search papers by keywords",
        "search_papers_by_natural_language": "Search papers by meaning",
        "find_similar_papers_by_paperid": "Find similar papers",
        "get_paper_metadata_batch": "Load paper details",
        "get_institution_id": "Resolve institution",
        "get_all_fields": "Load research fields",
        "get_all_tables": "Inspect data catalog",
        "papertitle_2_paperid": "Resolve paper title",
        "authorname_2_authorid": "Resolve author name",
        "ref_paper": "Explore citing papers",
        "get_paper_author_affiliations_graph": "Explore paper author affiliations",
        "semantic_synthesis_seed_pipeline": "Retrieve related literature",
        "expand_paper_citations_neo4j": "Expand paper citations",
    }
    public_name = names.get(tool_name)
    if public_name:
        return public_name
    return tool_name.replace("_", " ").strip().title() or "Database operation"


def _build_research_steps(
    observations: list[dict[str, Any]],
    selected_observation_ids: list[int],
) -> list[ResearchStep]:
    selected_ids = {int(value) for value in selected_observation_ids}
    steps: list[ResearchStep] = []
    for index, observation in enumerate(observations):
        arguments = observation.get("arguments") if isinstance(observation.get("arguments"), dict) else {}
        execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
        query_text = str(execution.get("queryText") or "").strip()
        if not query_text and isinstance(arguments.get("sql"), str):
            query_text = arguments["sql"].strip()
        query_language = execution.get("queryLanguage")
        if query_text and query_language not in {"sql", "mmsql"}:
            query_language = "mmsql" if re.search(r"\bMATCH\s*\{", query_text, re.IGNORECASE) else "sql"
        row_count = execution.get("rowCount")
        parameters = {key: value for key, value in arguments.items() if key != "sql"}
        status = "success" if observation.get("status") == "success" else "failed"
        if status == "success" and isinstance(row_count, int):
            result_summary = f"Returned {row_count} rows"
        elif status == "success":
            result_summary = "Operation completed"
        else:
            result_summary = "Query failed" if query_text else "Operation failed"
        steps.append(
            ResearchStep(
                id=f"research_step_{observation.get('observationId', index)}",
                round=int(observation.get("round") or 0),
                publicName=_public_tool_name(str(observation.get("toolName") or "")),
                status=status,
                parameters=parameters,
                queryText=query_text or None,
                queryLanguage=query_language if query_language in {"sql", "mmsql"} else None,
                toolLatencyMs=observation.get("latencyMs"),
                databaseLatencyMs=execution.get("databaseLatencyMs"),
                rowCount=row_count if isinstance(row_count, int) else None,
                resultSummary=result_summary,
                selectedForReport=int(observation.get("observationId", index)) in selected_ids,
            )
        )
    return steps


def _build_selected_report_observations(
    observations: list[dict[str, Any]],
    research_trace: list[dict[str, Any]],
    selected_observation_ids: list[int],
) -> list[dict[str, Any]]:
    observations_by_id = {int(item["observationId"]): item for item in observations if "observationId" in item}
    selected_set = {oid for oid in selected_observation_ids if oid in observations_by_id and has_data(observations_by_id[oid])}
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for observation_id in selected_observation_ids:
        if observation_id in seen or observation_id not in observations_by_id:
            continue
        seen.add(observation_id)
        observation = observations_by_id[observation_id]
        if not has_data(observation):
            continue
        item = {
            "observationId": observation_id,
            "round": observation.get("round"),
            "resultAnalyses": [entry for entry in observation.get("resultAnalyses", [])
                               if entry.get("sourceObservationIds")
                               and set(entry["sourceObservationIds"]) <= selected_set],
            "toolName": observation.get("toolName"),
            "arguments": observation.get("arguments", {}),
            "status": observation.get("status"),
            "latencyMs": observation.get("latencyMs"),
            "execution": observation.get("execution", {}),
        }
        if observation.get("status") == "success":
            item["result"] = observation.get("result", "")
        else:
            item["error"] = observation.get("error", "")
        selected.append(item)
    return selected


def _llm_response_for_report(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    return {
        "content": event.get("content", ""),
        "toolCalls": event.get("toolCalls", []),
        "usedLlm": event.get("usedLlm"),
        "error": event.get("error"),
    }


def _generation_observations(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send each joint analysis once, retaining all source IDs and every data row.

    Persisted observations retain their per-observation associations. Only the
    repeated analysis text in generation inputs is deduplicated.
    """
    seen: set[str] = set()
    prepared = []
    for observation in selected:
        item = dict(observation)
        item["resultAnalyses"] = []
        for analysis in observation.get("resultAnalyses", []):
            key = json.dumps(analysis, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                item["resultAnalyses"].append(analysis)
        prepared.append(item)
    return prepared

def _default_tool_loader(agent_mode: str) -> list[Any]:
    tool_mode = (get_env("BIGDIPPER_AGENT_EXPERIMENT_TOOL_MODE", "mcp") or "mcp").lower()
    if tool_mode == "disabled":
        return []
    if agent_mode == "baseline_polystore":
        config = get_env("BIGDIPPER_DB_TOOLS_MCP_CONFIG")
        if not config:
            raise ValueError("The DB_Tools alternative requires its own BIGDIPPER_DB_TOOLS_MCP_CONFIG")
        return load_configured_mcp_tools(config)
    from app.research_skills import skill_tool
    return [*load_configured_mcp_tools(), skill_tool()]


def _task_family_hint_message(task_family: str | None) -> dict[str, str] | None:
    normalized = (task_family or "").strip()
    if not normalized:
        return None
    semantic_hint_enabled = (get_env("BIGDIPPER_AGENT_SEMANTIC_HINT_ENABLED", "true") or "true").strip().lower()
    if semantic_hint_enabled in {"0", "false", "no", "off"}:
        return {
            "role": "system",
            "content": (
                "Benchmark task-family hint: "
                f"task_family=\"{normalized}\". "
                "Use this value only as a high-level task classification. "
                "Only call tools that are present in the current tool list, and keep the user's original query text unchanged."
            ),
        }
    return {
        "role": "system",
        "content": (
            "Benchmark semantic task-family hint: "
            f"task_family=\"{normalized}\". "
            "When calling semantic_synthesis_seed_pipeline, pass this value as task_family unless it clearly conflicts with the user query. "
            "Keep the tool query argument equal to the user's original query text; do not append this hint to the query sent to retrieval."
        ),
    }


def _research_prompt(agent_mode: str, prompt_docs: str, prompt_dir: Path) -> str:
    primary = prompt_dir / ("research_db_tools.md" if agent_mode == "baseline_polystore" else "research_agent.md")
    if primary.exists():
        base = _read_prompt_file(primary)
    else:
        filename = "research_baseline_polystore.md" if agent_mode == "baseline_polystore" else "research_mmdb_scholar.md"
        base = _read_prompt_file(prompt_dir / filename)
    if prompt_docs:
        return f"{base}\n\n## 可用文档与 Skills\n{prompt_docs}"
    return base


def _load_prompt_docs(agent_mode: str, docs_dir: Path) -> str:
    max_chars = int(get_env("BIGDIPPER_AGENT_DOC_MAX_CHARS", "6000") or "6000")
    if agent_mode == "baseline_polystore":
        names = []
    else:
        names = _mmdb_prompt_doc_names(docs_dir)
    sections: list[str] = []
    for name in names:
        path = docs_dir / name
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[truncated]"
        sections.append(f"### {name}\n{content}")
    return "\n\n".join(sections)


def _mmdb_prompt_doc_names(docs_dir: Path) -> list[str]:
    return ["skill_catalog.md"]


def _report_prompt(prompt_dir: Path, task_family: str | None = None) -> str:
    del task_family
    return _read_prompt_file(prompt_dir / "report_agent.md")


def _citation_prompt(prompt_dir: Path, task_family: str | None = None) -> str:
    del task_family
    return _read_prompt_file(prompt_dir / "citation_agent.md")


def _task_prompt_path(prompt_dir: Path, base_name: str, task_family: str | None) -> Path:
    suffix = _safe_task_family_suffix(task_family)
    if suffix:
        candidate = prompt_dir / f"{base_name}_{suffix}.md"
        if candidate.exists():
            return candidate
    return prompt_dir / f"{base_name}.md"


def _safe_task_family_suffix(task_family: str | None) -> str:
    raw = (task_family or "").strip().lower().replace("-", "_")
    if not raw:
        return ""
    safe = "".join(char for char in raw if char.isalnum() or char == "_")
    return safe.strip("_")


def _read_prompt_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Missing agent prompt file: {path}") from exc
    if not content:
        raise RuntimeError(f"Agent prompt file is empty: {path}")
    return content


def _stage_thinking(name: str, *, default: bool) -> bool:
    value = get_env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _tool_definition(tool: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    args_schema = getattr(tool, "args_schema", None)
    if isinstance(args_schema, dict):
        parameters = args_schema
    elif args_schema is not None and hasattr(args_schema, "model_json_schema"):
        parameters = args_schema.model_json_schema()
    return {
        "type": "function",
        "function": {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "parameters": parameters,
        },
    }


def _extract_tool_calls(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not message:
        return []
    calls: list[dict[str, Any]] = []
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        calls.append({"id": raw_call.get("id") or f"call_{len(calls)}", "name": name, "arguments": arguments})
    return calls


def _invoke_tool(tool: Any, arguments: dict[str, Any]) -> Any:
    if hasattr(tool, "invoke"):
        try:
            return tool.invoke(arguments)
        except NotImplementedError:
            if hasattr(tool, "ainvoke"):
                return asyncio.run(tool.ainvoke(arguments))
            raise
        except RuntimeError as exc:
            if "does not support sync invocation" in str(exc) and hasattr(tool, "ainvoke"):
                return asyncio.run(tool.ainvoke(arguments))
            raise
    if hasattr(tool, "ainvoke"):
        return asyncio.run(tool.ainvoke(arguments))
    if callable(tool):
        return tool(**arguments)
    raise RuntimeError(f"Tool {getattr(tool, 'name', '<unknown>')} is not callable")


def _tool_output_failed(output: Any) -> bool:
    payload = _tool_output_payload(output)
    if str(payload.get("status") or "").lower() in {"error", "failed"}:
        return True
    # MCP adapters may return an error content block instead of our JSON envelope.
    def protocol_error(value):
        if isinstance(value, str):
            decoded = _decode_json(value)
            return protocol_error(decoded) if decoded is not None else value.lstrip().startswith("Error calling tool ")
        if isinstance(value, list):
            return any(protocol_error(item) for item in value)
        if isinstance(value, dict):
            return value.get("isError") is True or any(protocol_error(value.get(k)) for k in ("text", "content", "result"))
        return False
    return protocol_error(output)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode_json(value: Any) -> Any | None:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _tool_output_payload(output: Any) -> dict[str, Any]:
    parsed = _decode_json(_stringify(output))
    return _find_tool_payload(parsed)


def _find_tool_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "status" in value or "_execution" in value:
            return value
        for key in ("text", "content", "result"):
            nested = _find_tool_payload(_decode_json(value.get(key)))
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_tool_payload(item)
            if nested:
                return nested
    return {}


def _extract_execution_metadata(output: Any) -> dict[str, Any]:
    payload = _tool_output_payload(output)
    execution = payload.get("_execution")
    if not isinstance(execution, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("backend", "queryLanguage", "queryText", "parameters", "statements",
                "seedQuery", "databaseLatencyMs", "rowCount", "cacheHit"):
        if key in execution:
            normalized[key] = execution[key]
    return normalized


def _tool_error_message(output: Any) -> str:
    payload = _tool_output_payload(output)
    return str(payload.get("message") or payload.get("error") or "Tool execution failed")


def _strip_execution_metadata(output: Any) -> str:
    payload = _tool_output_payload(output)
    if not payload:
        return _stringify(output)
    cleaned = dict(payload)
    cleaned.pop("_execution", None)
    return json.dumps(cleaned, ensure_ascii=False, default=str)


def _prepare_report_observations(
    observations: list[dict[str, Any]],
    *,
    row_limit: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    return [_prepare_report_observation(item, row_limit=row_limit, max_chars=max_chars) for item in observations]


def _prepare_evidence_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound evidence-agent input without discarding the useful paper rows."""
    prepared: list[dict[str, Any]] = []
    for observation in observations:
        item = _prepare_report_observation(observation, row_limit=40, max_chars=12000)
        item["observationId"] = observation.get("observationId")
        item["llmResponse"] = _llm_response_for_report(observation.get("llmResponse"))
        prepared.append(item)
    return prepared


def _prepare_report_observation(
    observation: dict[str, Any],
    *,
    row_limit: int,
    max_chars: int,
) -> dict[str, Any]:
    compact = {
        "round": observation.get("round"),
        "toolName": observation.get("toolName"),
        "arguments": observation.get("arguments", {}),
        "status": observation.get("status"),
        "latencyMs": observation.get("latencyMs"),
    }
    if observation.get("status") == "success":
        compact["resultSummary"] = _compact_tool_result(observation.get("result"), row_limit=row_limit, max_chars=max_chars)
    else:
        compact["error"] = _truncate_text(str(observation.get("error", "")), max_chars)
    return compact


def _compact_tool_result(value: Any, *, row_limit: int, max_chars: int) -> Any:
    parsed = _parse_json_if_possible(value)
    if parsed is not None:
        return _compact_value(parsed, row_limit=row_limit, max_chars=max_chars)
    return _truncate_text(str(value), max_chars)


def _compact_value(value: Any, *, row_limit: int, max_chars: int) -> Any:
    if isinstance(value, str):
        parsed = _parse_json_if_possible(value)
        if parsed is not None:
            return _compact_value(parsed, row_limit=row_limit, max_chars=max_chars)
        return _truncate_text(value, max_chars)
    if isinstance(value, list):
        compact_items = [_compact_value(item, row_limit=row_limit, max_chars=max_chars) for item in value[:row_limit]]
        if len(value) > row_limit:
            compact_items.append({"truncatedItems": len(value) - row_limit})
        return compact_items
    if isinstance(value, dict):
        compact_dict: dict[str, Any] = {}
        for key, item in value.items():
            compact_dict[str(key)] = _compact_value(item, row_limit=row_limit, max_chars=max_chars)
        return compact_dict
    return value


def _parse_json_if_possible(value: Any) -> Any | None:
    if not isinstance(value, str):
        return value if isinstance(value, (dict, list)) else None
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"...[truncated {len(value) - max_chars} chars]"


def _empty_evidence_bundle() -> dict[str, Any]:
    return {
        "shouldRenderGraph": False,
        "reason": "",
        "evidence": [],
        "citations": [],
        "graphIntent": {"title": "Evidence Graph", "nodes": [], "edges": [], "expansion": {"maxNodes": 80}},
        "missingReportEntities": [],
    }


def _bundle_is_empty(bundle: dict[str, Any]) -> bool:
    graph_intent = bundle.get("graphIntent") if isinstance(bundle.get("graphIntent"), dict) else {}
    return not (bundle.get("evidence") or bundle.get("citations") or graph_intent.get("nodes") or graph_intent.get("edges"))


def _parse_evidence_bundle(content: str, selected_observation_ids: list[int]) -> dict[str, Any]:
    parsed = _parse_json_if_possible(content)
    if not isinstance(parsed, dict):
        return _empty_evidence_bundle()
    if isinstance(parsed.get("evidenceBundle"), dict):
        parsed = parsed["evidenceBundle"]
    allowed_observation_ids = set(selected_observation_ids)
    evidence = _normalize_evidence_items(parsed.get("evidence") or parsed.get("evidenceSubset") or [], allowed_observation_ids)
    citations = _normalize_evidence_items(parsed.get("citations") or parsed.get("papers") or [], allowed_observation_ids)
    if not citations:
        citations = [item for item in evidence if item.get("paperId") or item.get("title")]
    graph_intent = _normalize_graph_intent(parsed.get("graphIntent") or parsed.get("graph_intent") or {}, allowed_observation_ids)
    if not graph_intent.get("nodes") and (evidence or citations):
        graph_intent = _graph_intent_from_evidence(citations or evidence, title=_intent_title(parsed))
    return {
        "shouldRenderGraph": bool(parsed.get("shouldRenderGraph") or parsed.get("should_render_graph") or graph_intent.get("nodes")),
        "reason": str(parsed.get("reason") or ""),
        "evidence": evidence[:50],
        "citations": citations[:50],
        "graphIntent": graph_intent,
        "missingReportEntities": parsed.get("missingReportEntities") if isinstance(parsed.get("missingReportEntities"), list) else [],
    }


def _normalize_evidence_items(raw_items: Any, allowed_observation_ids: set[int]) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        source_ids = _filter_source_observation_ids(
            raw.get("sourceObservationIds") or raw.get("source_observation_ids") or raw.get("observationIds"),
            allowed_observation_ids,
        )
        if allowed_observation_ids and not source_ids:
            continue
        item = dict(raw)
        item["evidenceId"] = str(item.get("evidenceId") or item.get("id") or f"ev_{index + 1}")
        item["evidenceType"] = str(item.get("evidenceType") or item.get("type") or ("paper" if item.get("paperId") else "other"))
        item["label"] = str(item.get("label") or item.get("title") or item.get("name") or item["evidenceId"])
        item["sourceObservationIds"] = source_ids
        item["confidence"] = _bounded_confidence(item.get("confidence"))
        items.append(item)
    return items


def _normalize_graph_intent(raw_intent: Any, allowed_observation_ids: set[int]) -> dict[str, Any]:
    if not isinstance(raw_intent, dict):
        raw_intent = {}
    allowed_node_types = {"paper", "author", "institution", "field"}
    allowed_edge_types = {"cites", "reference", "cited_by", "authored_by", "affiliated_with", "coauthored_with", "institution_collaboration", "has_field", "similar"}
    nodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_intent.get("nodes") or []):
        if not isinstance(raw, dict):
            continue
        node_type = str(raw.get("nodeType") or raw.get("node_type") or "").strip()
        if node_type not in allowed_node_types:
            continue
        source_ids = _filter_source_observation_ids(raw.get("sourceObservationIds") or raw.get("source_observation_ids"), allowed_observation_ids)
        if allowed_observation_ids and raw.get("sourceObservationIds") is not None and not source_ids:
            continue
        node = dict(raw)
        node["refId"] = str(node.get("refId") or node.get("ref_id") or f"{node_type}_{index + 1}")
        node["nodeType"] = node_type
        node["id"] = str(node.get("id") or node.get("paperId") or node.get("paper_id") or node.get("label") or "").strip()
        node["label"] = str(node.get("label") or node.get("title") or node.get("id") or node["refId"]).strip()
        node["sourceObservationIds"] = source_ids
        node["confidence"] = _bounded_confidence(node.get("confidence"))
        if node["id"] or node["label"]:
            nodes.append(node)
        if len(nodes) >= 120:
            break
    edges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_intent.get("edges") or []):
        if not isinstance(raw, dict):
            continue
        edge_type = str(raw.get("type") or raw.get("edgeType") or "").strip()
        if edge_type not in allowed_edge_types:
            continue
        source_ids = _filter_source_observation_ids(raw.get("sourceObservationIds") or raw.get("source_observation_ids"), allowed_observation_ids)
        if allowed_observation_ids and raw.get("sourceObservationIds") is not None and not source_ids:
            continue
        edge = dict(raw)
        edge["type"] = edge_type
        edge["sourceObservationIds"] = source_ids
        edge["confidence"] = _bounded_confidence(edge.get("confidence"))
        edge["id"] = str(edge.get("id") or f"intent-edge-{index + 1}")
        edges.append(edge)
        if len(edges) >= 240:
            break
    expansion = raw_intent.get("expansion") if isinstance(raw_intent.get("expansion"), dict) else {}
    return {
        "title": _intent_title(raw_intent),
        "nodes": nodes,
        "edges": edges,
        "expansion": {
            "includePaperDetails": bool(expansion.get("includePaperDetails", True)),
            "includeAuthorDetails": bool(expansion.get("includeAuthorDetails", True)),
            "includeInstitutionDetails": bool(expansion.get("includeInstitutionDetails", True)),
            "includeCitationEdges": bool(expansion.get("includeCitationEdges", True)),
            "maxNodes": int(expansion.get("maxNodes") or 80),
        },
    }


def _filter_source_observation_ids(value: Any, allowed_observation_ids: set[int]) -> list[int]:
    ids = _normalize_int_list(value)
    if not allowed_observation_ids:
        return ids
    return [item for item in ids if item in allowed_observation_ids]


def _intent_title(value: dict[str, Any]) -> str:
    return str(value.get("title") or "Evidence Graph")


def _graph_intent_from_evidence(items: list[dict[str, Any]], title: str = "Evidence Graph") -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(items[:50]):
        paper_id = str(item.get("paperId") or item.get("paper_id") or item.get("id") or "").strip()
        label = str(item.get("title") or item.get("label") or paper_id or "").strip()
        if not paper_id and not label:
            continue
        node_type = str(item.get("nodeType") or item.get("evidenceType") or "paper")
        if node_type not in {"paper", "author", "institution", "field"}:
            node_type = "paper" if paper_id else "field"
        nodes.append(
            {
                "refId": f"{node_type}_{len(nodes) + 1}",
                "nodeType": node_type,
                "id": paper_id or label,
                "label": label or paper_id,
                "role": "evidence",
                "mentionedInReport": True,
                "sourceObservationIds": item.get("sourceObservationIds") or [],
                "confidence": item.get("confidence", 0.5),
            }
        )
    return {"title": title, "nodes": nodes, "edges": [], "expansion": {"maxNodes": 80, "includeCitationEdges": True}}


def _fallback_evidence_bundle(selected: list[dict[str, Any]]) -> dict[str, Any]:
    citations = _fallback_citations(selected)
    evidence: list[dict[str, Any]] = []
    for index, citation in enumerate(citations):
        evidence.append(
            {
                "evidenceId": f"ev_{index + 1}",
                "evidenceType": "paper",
                "label": citation.get("title") or citation.get("paperId") or f"paper_{index + 1}",
                "paperId": citation.get("paperId", ""),
                "title": citation.get("title", ""),
                "supportingClaims": [citation.get("reason", "Selected from research-agent observations.")],
                "sourceObservationIds": citation.get("sourceObservationIds") or [],
                "confidence": citation.get("confidence", 0.5),
            }
        )
    graph_intent = _graph_intent_from_evidence(evidence)
    # Preserve the retrieval path even when the citation model times out.
    # These field nodes act as compact, visible evidence provenance and are
    # connected to the papers returned by the corresponding observation.
    paper_by_observation: dict[int, list[str]] = {}
    for citation in citations:
        for observation_id in _normalize_int_list(citation.get("sourceObservationIds")):
            paper_by_observation.setdefault(observation_id, []).append(str(citation.get("paperId") or ""))
    nodes = graph_intent["nodes"]
    edges = graph_intent["edges"]
    seen_fields: set[str] = set()
    for observation in selected:
        observation_id = int(observation.get("observationId", -1))
        if observation_id < 0:
            continue
        tool_name = str(observation.get("toolName") or "research observation")
        arguments = observation.get("arguments") if isinstance(observation.get("arguments"), dict) else {}
        query_hint = str(arguments.get("query") or arguments.get("venue_key") or arguments.get("venue_keys") or "").strip()
        label = f"检索证据：{tool_name}"
        if query_hint:
            label += f" · {query_hint[:80]}"
        field_id = f"field-observation-{observation_id}"
        if field_id in seen_fields:
            continue
        seen_fields.add(field_id)
        nodes.append(
            {
                "refId": field_id,
                "nodeType": "field",
                "id": field_id,
                "label": label,
                "role": "retrieval_evidence",
                "evidenceType": "retrieval",
                "sourceObservationIds": [observation_id],
                "confidence": 0.7,
            }
        )
        for paper_id in paper_by_observation.get(observation_id, []):
            if paper_id:
                edges.append(
                    {
                        "id": f"edge-{field_id}-{paper_id}",
                        "sourceRef": field_id,
                        "targetRef": paper_id,
                        "type": "has_field",
                        "label": "retrieved",
                        "sourceObservationIds": [observation_id],
                        "confidence": 0.7,
                    }
                )
    return {
        "shouldRenderGraph": bool(graph_intent.get("nodes")),
        "reason": "Fallback EvidenceBundle preserved paper evidence and retrieval provenance from selected observations.",
        "evidence": evidence,
        "citations": citations,
        "graphIntent": graph_intent,
        "missingReportEntities": [],
    }


def _parse_citations_from_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    raw = bundle.get("citations") or bundle.get("evidence") or []
    citations: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paperId") or item.get("paper_id") or item.get("id") or "").strip()
        title = str(item.get("title") or item.get("label") or "").strip()
        if not paper_id and not title:
            continue
        citations.append(
            {
                "paperId": paper_id,
                "title": title,
                "reason": str(item.get("reason") or item.get("supportingClaims") or "Selected as report evidence."),
                "sourceObservationIds": _normalize_int_list(item.get("sourceObservationIds")),
                "confidence": _bounded_confidence(item.get("confidence")),
            }
        )
    return citations[:50]


def _parse_citations(content: str) -> list[dict[str, Any]]:
    parsed = _parse_json_if_possible(content)
    if isinstance(parsed, dict):
        raw_items = parsed.get("evidence") or parsed.get("evidenceSubset") or parsed.get("citations") or parsed.get("papers") or []
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = []
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paperId") or item.get("paper_id") or item.get("paperid") or "").strip()
        title = str(item.get("title") or item.get("displayName") or item.get("display_name") or "").strip()
        evidence_type = str(item.get("evidenceType") or item.get("type") or ("paper" if paper_id or title else "data_subset")).strip()
        label = str(item.get("label") or title or item.get("name") or item.get("description") or f"evidence_{index + 1}").strip()
        data = item.get("data", item.get("payload", item.get("value")))
        reason = str(item.get("reason") or item.get("supporting_claim") or item.get("supportingClaim") or "").strip()
        if not any([paper_id, title, label, data, reason]):
            continue
        dedupe_key = paper_id or title.lower() or f"{evidence_type}:{label}:{json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)[:300]}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        citation: dict[str, Any] = {
            "evidenceType": evidence_type,
            "label": label,
            "paperId": paper_id,
            "title": title,
            "reason": reason,
            "confidence": _bounded_confidence(item.get("confidence")),
            "sourceObservationIds": _normalize_int_list(item.get("sourceObservationIds") or item.get("source_observation_ids") or item.get("observationIds")),
        }
        if data is not None:
            citation["data"] = data
        for key, value in item.items():
            if key not in citation and key not in {"paper_id", "paperid", "displayName", "display_name", "supporting_claim", "supportingClaim", "source_observation_ids", "observationIds", "payload", "value"}:
                citation[key] = value
        citations.append(citation)
    return citations[:50]


def _bounded_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return round(min(1.0, max(0.0, confidence)), 4)


def _normalize_int_list(value: Any) -> list[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, str):
        parsed = _parse_json_if_possible(value)
        if isinstance(parsed, list):
            value = parsed
        elif value.strip():
            value = [part.strip() for part in value.split(",")]
        else:
            value = []
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _fallback_citations(selected: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for observation in selected:
        candidates = _extract_paper_like_items(observation.get("result"))
        for item in candidates:
            paper_id = str(item.get("paperId") or item.get("paper_id") or item.get("paperid") or item.get("id") or "").strip()
            title = str(item.get("title") or item.get("display_name") or item.get("displayName") or "").strip()
            if not paper_id and not title:
                continue
            key = paper_id or title.lower()
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                {
                    "paperId": paper_id,
                    "title": title,
                    "reason": "Selected from research-agent observations as relevant report context.",
                    "confidence": 0.5,
                    "sourceObservationIds": _normalize_int_list(observation.get("observationId")),
                }
            )
            if len(citations) >= limit:
                return citations
    return citations


def _extract_paper_like_items(value: Any) -> list[dict[str, Any]]:
    parsed = _parse_json_if_possible(value)
    items: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if len(items) >= 50:
            return
        if isinstance(node, str):
            nested = _parse_json_if_possible(node)
            if nested is not None:
                walk(nested)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        if any(key in node for key in ("paperId", "paper_id", "paperid", "id")) and any(key in node for key in ("title", "display_name", "displayName")):
            items.append(node)
        for child in node.values():
            walk(child)

    walk(parsed)
    return items


def _fallback_report(
    query: str,
    selected: list[dict[str, Any]],
    report_error: str | None = None,
) -> str:
    lines = [
        "我是BigDipper Scholar。",
        "",
        "## 查询任务",
        query,
        "",
        "## 探索摘要",
        f"本次 research-agent 选择了 {len(selected)} 条探索结果用于报告生成。",
    ]
    if report_error:
        lines.extend(["", "## 报告生成状态", f"报告模型调用失败：{report_error}。以下为后端根据 research-agent 选择的探索结果生成的自动摘要报告。"])
    if selected:
        lines.extend(["", "## 可用发现"])
        for item in selected[:10]:
            value = item.get("result") if item.get("status") == "success" else item.get("error")
            lines.append(f"- observation {item.get('observationId')} / {item.get('toolName')}: {_short_report_item(value)}")
    else:
        lines.extend(["", "## 可用发现", "research-agent 没有选择可用于报告生成的工具结果。"])
    lines.extend(["", "## 说明", "完整查询、返回结果和分析来源保存在研究过程记录中。"])
    return "\n".join(lines)


def _short_report_item(value: Any, limit: int = 700) -> str:
    if value is None:
        return "无结构化结果摘要"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return _truncate_text(text, limit)
