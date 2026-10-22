from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.tools import StructuredTool

from app.logging_config import get_logger
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider


logger = get_logger("bigdipper.tools")


def build_scholar_tools() -> list[StructuredTool]:
    provider = HybridToolProvider([HelmDBToolProvider(), MockToolProvider()])

    def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started_at = time.perf_counter()
        logger.info("langchain_tool=start tool=%s arguments=%s", name, _json(arguments))
        try:
            result = provider.call_tool(name, arguments)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.exception("langchain_tool=error tool=%s elapsed_ms=%.2f error=%s", name, elapsed_ms, exc)
            return {"status": "error", "tool": name, "error": str(exc), "data": [], "summary": f"{name} failed"}
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "langchain_tool=end tool=%s elapsed_ms=%.2f summary=%s preview=%s",
            name,
            elapsed_ms,
            result.get("summary"),
            _json(_preview_result(result)),
        )
        return result

    def get_all_fields(limit: int = 20) -> dict[str, Any]:
        """List HelmDB scholarly fields with level, works count, and cited-by count."""
        return call_tool("get_all_fields", {"limit": limit})

    def search_institutions(institution_name: str, limit: int = 20) -> dict[str, Any]:
        """Search HelmDB institutions by English institution name."""
        return call_tool("search_institutions", {"institution_name": institution_name, "limit": limit})

    def get_institution_field_stats(field_name: str, limit: int = 20) -> dict[str, Any]:
        """Rank institutions by output and citation stats in a HelmDB field."""
        return call_tool("get_institution_field_stats", {"field_name": field_name, "limit": limit})

    def get_field_top_authors(field_name: str, limit: int = 20) -> dict[str, Any]:
        """Rank authors by citations in a HelmDB field."""
        return call_tool("get_field_top_authors", {"field_name": field_name, "limit": limit})

    def paper_title_to_id(paper_title: str, limit: int = 10) -> dict[str, Any]:
        """Search paper IDs by title keyword in HelmDB."""
        return call_tool("paper_title_to_id", {"paper_title": paper_title, "limit": limit})

    def get_paper_references(paper_id: str, limit: int = 20) -> dict[str, Any]:
        """Find papers that cite a HelmDB paper ID."""
        return call_tool("get_paper_references", {"paper_id": paper_id, "limit": limit})

    def search_papers(query: str, limit: int = 5) -> dict[str, Any]:
        """Search mock scholarly papers by title, abstract, author, venue, or topic."""
        return call_tool("search_papers", {"query": query, "limit": limit})

    def get_paper_detail(paper_id: str) -> dict[str, Any]:
        """Return detailed metadata for one mock paper."""
        return call_tool("get_paper_detail", {"paper_id": paper_id})

    def get_related_papers(paper_id: str, mode: str = "similar") -> dict[str, Any]:
        """Return related mock papers for a seed paper."""
        return call_tool("get_related_papers", {"paper_id": paper_id, "mode": mode})

    def get_mock_graph(seed_paper_id: str | None = None) -> dict[str, Any]:
        """Return a small paper graph for frontend visualization."""
        arguments: dict[str, Any] = {}
        if seed_paper_id:
            arguments["seed_paper_id"] = seed_paper_id
        return call_tool("get_mock_graph", arguments)

    return [
        StructuredTool.from_function(get_all_fields),
        StructuredTool.from_function(search_institutions),
        StructuredTool.from_function(get_institution_field_stats),
        StructuredTool.from_function(get_field_top_authors),
        StructuredTool.from_function(paper_title_to_id),
        StructuredTool.from_function(get_paper_references),
        StructuredTool.from_function(search_papers),
        StructuredTool.from_function(get_paper_detail),
        StructuredTool.from_function(get_related_papers),
        StructuredTool.from_function(get_mock_graph),
    ]


def _preview_result(result: dict[str, Any]) -> dict[str, Any]:
    preview = dict(result)
    if isinstance(preview.get("data"), list):
        preview["data"] = preview["data"][:10]
    if isinstance(preview.get("papers"), list):
        preview["papers"] = preview["papers"][:10]
    if isinstance(preview.get("edges"), list):
        preview["edges"] = preview["edges"][:10]
    return preview


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
