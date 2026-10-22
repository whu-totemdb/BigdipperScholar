from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.logging_config import get_logger
from app.models import TraceStep
from app.resource_usage import (
    ResourceSpan,
    extract_llm_model,
    extract_llm_usage,
    finish_resource_span,
    start_resource_span,
)
from app.run_registry import RunRecord


logger = get_logger("bigdipper.agent")


class AgentTraceLogger(BaseCallbackHandler):
    def __init__(
        self,
        conversation_id: str,
        run_record: RunRecord | None = None,
        collect_trace: bool = False,
    ) -> None:
        self.conversation_id = conversation_id
        self.run_record = run_record
        self.collect_trace = collect_trace
        self.trace_steps: list[TraceStep] = []
        self._llm_started_at: list[float] = []
        self._llm_step_ids: list[str] = []
        self._llm_resource_spans: list[ResourceSpan | None] = []
        self._tool_started_at: dict[str, float] = {}
        self._tool_step_ids: dict[str, str] = {}
        self._tool_resource_spans: dict[str, ResourceSpan | None] = {}

    def raise_if_cancelled(self) -> None:
        if self.run_record is not None:
            self.run_record.raise_if_cancelled()

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any) -> None:
        del kwargs
        self.raise_if_cancelled()
        self._llm_started_at.append(time.perf_counter())
        self._llm_resource_spans.append(
            start_resource_span(
                "LLM",
                "llm",
                input_value=messages[-1] if messages else None,
                model=_serialized_model(serialized),
            )
        )
        step_id = self._append_step(
            kind="llm",
            status="running",
            title="Call language model",
            summary="The agent is asking the language model to decide the next response or tool call.",
        )
        self._llm_step_ids.append(step_id)
        logger.info("conversation=%s agent_llm_round=start", self.conversation_id)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        del kwargs
        elapsed = _elapsed(self._llm_started_at.pop() if self._llm_started_at else None)
        finish_resource_span(
            self._llm_resource_spans.pop() if self._llm_resource_spans else None,
            status="success",
            model=extract_llm_model(response),
            usage=extract_llm_usage(response),
        )
        self._complete_step(
            self._llm_step_ids.pop() if self._llm_step_ids else None,
            status="completed",
            summary="The language model returned the next agent step.",
            duration_ms=elapsed,
        )
        logger.info("conversation=%s agent_llm_round=end elapsed_ms=%.2f", self.conversation_id, elapsed)
        self.raise_if_cancelled()

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        del kwargs
        elapsed = _elapsed(self._llm_started_at.pop() if self._llm_started_at else None)
        finish_resource_span(
            self._llm_resource_spans.pop() if self._llm_resource_spans else None,
            status="failed",
        )
        self._complete_step(
            self._llm_step_ids.pop() if self._llm_step_ids else None,
            status="failed",
            summary="The language model call failed.",
            result_summary=str(error)[:500],
            duration_ms=elapsed,
        )
        logger.exception(
            "conversation=%s agent_llm_round=error elapsed_ms=%.2f error=%s",
            self.conversation_id,
            elapsed,
            error,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.raise_if_cancelled()
        tool_name = serialized.get("name", "unknown_tool")
        self._tool_started_at[str(run_id)] = time.perf_counter()
        self._tool_resource_spans[str(run_id)] = start_resource_span(
            tool_name,
            "tool",
            input_value=input_str,
        )
        step_id = self._append_step(
            kind="tool",
            status="running",
            title=f"Run tool {tool_name}",
            summary=f"The agent is calling tool `{tool_name}`.",
            tool_name=tool_name,
            arguments=_parse_arguments(input_str),
        )
        self._tool_step_ids[str(run_id)] = step_id
        logger.info(
            "conversation=%s tool_call=start tool=%s input=%s",
            self.conversation_id,
            tool_name,
            _compact(input_str),
        )

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        elapsed = _elapsed(self._tool_started_at.pop(str(run_id), None))
        finish_resource_span(
            self._tool_resource_spans.pop(str(run_id), None),
            status=_resource_result_status(output),
        )
        self._complete_step(
            self._tool_step_ids.pop(str(run_id), None),
            status="completed",
            summary="The tool call completed.",
            result_summary=_result_summary(output),
            duration_ms=elapsed,
        )
        logger.info(
            "conversation=%s tool_call=end elapsed_ms=%.2f output_preview=%s",
            self.conversation_id,
            elapsed,
            _compact(output),
        )
        self.raise_if_cancelled()

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        elapsed = _elapsed(self._tool_started_at.pop(str(run_id), None))
        finish_resource_span(
            self._tool_resource_spans.pop(str(run_id), None),
            status="failed",
        )
        self._complete_step(
            self._tool_step_ids.pop(str(run_id), None),
            status="failed",
            summary="The tool call failed.",
            result_summary=str(error)[:500],
            duration_ms=elapsed,
        )
        logger.exception(
            "conversation=%s tool_call=error elapsed_ms=%.2f error=%s",
            self.conversation_id,
            elapsed,
            error,
        )

    def add_final_step(self, status: str, summary: str) -> None:
        kind = "final" if status == "completed" else "error"
        self._append_step(
            kind=kind,
            status=status,
            title="Finalize response",
            summary=summary,
            completed_at=_now(),
            duration_ms=0.0,
        )

    def add_cancel_step(self) -> None:
        self._append_step(
            kind="cancel",
            status="cancelled",
            title="Cancel run",
            summary="The run was cancelled before its result was committed to conversation history.",
            completed_at=_now(),
            duration_ms=0.0,
        )

    def _append_step(
        self,
        *,
        kind: str,
        status: str,
        title: str,
        summary: str,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
        duration_ms: float | None = None,
    ) -> str:
        step_id = f"trace_{uuid.uuid4().hex[:12]}"
        if not self.collect_trace:
            return step_id
        now = _now()
        self.trace_steps.append(
            TraceStep(
                id=step_id,
                round=max(1, len(self.trace_steps) + 1),
                kind=kind,
                status=status,
                title=title,
                summary=summary,
                toolName=tool_name,
                arguments=arguments,
                startedAt=now,
                completedAt=completed_at,
                durationMs=duration_ms,
            )
        )
        return step_id

    def _complete_step(
        self,
        step_id: str | None,
        *,
        status: str,
        summary: str,
        result_summary: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        if not self.collect_trace or step_id is None:
            return
        for index, step in enumerate(self.trace_steps):
            if step.id != step_id:
                continue
            self.trace_steps[index] = step.model_copy(
                update={
                    "status": status,
                    "summary": summary,
                    "result_summary": result_summary,
                    "completed_at": _now(),
                    "duration_ms": duration_ms,
                }
            )
            return


def _elapsed(started_at: float | None) -> float:
    if started_at is None:
        return 0.0
    return (time.perf_counter() - started_at) * 1000


def _compact(value: Any, max_chars: int = 1600) -> str:
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            value = str(value)
    value = " ".join(value.split())
    return value[:max_chars] + ("..." if len(value) > max_chars else "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_arguments(input_str: str) -> dict[str, Any]:
    try:
        parsed = json.loads(input_str)
    except Exception:
        return {"input": _compact(input_str, max_chars=300)}
    if not isinstance(parsed, dict):
        return {"input": parsed}
    return _sanitize_arguments(parsed)


def _sanitize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(secret_key in lowered for secret_key in ("key", "token", "password", "secret")):
            sanitized[key] = "<redacted>"
        elif isinstance(value, str):
            sanitized[key] = value[:300]
        else:
            sanitized[key] = value
    return sanitized


def _result_summary(output: Any) -> str:
    text = _compact(output, max_chars=500)
    row_count = _extract_row_count(text)
    if row_count is not None:
        return f"{row_count} rows returned. Preview: {text[:300]}"
    return text


def _extract_row_count(text: str) -> int | None:
    for pattern in ('"row_count":', '"rowCount":', "'row_count':", "'rowCount':"):
        if pattern not in text:
            continue
        tail = text.split(pattern, 1)[1].lstrip()
        digits = []
        for char in tail:
            if char.isdigit():
                digits.append(char)
            elif digits:
                break
        if digits:
            return int("".join(digits))
    return None


def _resource_result_status(output: Any) -> str:
    if isinstance(output, dict):
        return "failed" if str(output.get("status") or "").lower() in {"error", "failed"} else "success"
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except Exception:
            return "success"
        if isinstance(parsed, dict):
            return _resource_result_status(parsed)
    return "success"


def _serialized_model(serialized: dict[str, Any]) -> str | None:
    kwargs = serialized.get("kwargs")
    if not isinstance(kwargs, dict):
        return None
    value = kwargs.get("model") or kwargs.get("model_name")
    return str(value) if value else None
