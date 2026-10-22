from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import ToolSpec


class ToolProvider(ABC):
    @abstractmethod
    def list_tools(self) -> list[ToolSpec]:
        """Return tool metadata for API clients and agent routing."""

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a named tool with JSON-serializable arguments."""
