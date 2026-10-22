from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_env


DEFAULT_DEEPSEEK_BASE_URL = ""
DEFAULT_DEEPSEEK_MODEL = ""


@dataclass(frozen=True)
class LLMResult:
    content: str
    used_llm: bool
    error: str | None = None
    message: dict[str, Any] | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 20.0,
        thinking_enabled: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key or get_env("LLM_API_KEY") or get_env("DEEPSEEK_API_KEY")
        self.base_url = (base_url or get_env("LLM_BASE_URL") or get_env("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or get_env("LLM_MODEL") or get_env("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL
        self.timeout = timeout
        self.thinking_enabled = (
            thinking_enabled
            if thinking_enabled is not None
            else _env_bool("DEEPSEEK_THINKING_DEFAULT", default=False)
        )
        self.reasoning_effort = reasoning_effort or get_env("DEEPSEEK_REASONING_EFFORT") or "high"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResult:
        if not self.is_configured:
            return LLMResult(
                content="DeepSeek is not configured; using deterministic fallback.",
                used_llm=False,
                error="Configure LLM_BASE_URL, LLM_API_KEY and LLM_MODEL",
                model=self.model,
            )

        thinking_enabled = self.thinking_enabled if thinking is None else thinking
        endpoint = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if _env_bool("LLM_SEND_THINKING", default=False):
            payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled and _env_bool("LLM_SEND_REASONING_EFFORT", default=False):
            payload["reasoning_effort"] = reasoning_effort or self.reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            return LLMResult(
                content="DeepSeek call failed; using deterministic fallback.",
                used_llm=False,
                error=str(exc),
                model=self.model,
            )

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            return LLMResult(
                content="DeepSeek returned an unexpected response; using deterministic fallback.",
                used_llm=False,
                error=str(exc),
                model=self.model,
            )

        content = message.get("content") or ""
        usage = data.get("usage")
        return LLMResult(
            content=content.strip(),
            used_llm=True,
            message=message,
            model=str(data.get("model") or self.model),
            usage=usage if isinstance(usage, dict) else None,
        )


def _env_bool(name: str, *, default: bool) -> bool:
    value = get_env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
