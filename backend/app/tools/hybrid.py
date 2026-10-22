from __future__ import annotations

from typing import Any

from app.models import ToolSpec
from app.tools.base import ToolProvider


class HybridToolProvider(ToolProvider):
    def __init__(self, providers: list[ToolProvider]) -> None:
        self.providers = providers

    def list_tools(self) -> list[ToolSpec]:
        tools: list[ToolSpec] = []
        for provider in self.providers:
            tools.extend(provider.list_tools())
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        for provider in self.providers:
            if any(tool.name == name for tool in provider.list_tools()):
                return provider.call_tool(name, arguments)
        raise ValueError(f"Unknown tool: {name}")
