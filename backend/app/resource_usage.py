from __future__ import annotations

import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator, Mapping

from app.models import (
    ResourceStatus,
    ResourceTraceStep,
    ResourceType,
    ResourceUsage,
    ResourceUsageModule,
    ResourceUsageSummary,
    RunStatus,
)


_active_collector: contextvars.ContextVar[ResourceUsageCollector | None] = contextvars.ContextVar(
    "bigdipper_resource_usage_collector",
    default=None,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ResourceSpan:
    step_id: str
    name: str
    type: ResourceType
    started_at: datetime
    started_perf: float
    input_preview: str | None = None
    model: str | None = None
    backend: str | None = None


class ResourceUsageCollector:
    def __init__(
        self,
        request_id: str,
        conversation_id: str,
        *,
        started_at: datetime | None = None,
        started_perf: float | None = None,
        started_cpu: float | None = None,
    ) -> None:
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.started_at = started_at or _now()
        self.started_perf = started_perf if started_perf is not None else time.perf_counter()
        self.started_cpu = started_cpu if started_cpu is not None else time.process_time()
        self._steps: list[ResourceTraceStep] = []
        self._lock = Lock()

    def start_span(
        self,
        name: str,
        resource_type: ResourceType,
        *,
        input_value: Any = None,
        model: str | None = None,
        backend: str | None = None,
    ) -> ResourceSpan:
        return ResourceSpan(
            step_id=f"resource_{uuid.uuid4().hex[:12]}",
            name=name,
            type=resource_type,
            started_at=_now(),
            started_perf=time.perf_counter(),
            input_preview=_preview(input_value),
            model=model,
            backend=backend,
        )

    def finish_span(
        self,
        span: ResourceSpan,
        *,
        status: ResourceStatus,
        model: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        finished_at = _now()
        prompt_tokens, completion_tokens, total_tokens = _token_usage(usage)
        step = ResourceTraceStep(
            stepId=span.step_id,
            name=span.name,
            type=span.type,
            startedAt=span.started_at,
            finishedAt=finished_at,
            latencyMs=round((time.perf_counter() - span.started_perf) * 1000, 2),
            status=status,
            inputPreview=span.input_preview,
            model=model or span.model,
            backend=span.backend,
            promptTokens=prompt_tokens,
            completionTokens=completion_tokens,
            totalTokens=total_tokens,
        )
        with self._lock:
            self._steps.append(step)

    def finish(self, status: RunStatus) -> ResourceUsage:
        finished_at = _now()
        latency_ms = round((time.perf_counter() - self.started_perf) * 1000, 2)
        cpu_percent = None
        if latency_ms > 0:
            cpu_percent = round(max(0.0, time.process_time() - self.started_cpu) * 100_000 / latency_ms, 2)
        with self._lock:
            steps = sorted(self._steps, key=lambda item: item.started_at)

        llm_steps = _steps_of_type(steps, "llm")
        tool_steps = _steps_of_type(steps, "tool")
        database_steps = _steps_of_type(steps, "database")
        graph_steps = _steps_of_type(steps, "graph")
        vector_steps = _steps_of_type(steps, "vector")
        prompt_tokens = _sum_optional(step.prompt_tokens for step in llm_steps)
        completion_tokens = _sum_optional(step.completion_tokens for step in llm_steps)
        total_tokens = _sum_optional(step.total_tokens for step in llm_steps)

        summary = ResourceUsageSummary(
            llmCalls=len(llm_steps),
            toolCalls=len(tool_steps),
            dbQueries=len(database_steps),
            postgresQueries=len([step for step in database_steps if step.backend in {"postgresql", "gaussdb"}]),
            sqliteQueries=len([step for step in database_steps if step.backend == "sqlite"]),
            graphQueries=len(graph_steps),
            vectorQueries=len(vector_steps),
            embeddingCalls=len(_steps_of_type(steps, "embedding")),
            rerankCalls=len(_steps_of_type(steps, "rerank")),
            pdfCalls=len(_steps_of_type(steps, "pdf")),
            visionCalls=len(_steps_of_type(steps, "vision")),
            promptTokens=prompt_tokens,
            completionTokens=completion_tokens,
            totalTokens=total_tokens,
            cpuPercent=cpu_percent,
            memoryMb=_process_memory_mb(),
            peakMemoryMb=None,
        )
        visible_steps = [step for step in steps if _is_visible_trace_step(step)]
        should_display = any(_is_business_resource_step(step) for step in steps)
        modules = _modules(visible_steps)
        modules.append(
            ResourceUsageModule(
                name="System",
                type="system",
                calls=1,
                latencyMs=latency_ms,
                status=_resource_status(status),
            )
        )
        return ResourceUsage(
            requestId=self.request_id,
            conversationId=self.conversation_id,
            messageId=None,
            status=status,
            startedAt=self.started_at,
            finishedAt=finished_at,
            latencyMs=latency_ms,
            shouldDisplay=should_display,
            hiddenReason=None if should_display else "no_external_resource_used",
            summary=summary,
            modules=modules,
            trace=visible_steps,
        )


@contextmanager
def resource_usage_session(collector: ResourceUsageCollector) -> Iterator[ResourceUsageCollector]:
    token = _active_collector.set(collector)
    try:
        yield collector
    finally:
        _active_collector.reset(token)


def start_resource_span(
    name: str,
    resource_type: ResourceType,
    *,
    input_value: Any = None,
    model: str | None = None,
    backend: str | None = None,
) -> ResourceSpan | None:
    collector = _active_collector.get()
    if collector is None:
        return None
    return collector.start_span(
        name,
        resource_type,
        input_value=input_value,
        model=model,
        backend=backend,
    )


def finish_resource_span(
    span: ResourceSpan | None,
    *,
    status: ResourceStatus,
    model: str | None = None,
    usage: Mapping[str, Any] | None = None,
) -> None:
    collector = _active_collector.get()
    if collector is not None and span is not None:
        collector.finish_span(span, status=status, model=model, usage=usage)


class InstrumentedLLMClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def complete(self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        model = getattr(self._client, "model", None)
        span = start_resource_span(
            "LLM",
            "llm",
            input_value=_last_message_preview(messages),
            model=model,
        )
        try:
            result = self._client.complete(messages, *args, **kwargs)
        except Exception:
            finish_resource_span(span, status="failed", model=model)
            raise
        used_llm = bool(getattr(result, "used_llm", True))
        finish_resource_span(
            span,
            status="success" if used_llm else "failed",
            model=getattr(result, "model", None) or model,
            usage=getattr(result, "usage", None),
        )
        return result


class InstrumentedToolProvider:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def list_tools(self) -> list[Any]:
        return self._provider.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        span = start_resource_span(name, "tool", input_value=arguments)
        try:
            result = self._provider.call_tool(name, arguments)
        except Exception:
            finish_resource_span(span, status="failed")
            raise
        finish_resource_span(span, status=_result_status(result))
        return result


def extract_llm_usage(response: Any) -> dict[str, Any] | None:
    candidates: list[Any] = [
        getattr(response, "llm_output", None),
        getattr(response, "usage_metadata", None),
        getattr(response, "response_metadata", None),
    ]
    generations = getattr(response, "generations", None) or []
    for generation_group in generations:
        for generation in generation_group if isinstance(generation_group, list) else [generation_group]:
            message = getattr(generation, "message", None)
            candidates.extend(
                [
                    getattr(message, "usage_metadata", None),
                    getattr(message, "response_metadata", None),
                    getattr(generation, "generation_info", None),
                ]
            )
    for candidate in candidates:
        usage = _find_usage(candidate)
        if usage:
            return usage
    return None


def extract_llm_model(response: Any) -> str | None:
    for candidate in [
        getattr(response, "llm_output", None),
        getattr(response, "response_metadata", None),
    ]:
        if not isinstance(candidate, Mapping):
            continue
        for key in ("model_name", "model", "modelName"):
            value = candidate.get(key)
            if value:
                return str(value)
    return None


def _steps_of_type(steps: list[ResourceTraceStep], resource_type: ResourceType) -> list[ResourceTraceStep]:
    return [step for step in steps if step.type == resource_type]


def _is_visible_trace_step(step: ResourceTraceStep) -> bool:
    return step.type != "system" and not (step.type == "database" and step.backend == "sqlite")


def _is_business_resource_step(step: ResourceTraceStep) -> bool:
    if step.type in {"llm", "system"}:
        return False
    return not (step.type == "database" and step.backend == "sqlite")


def _modules(steps: list[ResourceTraceStep]) -> list[ResourceUsageModule]:
    definitions: list[tuple[str, ResourceType]] = [
        ("LLM", "llm"),
        ("Tools", "tool"),
        ("Database", "database"),
        ("Knowledge Graph", "graph"),
        ("Vector Search", "vector"),
        ("Embeddings", "embedding"),
        ("Rerank", "rerank"),
        ("PDF", "pdf"),
        ("Vision", "vision"),
    ]
    modules: list[ResourceUsageModule] = []
    for name, resource_type in definitions:
        matching = _steps_of_type(steps, resource_type)
        if not matching:
            continue
        modules.append(
            ResourceUsageModule(
                name=name,
                type=resource_type,
                calls=len(matching),
                latencyMs=round(sum(step.latency_ms for step in matching), 2),
                status="failed" if any(step.status == "failed" for step in matching) else "success",
                promptTokens=_sum_optional(step.prompt_tokens for step in matching),
                completionTokens=_sum_optional(step.completion_tokens for step in matching),
                totalTokens=_sum_optional(step.total_tokens for step in matching),
            )
        )
    return modules


def _token_usage(usage: Mapping[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    if not isinstance(usage, Mapping):
        return None, None, None
    nested = usage.get("token_usage")
    if isinstance(nested, Mapping):
        usage = nested
    prompt = _optional_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _optional_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _optional_int(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return prompt, completion, total


def _find_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    prompt, completion, total = _token_usage(value)
    if prompt is not None or completion is not None or total is not None:
        return dict(value)
    for key in ("usage", "token_usage", "usage_metadata"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _find_usage(nested)
            if found:
                return found
    return None


def _sum_optional(values: Iterator[int | None]) -> int | None:
    collected = list(values)
    if not collected or any(value is None for value in collected):
        return None
    return sum(value for value in collected if value is not None)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _result_status(result: Any) -> ResourceStatus:
    if isinstance(result, Mapping):
        return "failed" if str(result.get("status") or "").lower() in {"error", "failed"} else "success"
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except Exception:
            return "success"
        return _result_status(parsed)
    return "success"


def _resource_status(status: RunStatus) -> ResourceStatus:
    if status == "completed":
        return "success"
    return status


def _last_message_preview(messages: list[dict[str, Any]]) -> Any:
    if not messages:
        return None
    message = messages[-1]
    return {"role": message.get("role"), "content": message.get("content")}


def _preview(value: Any, max_chars: int = 300) -> str | None:
    if value is None:
        return None
    sanitized = _sanitize(value)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, default=str)
    except TypeError:
        text = str(sanitized)
    text = " ".join(text.split())
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("password", "secret", "token", "api_key", "apikey")):
                sanitized[str(key)] = "<redacted>"
            else:
                sanitized[str(key)] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:10]]
    if isinstance(value, str):
        return value[:300]
    return value


def _process_memory_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        return None
    return None
