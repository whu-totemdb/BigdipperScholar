from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

from app.config import get_env
from app.logging_config import get_logger
from app.models import PaperSearchItem


logger = get_logger("bigdipper.graph_title_mcp")


class McpTitleProvider:
    def __init__(self, variant: str | None = None) -> None:
        self.variant = (variant or get_env("BIGDIPPER_MCP_DATA_VARIANT", "legacy") or "legacy").lower()

    @property
    def enabled(self) -> bool:
        return self.variant == "mmsql"

    def search_title(self, query: str, limit: int = 10) -> tuple[list[PaperSearchItem], list[str]]:
        if not self.enabled:
            return [], []
        module_name = _module_for_variant(self.variant)
        if not module_name:
            return [], [f"Unsupported MCP data variant: {self.variant}"]
        try:
            module = importlib.import_module(module_name)
            raw = asyncio.run(module.papertitle_2_paperid(query))
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.exception("mcp_title_query=error variant=%s error=%s", self.variant, exc)
            return [], [f"MCP {self.variant} papertitle_2_paperid failed: {exc}"]
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message") if isinstance(payload, dict) else "invalid response"
            return [], [f"MCP {self.variant} papertitle_2_paperid returned no usable data: {message}"]
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            return [], [f"MCP {self.variant} papertitle_2_paperid returned malformed data"]
        rows = sorted(rows, key=_title_rank_key, reverse=True)[: max(1, min(limit, 50))]
        return [_paper_from_mcp_row(row, index) for index, row in enumerate(rows) if isinstance(row, dict)], []


def _module_for_variant(variant: str) -> str | None:
    if variant == "mmsql":
        return "app.mcp_servers.cn_queries"
    return None


def _title_rank_key(row: dict[str, Any]) -> tuple[int, int, float]:
    return (
        1 if row.get("exact_normalized_match") else 0,
        _int(row.get("cited_by_count")),
        _float(row.get("rank")),
    )


def _paper_from_mcp_row(row: dict[str, Any], index: int) -> PaperSearchItem:
    year = _int(row.get("year"))
    citations = _int(row.get("cited_by_count") or row.get("citationCount") or row.get("citation_count"))
    title = str(row.get("title") or row.get("display_name") or "Untitled paper")
    label = f"Unknown, {year}" if year else "Unknown"
    return PaperSearchItem(
        id=str(row.get("paperid") or row.get("id") or f"paper_{index}"),
        nodeType="paper",
        label=label,
        title=title,
        year=year,
        authors=[],
        venue=str(row.get("venue") or row.get("doctype") or ""),
        abstract=str(row.get("abstract") or ""),
        doi=row.get("doi"),
        citationCount=citations,
        topic=str(row.get("topic") or ""),
        summary=title,
        metric=float(citations),
        position={"x": index * 140, "y": 0},
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
