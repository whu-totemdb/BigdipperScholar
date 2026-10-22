from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_env
from app.logging_config import get_logger
from app.graph_neo4j import Neo4jGraphService, Neo4jUnavailable
from app.graph_similar import SimilarPaperService
from app.graph_title_mcp import McpTitleProvider
from app.models import GraphNode, GraphPosition, GraphResponse, PaperSearchItem
from app.tools.base import ToolProvider


MAX_LIMIT = 50
logger = get_logger("bigdipper.graph_demo")


class GraphDemoError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class GraphDemoService:
    def __init__(
        self,
        fallback_provider: ToolProvider | None = None,
        title_provider: McpTitleProvider | None = None,
        neo4j_service: Neo4jGraphService | None = None,
        similar_service: SimilarPaperService | None = None,
        semantic_sqlite_path: str | Path | None = None,
    ) -> None:
        self.fallback_provider = fallback_provider
        self.title_provider = title_provider or McpTitleProvider()
        self.neo4j_service = neo4j_service or Neo4jGraphService()
        self.similar_service = similar_service or SimilarPaperService()
        self.semantic_sqlite_path = Path(semantic_sqlite_path) if semantic_sqlite_path else None
        self._last_neo4j_warning: str | None = None
        self.connection_params = {
            "host": get_env("DB_HOST"),
            "port": int(get_env("DB_PORT", "5432") or "5432"),
            "user": get_env("DB_USER"),
            "password": get_env("DB_PASSWORD"),
            "dbname": get_env("DB_NAME"),
            "options": "-c client_encoding=UTF8 -c statement_timeout=300000",
            "connect_timeout": int(get_env("BIGDIPPER_GRAPH_DB_CONNECT_TIMEOUT", "2") or "2"),
        }

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> tuple[list[PaperSearchItem], list[str]]:
        term = query.strip()
        if not term:
            raise GraphDemoError("invalid_query", "query is required")
        safe_limit = _limit(limit, default=10)
        where = ["title ILIKE %s"]
        params: list[Any] = [f"%{term}%"]
        if year_from is not None:
            where.append("year >= %s")
            params.append(year_from)
        if year_to is not None:
            where.append("year <= %s")
            params.append(year_to)
        params.append(safe_limit)
        sql = f"""
            SELECT paperid, title, year, cited_by_count, doi,
                   public.inverted_index_to_text(abstract_inverted_index) AS abstract
            FROM cn_paperdetails
            WHERE {' AND '.join(where)}
            ORDER BY cited_by_count DESC NULLS LAST, year DESC NULLS LAST
            LIMIT %s;
        """
        result = self._query(sql, tuple(params), "v3_search_papers")
        if result["status"] == "success":
            rows = result["data"]
            if rows:
                items = [self._paper_item(row, index) for index, row in enumerate(rows)]
                self._attach_authors(items)
                return items, []
            return self._fallback_search(term, safe_limit, "PostgreSQL returned no matching rows")
        return self._fallback_search(term, safe_limit, result.get("error"))

    def get_paper(self, paper_id: str) -> tuple[PaperSearchItem, list[str]]:
        pid = paper_id.strip()
        if not pid:
            raise GraphDemoError("invalid_query", "paperId is required")
        result = self._query(
            """
            SELECT paperid, title, year, cited_by_count, doi,
                   public.inverted_index_to_text(abstract_inverted_index) AS abstract
            FROM cn_paperdetails
            WHERE paperid = %s
            LIMIT 1;
            """,
            (pid,),
            "v3_get_paper",
        )
        if result["status"] == "success":
            if result["data"]:
                item = self._paper_item(result["data"][0], 0)
                self._attach_authors([item])
                return item, []
            fallback = self._fallback_detail(pid, "PostgreSQL returned no matching row")
            if fallback:
                return fallback
            raise GraphDemoError("paper_not_found", "Paper not found", {"paperId": pid})
        fallback = self._fallback_detail(pid, result.get("error"))
        if fallback:
            return fallback
        raise GraphDemoError("database_unavailable", "Database unavailable", {"error": result.get("error")})

    def query_graph(
        self,
        query_type: str,
        workspace_id: str | None,
        query: str | None = None,
        seed_paper_id: str | None = None,
        limit: int = 20,
        include_references: bool = True,
        include_citations: bool = True,
        include_authors: bool = False,
        include_institutions: bool = False,
        include_fields: bool = False,
    ) -> GraphResponse:
        safe_limit = _limit(limit)
        if query_type == "paper_search":
            if not query:
                raise GraphDemoError("invalid_query", "query is required for paper_search")
            return self.search_paper_graph(query, workspace_id, safe_limit)

        if query_type == "paper_similar":
            return self.similar_paper_graph(
                query,
                seed_paper_id,
                workspace_id,
                safe_limit,
                include_references=include_references,
                include_citations=include_citations,
            )

        if not seed_paper_id:
            raise GraphDemoError("invalid_query", "seedPaperId is required")

        if query_type == "paper_semantic_graph":
            return self.paper_semantic_graph(seed_paper_id, workspace_id)
        self._last_neo4j_warning = None
        neo4j_graph = self._neo4j_graph_for_query(
            query_type,
            seed_paper_id,
            workspace_id,
            safe_limit,
            include_references=include_references,
            include_citations=include_citations,
            include_authors=include_authors,
            include_institutions=include_institutions,
            include_fields=include_fields,
        )
        if neo4j_graph is not None:
            return neo4j_graph
        seed, seed_warnings = self.get_paper(seed_paper_id)
        warnings = list(seed_warnings)
        if self._last_neo4j_warning:
            warnings.insert(0, self._last_neo4j_warning)
        if query_type == "paper_connections":
            return GraphResponse(
                graphId=_new_graph_id("paper_connections", seed_paper_id),
                workspaceId=workspace_id,
                title=f"Connections: {seed.label}",
                queryContext="Paper connections",
                seedNodeId=seed.id,
                seedPaperId=seed.id,
                defaultViewMode="graph",
                nodes=[_paper_node(seed)],
                papers=[seed],
                edges=[],
                warnings=_dedupe_warnings(warnings or ["HelmDB relationship data is temporarily unavailable; returned the seed paper only."]),
            )
        related: list[PaperSearchItem] = []
        edges: list[dict[str, Any]] = []

        if query_type == "paper_references":
            related, edges, more_warnings = self._paper_relations(seed, "references", safe_limit)
            title = f"References: {seed.label}"
            context = f"References for {seed.label}"
        elif query_type == "paper_citations":
            related, edges, more_warnings = self._paper_relations(seed, "citations", safe_limit)
            title = f"Cited By: {seed.label}"
            context = f"Citations for {seed.label}"
        elif query_type == "paper_neighborhood":
            if include_references:
                refs, ref_edges, ref_warnings = self._paper_relations(seed, "references", safe_limit)
                related.extend(refs)
                edges.extend(ref_edges)
                warnings.extend(ref_warnings)
            if include_citations:
                cites, cite_edges, cite_warnings = self._paper_relations(seed, "citations", safe_limit)
                related.extend(cites)
                edges.extend(cite_edges)
                warnings.extend(cite_warnings)
            title = f"Neighborhood: {seed.label}"
            context = f"References and citations for {seed.label}"
            more_warnings = []
        else:
            raise GraphDemoError("invalid_query", f"Unsupported queryType: {query_type}")
        warnings.extend(more_warnings)

        papers = _dedupe_papers([seed, *related])
        if include_authors or include_institutions:
            nodes, extra_edges = paper_graph_nodes(papers, include_institutions=include_institutions)
            edges.extend(extra_edges)
        else:
            nodes = [_paper_node(paper) for paper in papers]
        if include_authors and not any(node.node_type == "author" for node in nodes):
            warnings.append("Author joins returned no data; paper nodes are still available.")
        if include_institutions and not any(node.node_type == "institution" for node in nodes):
            warnings.append("Institution joins returned no data; paper nodes are still available.")

        return GraphResponse(
            graphId=_new_graph_id(query_type, seed_paper_id),
            workspaceId=workspace_id,
            title=title,
            queryContext=context,
            seedNodeId=seed.id,
            seedPaperId=seed.id,
            defaultViewMode="graph",
            nodes=nodes,
            papers=papers,
            edges=_dedupe_edges(edges),
            warnings=warnings,
        )

    def paper_semantic_graph(self, seed_paper_id: str, workspace_id: str | None) -> GraphResponse:
        paper_id = seed_paper_id.strip()
        seed, warnings = self._semantic_seed_paper(paper_id)
        graph_row, entities, relations, properties, graph_warnings = self._semantic_graph_data(paper_id)
        warnings.extend(graph_warnings)
        if graph_row is None:
            warnings.append("Semantic graph not found for seed paper; returned seed paper only.")
            return GraphResponse(
                graphId=_new_graph_id("paper_semantic_graph", paper_id),
                workspaceId=workspace_id,
                title=f"Semantic Graph: {seed.title}",
                queryContext="Paper semantic graph",
                seedNodeId=seed.id,
                seedPaperId=seed.id,
                defaultViewMode="graph",
                nodes=[_paper_node(seed)],
                papers=[seed],
                edges=[],
                warnings=_dedupe_warnings(warnings),
            )

        return _semantic_graph_response(seed, workspace_id, graph_row, entities, relations, properties, warnings)

    def _semantic_seed_paper(self, paper_id: str) -> tuple[PaperSearchItem, list[str]]:
        if self.semantic_sqlite_path:
            return self._semantic_seed_paper_sqlite(paper_id)
        result = self._query(
            """
            SELECT pd.paperid, pd.title, pd.year, pd.cited_by_count, pd.doi,
                   public.inverted_index_to_text(pd.abstract_inverted_index) AS abstract,
                   COALESCE(src.venue, '') AS venue
            FROM cn_paperdetails pd
            LEFT JOIN paper_kg_sources src ON src.paperid = pd.paperid
            WHERE pd.paperid = %s
            LIMIT 1;
            """,
            (paper_id,),
            "semantic_seed_paper",
        )
        if result["status"] == "success" and result["data"]:
            return self._paper_item(result["data"][0], 0), []
        try:
            return self.get_paper(paper_id)
        except GraphDemoError as exc:
            seed = _paper_from_payload({"id": paper_id, "paperid": paper_id, "title": paper_id}, 0)
            return seed, [f"Seed paper metadata unavailable; returned placeholder. {exc.message}"]

    def _semantic_seed_paper_sqlite(self, paper_id: str) -> tuple[PaperSearchItem, list[str]]:
        if not self.semantic_sqlite_path:
            return _paper_from_payload({"id": paper_id, "title": paper_id}, 0), []
        with sqlite3.connect(self.semantic_sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT paperid, title, year, cited_by_count, doi
                    FROM cn_paperdetails
                    WHERE paperid = ?
                    LIMIT 1
                    """,
                    (paper_id,),
                ).fetchone()
            except sqlite3.DatabaseError:
                row = None
            if row:
                return self._paper_item(dict(row), 0), []
            source = conn.execute(
                """
                SELECT paperid, corpus_id, venue, raw_record
                FROM paper_kg_sources
                WHERE paperid = ? OR corpus_id = ?
                LIMIT 1
                """,
                (paper_id, paper_id),
            ).fetchone()
        if source:
            raw_record = _json_field(source["raw_record"])
            content_source = raw_record.get("content-source") if isinstance(raw_record, dict) else {}
            title_payload = content_source.get("title") if isinstance(content_source, dict) else {}
            return _paper_from_payload(
                {
                    "id": source["paperid"],
                    "paperid": source["paperid"],
                    "title": title_payload.get("value") if isinstance(title_payload, dict) else source["paperid"],
                    "year": content_source.get("pub-date") if isinstance(content_source, dict) else 0,
                    "venue": source["venue"],
                },
                0,
            ), []
        return _paper_from_payload({"id": paper_id, "paperid": paper_id, "title": paper_id}, 0), ["Seed paper metadata unavailable; returned placeholder."]

    def _semantic_graph_data(
        self, paper_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        if self.semantic_sqlite_path:
            return self._semantic_graph_data_sqlite(paper_id)
        graph = self._query(
            "SELECT * FROM paper_semantic_graphs WHERE paperid = %s LIMIT 1;",
            (paper_id,),
            "semantic_graph",
        )
        if graph["status"] != "success":
            return None, [], [], [], [f"Semantic graph query failed: {graph.get('error')}"]
        if not graph["data"]:
            return None, [], [], [], []
        entities = self._query(
            "SELECT * FROM paper_semantic_entities WHERE paperid = %s ORDER BY entity_id;",
            (paper_id,),
            "semantic_entities",
        )
        relations = self._query(
            "SELECT * FROM paper_semantic_relations WHERE paperid = %s ORDER BY relation_id;",
            (paper_id,),
            "semantic_relations",
        )
        properties = self._query(
            "SELECT * FROM paper_semantic_properties WHERE paperid = %s ORDER BY property_id;",
            (paper_id,),
            "semantic_properties",
        )
        warnings = []
        for label, result in [("entities", entities), ("relations", relations), ("properties", properties)]:
            if result["status"] != "success":
                warnings.append(f"Semantic graph {label} query failed: {result.get('error')}")
        return (
            graph["data"][0],
            entities["data"] if entities["status"] == "success" else [],
            relations["data"] if relations["status"] == "success" else [],
            properties["data"] if properties["status"] == "success" else [],
            warnings,
        )

    def _semantic_graph_data_sqlite(
        self, paper_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        if not self.semantic_sqlite_path:
            return None, [], [], [], []
        with sqlite3.connect(self.semantic_sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                graph = conn.execute("SELECT * FROM paper_semantic_graphs WHERE paperid = ? LIMIT 1", (paper_id,)).fetchone()
                if graph is None:
                    return None, [], [], [], []
                entities = conn.execute("SELECT * FROM paper_semantic_entities WHERE paperid = ? ORDER BY entity_id", (paper_id,)).fetchall()
                relations = conn.execute("SELECT * FROM paper_semantic_relations WHERE paperid = ? ORDER BY relation_id", (paper_id,)).fetchall()
                properties = conn.execute("SELECT * FROM paper_semantic_properties WHERE paperid = ? ORDER BY property_id", (paper_id,)).fetchall()
            except sqlite3.DatabaseError as exc:
                return None, [], [], [], [f"Semantic graph query failed: {exc}"]
        return dict(graph), [dict(row) for row in entities], [dict(row) for row in relations], [dict(row) for row in properties], []

    def search_paper_graph(self, query: str, workspace_id: str | None, limit: int = 10) -> GraphResponse:
        term = query.strip()
        if not term:
            raise GraphDemoError("invalid_query", "query is required")
        safe_limit = _limit(limit, default=10)
        mcp_papers, mcp_warnings = self.title_provider.search_title(term, safe_limit)
        if mcp_papers:
            return _paper_search_candidates_graph(term, workspace_id, mcp_papers, mcp_warnings)

        result = self._query(_PAPER_SEARCH_GRAPH_SQL, (f"%{term}%", safe_limit), "v3_search_paper_graph")
        if result["status"] == "success" and result["data"]:
            graph = _graph_response_from_search_rows(term, workspace_id, result["data"])
            return _paper_search_candidates_graph(term, workspace_id, graph.papers, [*mcp_warnings, *graph.warnings])

        papers, warnings = self.search_papers(term, safe_limit)
        return _paper_search_candidates_graph(term, workspace_id, papers, [*mcp_warnings, *warnings])

    def similar_paper_graph(
        self,
        query: str | None,
        seed_paper_id: str | None,
        workspace_id: str | None,
        limit: int = 10,
        *,
        include_references: bool = True,
        include_citations: bool = True,
    ) -> GraphResponse:
        safe_limit = _limit(limit, default=10)
        query_text = (query or "").strip()
        warnings: list[str] = []
        seed: PaperSearchItem | None = None

        if seed_paper_id:
            try:
                seed, seed_warnings = self.get_paper(seed_paper_id)
                warnings.extend(seed_warnings)
                if not query_text:
                    query_text = f"{seed.title} {seed.abstract}".strip()
            except GraphDemoError:
                if not query_text:
                    query_text = seed_paper_id
        if not query_text:
            raise GraphDemoError("invalid_query", "query or seedPaperId is required for paper_similar")

        raw_results, search_warnings = self._similar_search(query_text, safe_limit, seed_paper_id)
        warnings.extend(search_warnings)
        similar_results = _normalize_similar_results(raw_results, seed.id if seed else seed_paper_id, safe_limit)
        related_ids = [item["paper_id"] for item in similar_results]

        details = self._paper_details_by_ids(related_ids)
        related: list[PaperSearchItem] = []
        result_by_id = {item["paper_id"]: item for item in similar_results}
        for index, paper_id in enumerate(related_ids):
            paper = details.get(paper_id)
            if not paper:
                continue
            paper.position = _similar_position(index, len(related_ids))
            related.append(paper)

        if not related:
            warnings.append("No verified similarity results available; no similarity edges were added.")

        papers = _dedupe_papers(([seed] if seed else []) + related)
        if seed:
            seed.position = GraphPosition(x=0, y=0)
        similar_edges: list[dict[str, Any]] = []
        for paper in papers:
            if not seed or paper.id == seed.id:
                continue
            meta = result_by_id.get(paper.id, {"rank": len(similar_edges) + 1, "score": None})
            edge_data: dict[str, Any] = {"rank": meta.get("rank")}
            if meta.get("score") is not None:
                edge_data["score"] = meta.get("score")
            similar_edges.append(
                {
                    "id": f"edge-{seed.id}-{paper.id}-similar",
                    "source": seed.id,
                    "target": paper.id,
                    "type": "similar",
                    "label": "similar",
                    "data": edge_data,
                }
            )

        edges = list(similar_edges)
        if seed and (include_references or include_citations):
            citation_edges, citation_warnings = self._similar_citation_edges([paper.id for paper in papers])
            edges.extend(citation_edges)
            warnings.extend(citation_warnings)

        nodes = [_paper_node(paper) for paper in papers]
        graph_seed = seed.id if seed else (papers[0].id if papers else None)
        title_subject = seed.title or seed.label if seed else query_text[:80]
        return GraphResponse(
            graphId=_new_graph_id("paper_similar", graph_seed or query_text),
            workspaceId=workspace_id,
            title=f"Similar to: {title_subject}",
            queryContext="HelmDB similar paper search",
            seedNodeId=graph_seed,
            seedPaperId=graph_seed,
            defaultViewMode="graph" if similar_edges else "list",
            nodes=nodes,
            papers=papers,
            edges=_dedupe_edges(edges),
            warnings=_dedupe_warnings(warnings),
        )

    def _similar_citation_edges(self, paper_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        started = time.monotonic()
        try:
            edges = self.neo4j_service.citation_edges_among(paper_ids)
            logger.info("similar_citation_edges=end elapsed=%.3f edges=%d", time.monotonic() - started, len(edges))
            return edges, []
        except Exception as exc:
            logger.info("similar_citation_edges=end elapsed=%.3f edges=0 warning=%s", time.monotonic() - started, exc)
            return [], ["Some HelmDB citation relationships are temporarily unavailable; returned the similarity graph only."]

    def _similar_search(self, query_text: str, safe_limit: int, seed_paper_id: str | None) -> tuple[list[Any], list[str]]:
        try:
            return self.similar_service.search(query_text, safe_limit, seed_paper_id=seed_paper_id)
        except TypeError:
            return self.similar_service.search(query_text, safe_limit)

    def _paper_details_by_ids(self, paper_ids: list[str]) -> dict[str, PaperSearchItem]:
        details: dict[str, PaperSearchItem] = {}
        for paper_id in paper_ids:
            try:
                paper, _warnings = self.get_paper(paper_id)
            except GraphDemoError:
                continue
            details[paper.id] = paper
        return details

    def _neo4j_graph_for_query(
        self,
        query_type: str,
        seed_paper_id: str,
        workspace_id: str | None,
        limit: int,
        *,
        include_references: bool,
        include_citations: bool,
        include_authors: bool,
        include_institutions: bool,
        include_fields: bool = False,
    ) -> GraphResponse | None:
        try:
            if query_type == "paper_references":
                graph = self.neo4j_service.paper_references(seed_paper_id, workspace_id, limit)
            elif query_type == "paper_citations":
                graph = self.neo4j_service.paper_citations(seed_paper_id, workspace_id, limit)
            elif query_type == "paper_neighborhood":
                graph = self.neo4j_service.paper_neighborhood(
                    seed_paper_id,
                    workspace_id,
                    limit,
                    include_references=include_references,
                    include_citations=include_citations,
                    include_authors=include_authors,
                    include_institutions=include_institutions,
                    include_fields=include_fields,
                )
            elif query_type == "paper_connections":
                graph = self.neo4j_service.paper_connections(seed_paper_id, workspace_id, min(limit, 10))
            else:
                return None
            if graph.nodes:
                return graph
        except Neo4jUnavailable as exc:
            reason = str(exc) or exc.__class__.__name__
            logger.info("neo4j_graph=fallback query_type=%s paper_id=%s error=%s", query_type, seed_paper_id, reason)
            self._last_neo4j_warning = "HelmDB relationship data is temporarily unavailable; returned the seed paper only." if query_type == "paper_connections" else "HelmDB relationship data is temporarily unavailable; returned fallback graph data."
        return None

    def _fallback_similar(self, query: str, seed_paper_id: str | None, limit: int) -> tuple[list[PaperSearchItem], list[str]]:
        warning = "HelmDB returned fallback similar-paper data."
        if self.fallback_provider and seed_paper_id:
            result = self.fallback_provider.call_tool("get_related_papers", {"paper_id": seed_paper_id, "mode": "similar"})
            papers = [_paper_from_payload(item, index) for index, item in enumerate(result.get("papers", [])[:limit])]
            if papers:
                return papers, [warning]
        papers, warnings = self._fallback_search(query, limit, "similar-paper fallback")
        return papers, [warning, *warnings]

    def _paper_relations(
        self,
        seed: PaperSearchItem,
        mode: str,
        limit: int,
    ) -> tuple[list[PaperSearchItem], list[dict[str, Any]], list[str]]:
        if mode == "references":
            sql = """
                SELECT
                    r.cited_paperid AS paperid,
                    pd.title,
                    pd.year,
                    pd.cited_by_count,
                    pd.doi
                FROM cn_paper_ref r
                JOIN cn_paperdetails pd ON r.cited_paperid = pd.paperid
                WHERE r.citing_paperid = %s
                ORDER BY pd.cited_by_count DESC NULLS LAST
                LIMIT %s;
            """
            label = "v3_paper_references"
        else:
            sql = """
                SELECT
                    r.citing_paperid AS paperid,
                    pd.title,
                    pd.year,
                    pd.cited_by_count,
                    pd.doi
                FROM cn_paper_ref r
                JOIN cn_paperdetails pd ON r.citing_paperid = pd.paperid
                WHERE r.cited_paperid = %s
                ORDER BY pd.cited_by_count DESC NULLS LAST
                LIMIT %s;
            """
            label = "v3_paper_citations"
        result = self._query(sql, (seed.id, limit), label)
        if result["status"] == "success":
            if result["data"]:
                papers = [self._paper_item(row, index + 1) for index, row in enumerate(result["data"])]
                self._attach_authors(papers)
                if mode == "references":
                    edges = [_edge(seed.id, paper.id, "cites", seed.year, paper.year) for paper in papers]
                else:
                    edges = [_edge(paper.id, seed.id, "cites", paper.year, seed.year) for paper in papers]
                return papers, edges, []
            return self._fallback_relations(seed, mode, limit, "PostgreSQL returned no relation rows")
        return self._fallback_relations(seed, mode, limit, result.get("error"))

    def _paper_item(self, row: dict[str, Any], index: int) -> PaperSearchItem:
        paper_id = str(row.get("paperid") or row.get("id") or f"paper_{index}")
        title = str(row.get("title") or "Untitled paper")
        year = _int(row.get("year"))
        citations = _int(row.get("cited_by_count") or row.get("citationCount") or row.get("citation_count"))
        first_author = "Unknown"
        label = f"{first_author}, {year}" if year else first_author
        return PaperSearchItem(
            id=paper_id,
            nodeType="paper",
            label=label,
            title=title,
            year=year,
            authors=[],
            venue=str(row.get("venue") or ""),
            abstract=str(row.get("abstract") or ""),
            doi=row.get("doi"),
            citationCount=citations,
            topic=str(row.get("topic") or ""),
            summary=title,
            metric=float(citations),
            position={"x": index * 140, "y": 0},
        )

    def _attach_authors(self, papers: list[PaperSearchItem]) -> None:
        if not papers:
            return
        ids = [paper.id for paper in papers]
        placeholders = ",".join(["%s"] * len(ids))
        result = self._query(
            f"""
            SELECT paa.paperid, ad.display_name AS author_name
            FROM cn_paper_author_affiliation paa
            JOIN cn_author_details ad ON paa.authorid = ad.authorid
            WHERE paa.paperid IN ({placeholders})
              AND ad.display_name IS NOT NULL
            ORDER BY
                paa.paperid,
                CASE paa.author_position
                    WHEN $$first$$ THEN 0
                    WHEN $$middle$$ THEN 1
                    WHEN $$last$$ THEN 2
                    ELSE 3
                END,
                paa.authorid
            """,
            tuple(ids),
            "v3_paper_authors",
        )
        if result["status"] != "success":
            return
        by_paper: dict[str, list[str]] = {paper.id: [] for paper in papers}
        for row in result["data"]:
            name = str(row.get("author_name") or "").strip()
            pid = str(row.get("paperid") or "")
            if name and pid in by_paper and name not in by_paper[pid]:
                by_paper[pid].append(name)
        for paper in papers:
            if by_paper.get(paper.id):
                paper.authors = by_paper[paper.id]
                first = paper.authors[0]
                paper.label = f"{first}, {paper.year}" if paper.year else first

    def paper_author_affiliations(self, paper_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Return author and institution facts for a bounded evidence paper set."""
        ids = [str(item).strip() for item in paper_ids if str(item).strip()]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        result = self._query(
            f"""
            SELECT paa.paperid, paa.authorid,
                   COALESCE(ad.display_name, paa.authorid) AS author_name,
                   paa.institutionid, aff.affiliation_name AS institution_name,
                   paa.author_position
            FROM cn_paper_author_affiliation paa
            LEFT JOIN cn_author_details ad ON ad.authorid = paa.authorid
            LEFT JOIN cn_affiliations aff ON aff.institution_id = paa.institutionid
            WHERE paa.paperid IN ({placeholders})
            ORDER BY paa.paperid, paa.author_position, paa.authorid
            """,
            tuple(ids),
            "v3_paper_author_affiliations",
        )
        if result["status"] != "success":
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in ids}
        for row in result["data"]:
            paper_id = str(row.get("paperid") or "")
            if paper_id in grouped:
                grouped[paper_id].append(row)
        return grouped

    def _query(self, sql: str, params: tuple[Any, ...], label: str) -> dict[str, Any]:
        missing = [name for name in ["host", "user", "password", "dbname"] if not self.connection_params.get(name)]
        if missing:
            return {"status": "error", "error": f"Missing DB configuration: {', '.join(missing)}", "data": []}
        start = time.time()
        try:
            with psycopg2.connect(**self.connection_params) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(sql, params)
                    rows = [_jsonable(dict(row)) for row in cursor.fetchall()]
        except Exception as exc:
            logger.exception("graph_db_query=error label=%s error=%s", label, str(exc))
            return {"status": "error", "error": str(exc) or exc.__class__.__name__, "data": []}
        logger.info("graph_db_query=end label=%s elapsed=%.3f rows=%d", label, time.time() - start, len(rows))
        return {"status": "success", "data": rows}

    def _fallback_search(self, query: str, limit: int, error: str | None) -> tuple[list[PaperSearchItem], list[str]]:
        if not self.fallback_provider:
            raise GraphDemoError("database_unavailable", "Database unavailable", {"error": error})
        result = self.fallback_provider.call_tool("search_papers", {"query": query, "limit": limit})
        papers = [_paper_from_payload(item, index) for index, item in enumerate(result.get("papers", []))]
        return papers, ["HelmDB returned fallback literature search data."]

    def _fallback_detail(self, paper_id: str, error: str | None) -> tuple[PaperSearchItem, list[str]] | None:
        if not self.fallback_provider:
            return None
        result = self.fallback_provider.call_tool("get_paper_detail", {"paper_id": paper_id})
        payload = result.get("paper")
        if not payload:
            return None
        return _paper_from_payload(payload, 0), ["HelmDB returned fallback paper details."]

    def _fallback_relations(
        self,
        seed: PaperSearchItem,
        mode: str,
        limit: int,
        error: str | None,
    ) -> tuple[list[PaperSearchItem], list[dict[str, Any]], list[str]]:
        if not self.fallback_provider:
            raise GraphDemoError("database_unavailable", "Database unavailable", {"error": error})
        mock_mode = "references" if mode == "references" else "cited_by"
        result = self.fallback_provider.call_tool("get_related_papers", {"paper_id": seed.id, "mode": mock_mode})
        papers = [_paper_from_payload(item, index + 1) for index, item in enumerate(result.get("papers", [])[:limit])]
        edges = []
        for edge in result.get("edges", []):
            edge_type = "cites" if edge.get("type") in {"cites", "reference", "cited_by"} else edge.get("type", "similar")
            edges.append({"id": edge.get("id") or f"edge-{edge.get('source')}-{edge.get('target')}", "source": edge.get("source"), "target": edge.get("target"), "type": edge_type, "label": edge_type})
        return papers, edges, ["HelmDB returned fallback relationship data."]


def _paper_search_candidates_graph(
    term: str,
    workspace_id: str | None,
    papers: list[PaperSearchItem],
    warnings: list[str],
) -> GraphResponse:
    return GraphResponse(
        graphId=_new_graph_id("search", term),
        workspaceId=workspace_id,
        title=f"Search: {term}",
        queryContext="Paper title search",
        seedNodeId=None,
        seedPaperId=None,
        defaultViewMode="list",
        nodes=[_paper_node(paper) for paper in papers],
        papers=papers,
        edges=[],
        warnings=_dedupe_warnings(warnings),
    )


_PAPER_SEARCH_GRAPH_SQL = """
WITH matched_papers AS (
    SELECT paperid, title, display_name, year, cited_by_count, doi, doctype, language, abstract_inverted_index
    FROM cn_paperdetails
    WHERE title ILIKE %s
    ORDER BY cited_by_count DESC NULLS LAST, year DESC NULLS LAST
    LIMIT %s
), author_institution_rows AS (
    SELECT
        mp.paperid,
        paa.authorid,
        COALESCE(ad.display_name, paa.authorid) AS author_name,
        paa.author_position,
        CASE paa.author_position WHEN $$first$$ THEN 0 WHEN $$middle$$ THEN 1 WHEN $$last$$ THEN 2 ELSE 3 END AS author_rank,
        ad.orcid,
        ad.works_count AS author_works_count,
        ad.cited_by_count AS author_cited_by_count,
        paa.institutionid,
        aff.affiliation_name AS institution_name,
        aff.country_code,
        aff.type AS institution_type,
        aff.h_index AS institution_h_index,
        aff.productivity AS institution_productivity,
        paa.raw_affiliation_string
    FROM matched_papers mp
    LEFT JOIN cn_paper_author_affiliation paa ON mp.paperid = paa.paperid
    LEFT JOIN cn_author_details ad ON paa.authorid = ad.authorid
    LEFT JOIN cn_affiliations aff ON paa.institutionid = aff.institution_id
    WHERE paa.authorid IS NOT NULL
), author_institutions AS (
    SELECT
        paperid,
        authorid,
        json_agg(json_build_object(
            $$id$$, $$institution-$$ || institutionid,
            $$institutionid$$, institutionid,
            $$label$$, institution_name,
            $$country$$, country_code,
            $$type$$, institution_type,
            $$hIndex$$, institution_h_index,
            $$productivity$$, institution_productivity,
            $$rawAffiliation$$, raw_affiliation_string
        ) ORDER BY institution_name) AS institutions,
        array_agg(DISTINCT $$institution-$$ || institutionid) AS institution_ids
    FROM (
        SELECT DISTINCT paperid, authorid, institutionid, institution_name, country_code, institution_type, institution_h_index, institution_productivity, raw_affiliation_string
        FROM author_institution_rows
        WHERE institutionid IS NOT NULL
    ) rows
    GROUP BY paperid, authorid
), author_nodes AS (
    SELECT
        base.paperid,
        json_agg(json_build_object(
            $$id$$, $$author-$$ || base.authorid,
            $$nodeType$$, $$author$$,
            $$authorid$$, base.authorid,
            $$label$$, base.author_name,
            $$authorPosition$$, base.author_position,
            $$orcid$$, base.orcid,
            $$publicationCount$$, base.author_works_count,
            $$metric$$, base.author_cited_by_count,
            $$paperIds$$, ARRAY[base.paperid],
            $$institutionIds$$, COALESCE(ai.institution_ids, ARRAY[]::text[]),
            $$institutions$$, COALESCE(ai.institutions, $$[]$$::json)
        ) ORDER BY base.author_rank, base.author_name) AS authors
    FROM (
        SELECT paperid, authorid, author_name, author_position, MIN(author_rank) AS author_rank,
               MAX(orcid) AS orcid, MAX(author_works_count) AS author_works_count, MAX(author_cited_by_count) AS author_cited_by_count
        FROM author_institution_rows
        GROUP BY paperid, authorid, author_name, author_position
    ) base
    LEFT JOIN author_institutions ai ON base.paperid = ai.paperid AND base.authorid = ai.authorid
    GROUP BY base.paperid
), institution_nodes AS (
    SELECT
        paperid,
        json_agg(json_build_object(
            $$id$$, $$institution-$$ || institutionid,
            $$nodeType$$, $$institution$$,
            $$institutionid$$, institutionid,
            $$label$$, institution_name,
            $$country$$, country_code,
            $$institutionType$$, institution_type,
            $$hIndex$$, institution_h_index,
            $$productivity$$, institution_productivity,
            $$authorIds$$, author_ids,
            $$paperIds$$, ARRAY[paperid],
            $$paperCount$$, 1,
            $$metric$$, author_count
        ) ORDER BY author_count DESC, institution_name) AS institutions
    FROM (
        SELECT paperid, institutionid, institution_name, country_code, institution_type, institution_h_index, institution_productivity,
               COUNT(DISTINCT authorid) AS author_count,
               array_agg(DISTINCT $$author-$$ || authorid) AS author_ids
        FROM author_institution_rows
        WHERE institutionid IS NOT NULL
        GROUP BY paperid, institutionid, institution_name, country_code, institution_type, institution_h_index, institution_productivity
    ) institution_dedup
    GROUP BY paperid
), edge_base AS (
    SELECT DISTINCT paperid, paperid AS source, $$author-$$ || authorid AS target, $$authored_by$$ AS edge_type, $$authored by$$ AS edge_label,
           $$edge-$$ || paperid || $$-author-$$ || authorid AS edge_id
    FROM author_institution_rows
    WHERE authorid IS NOT NULL
    UNION
    SELECT DISTINCT paperid, $$author-$$ || authorid AS source, $$institution-$$ || institutionid AS target, $$affiliated_with$$ AS edge_type, $$affiliated with$$ AS edge_label,
           $$edge-author-$$ || authorid || $$-institution-$$ || institutionid AS edge_id
    FROM author_institution_rows
    WHERE authorid IS NOT NULL AND institutionid IS NOT NULL
), graph_edges AS (
    SELECT paperid, json_agg(json_build_object($$id$$, edge_id, $$source$$, source, $$target$$, target, $$type$$, edge_type, $$label$$, edge_label) ORDER BY edge_id) AS edges
    FROM edge_base
    GROUP BY paperid
)
SELECT
    mp.paperid,
    mp.title,
    mp.display_name,
    mp.year,
    mp.cited_by_count,
    mp.doi,
    mp.doctype,
    mp.language,
    public.inverted_index_to_text(mp.abstract_inverted_index) AS abstract,
    COALESCE(an.authors, $$[]$$::json) AS author_nodes,
    COALESCE(ins.institutions, $$[]$$::json) AS institution_nodes,
    COALESCE(ge.edges, $$[]$$::json) AS graph_edges
FROM matched_papers mp
LEFT JOIN author_nodes an ON mp.paperid = an.paperid
LEFT JOIN institution_nodes ins ON mp.paperid = ins.paperid
LEFT JOIN graph_edges ge ON mp.paperid = ge.paperid
ORDER BY mp.cited_by_count DESC NULLS LAST, mp.year DESC NULLS LAST;
"""


def _merge_seed_detail(graph: GraphResponse, seed: PaperSearchItem) -> None:
    for index, paper in enumerate(graph.papers):
        if paper.id == seed.id:
            graph.papers[index] = seed
            break
    for node in graph.nodes:
        if node.id == seed.id and node.node_type == "paper":
            payload = seed.model_dump(by_alias=True)
            for key, value in payload.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            node.title = seed.title
            node.year = seed.year
            node.authors = seed.authors
            node.venue = seed.venue
            node.abstract = seed.abstract
            node.doi = seed.doi
            node.citation_count = seed.citation_count
            node.topic = seed.topic
            node.summary = seed.summary
            node.metric = seed.metric
            node.data = payload
            node.data["source"] = "helmdb"
            break


def _dedupe_warnings(warnings: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for warning in warnings:
        if not warning or warning in seen:
            continue
        seen.add(warning)
        unique.append(warning)
    return unique


def _graph_response_from_search_rows(term: str, workspace_id: str | None, rows: list[dict[str, Any]]) -> GraphResponse:
    papers = [_paper_from_search_graph_row(row, index) for index, row in enumerate(rows)]
    nodes: list[GraphNode] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    for index, (row, paper) in enumerate(zip(rows, papers)):
        paper_node = _paper_node(paper)
        paper_node.position.x = float(index * 180)
        paper_node.position.y = 0
        paper_node.data = {**paper_node.data, "source": "postgresql"}
        nodes.append(paper_node)
        seen_nodes.add(paper_node.id)

        author_nodes = _list_value(row.get("author_nodes"))
        for author_index, author in enumerate(author_nodes):
            if not isinstance(author, dict):
                continue
            author_id = str(author.get("id") or f"author-{author.get('authorid')}")
            if not author_id or author_id in seen_nodes:
                continue
            institutions = [item for item in _list_value(author.get("institutions")) if isinstance(item, dict)]
            first_institution = institutions[0] if institutions else {}
            node = GraphNode(
                id=author_id,
                nodeType="author",
                label=str(author.get("label") or author.get("authorid") or "Unknown author"),
                summary=f"{len(_list_value(author.get('paperIds')))} matched paper(s)",
                metric=float(_int(author.get("metric"))),
                paperIds=[str(item) for item in _list_value(author.get("paperIds"))],
                publicationCount=_int(author.get("publicationCount")),
                institutionId=str(first_institution.get("id") or "") or None,
                institution=str(first_institution.get("label") or "") or None,
                position={"x": index * 180 + author_index * 34, "y": 170 + (author_index % 4) * 42},
                data={key: _jsonable(value) for key, value in author.items()},
            )
            nodes.append(node)
            seen_nodes.add(author_id)

        institution_nodes = _list_value(row.get("institution_nodes"))
        for institution_index, institution in enumerate(institution_nodes):
            if not isinstance(institution, dict):
                continue
            institution_id = str(institution.get("id") or f"institution-{institution.get('institutionid')}")
            if not institution_id or institution_id in seen_nodes:
                continue
            node = GraphNode(
                id=institution_id,
                nodeType="institution",
                label=str(institution.get("label") or institution.get("institutionid") or "Unknown institution"),
                summary=f"{_int(institution.get('metric'))} matched author(s)",
                metric=float(_int(institution.get("metric"))),
                authorIds=[str(item) for item in _list_value(institution.get("authorIds"))],
                paperIds=[str(item) for item in _list_value(institution.get("paperIds"))],
                paperCount=_int(institution.get("paperCount")),
                country=str(institution.get("country") or "Unknown"),
                position={"x": index * 180 + institution_index * 80, "y": 360 + (institution_index % 3) * 58},
                data={key: _jsonable(value) for key, value in institution.items()},
            )
            nodes.append(node)
            seen_nodes.add(institution_id)

        edges.extend(edge for edge in _list_value(row.get("graph_edges")) if isinstance(edge, dict))

    return GraphResponse(
        graphId=_new_graph_id("search", term),
        workspaceId=workspace_id,
        title=f"Search: {term}",
        queryContext="Paper title search",
        seedNodeId=None,
        seedPaperId=None,
        defaultViewMode="list",
        nodes=nodes,
        papers=papers,
        edges=_dedupe_edges(edges),
        warnings=[],
    )


def _paper_from_search_graph_row(row: dict[str, Any], index: int) -> PaperSearchItem:
    paper = _paper_from_payload(row, index)
    author_names = [str(author.get("label")) for author in _list_value(row.get("author_nodes")) if isinstance(author, dict) and author.get("label")]
    if author_names:
        paper.authors = author_names
        paper.label = f"{author_names[0]}, {paper.year}" if paper.year else author_names[0]
    return paper


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def paper_graph_nodes(papers: list[PaperSearchItem], include_institutions: bool = True) -> tuple[list[GraphNode], list[dict[str, Any]]]:
    nodes: list[GraphNode] = [_paper_node(paper) for paper in papers]
    edges: list[dict[str, Any]] = []
    authors: dict[str, dict[str, Any]] = {}
    institutions: dict[str, dict[str, Any]] = {}
    for paper_index, paper in enumerate(papers):
        for author_index, name in enumerate(paper.authors or []):
            author_id = f"author-{_slug(name)}"
            institution_basis = None  # Venue/topic labels cannot establish affiliation.
            institution_id = f"institution-{_slug(institution_basis)}" if institution_basis else None
            institution_label = _institution_label_from_basis(institution_basis) if institution_basis else None
            author = authors.setdefault(author_id, {"label": name, "paper_ids": [], "x": paper_index * 140 + author_index * 40, "y": 150, "institution_id": institution_id, "institution": institution_label})
            if paper.id not in author["paper_ids"]:
                author["paper_ids"].append(paper.id)
            edges.append({"id": f"edge-{paper.id}-{author_id}", "source": paper.id, "target": author_id, "type": "authored_by", "label": "authored by"})
            if include_institutions and institution_id and institution_label:
                inst = institutions.setdefault(institution_id, {"label": institution_label, "author_ids": [], "paper_ids": set(), "x": paper_index * 140, "y": 310})
                if author_id not in inst["author_ids"]:
                    inst["author_ids"].append(author_id)
                inst["paper_ids"].add(paper.id)
                edges.append({"id": f"edge-{author_id}-{institution_id}", "source": author_id, "target": institution_id, "type": "affiliated_with", "label": "affiliated with"})
    for author_id, author in authors.items():
        nodes.append(GraphNode(id=author_id, nodeType="author", label=author["label"], summary=f"{len(author['paper_ids'])} matched paper(s)", paperIds=author["paper_ids"], publicationCount=len(author["paper_ids"]), metric=float(len(author["paper_ids"])), institutionId=author.get("institution_id"), institution=author.get("institution"), position={"x": author["x"], "y": author["y"]}))
    for inst_id, inst in institutions.items():
        paper_ids = sorted(inst["paper_ids"])
        nodes.append(GraphNode(id=inst_id, nodeType="institution", label=inst["label"], summary=f"{len(inst['author_ids'])} matched author(s)", authorIds=inst["author_ids"], paperIds=paper_ids, paperCount=len(paper_ids), metric=float(len(paper_ids)), position={"x": inst["x"], "y": inst["y"]}))
    return nodes, _dedupe_edges(edges)


def _paper_node(paper: PaperSearchItem) -> GraphNode:
    payload = paper.model_dump(by_alias=True)
    node = GraphNode.model_validate(payload)
    node.data = payload
    return node


def _paper_from_payload(payload: dict[str, Any], index: int) -> PaperSearchItem:
    citation_count = _int(payload.get("citationCount") or payload.get("citation_count") or payload.get("cited_by_count"))
    authors = [str(author) for author in payload.get("authors", []) if author]
    year = _int(payload.get("year"))
    first = authors[0] if authors else "Unknown"
    return PaperSearchItem(
        id=str(payload.get("id") or payload.get("paperid") or f"paper_{index}"),
        nodeType="paper",
        label=str(payload.get("label") or (f"{first}, {year}" if year else first)),
        title=str(payload.get("title") or "Untitled paper"),
        year=year,
        authors=authors,
        venue=str(payload.get("venue") or ""),
        abstract=str(payload.get("abstract") or ""),
        doi=payload.get("doi"),
        citationCount=citation_count,
        topic=str(payload.get("topic") or ""),
        doctype=str(payload.get("doctype") or "") or None,
        language=str(payload.get("language") or "") or None,
        summary=str(payload.get("summary") or payload.get("title") or "Untitled paper"),
        metric=float(payload.get("metric") or citation_count),
        position=payload.get("position") or {"x": index * 140, "y": 0},
    )


def _normalize_similar_results(raw_results: list[Any], seed_paper_id: str | None, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_results):
        if isinstance(raw, dict):
            paper_id = str(raw.get("paper_id") or raw.get("paperId") or raw.get("paperid") or raw.get("id") or "").strip()
            score = raw.get("score")
            rank = raw.get("rank") or index + 1
        else:
            paper_id = str(raw or "").strip()
            score = None
            rank = index + 1
        if not paper_id or paper_id == seed_paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        items.append({"paper_id": paper_id, "rank": rank, "score": score})
        if len(items) >= limit:
            break
    return items


def _similar_position(index: int, total: int) -> GraphPosition:
    if total <= 0:
        return GraphPosition(x=160, y=0)
    radius = 210
    angle = (2 * 3.141592653589793 * index) / max(total, 1)
    return GraphPosition(x=round(radius * math.cos(angle), 2), y=round(radius * math.sin(angle), 2))


def _semantic_graph_response(
    seed: PaperSearchItem,
    workspace_id: str | None,
    graph_row: dict[str, Any],
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    properties: list[dict[str, Any]],
    warnings: list[str],
) -> GraphResponse:
    paper_id = seed.id
    paper_node = _paper_node(seed)
    paper_node.data = {**paper_node.data, "source": "semantic_kg", "semanticGraph": _jsonable(graph_row)}
    nodes: list[GraphNode] = [paper_node]
    edges: list[dict[str, Any]] = []
    properties_by_entity: dict[str, list[dict[str, Any]]] = {}
    for prop in properties:
        entity_id = str(prop.get("entity_id") or prop.get("entity-id") or "").strip()
        if not entity_id:
            continue
        properties_by_entity.setdefault(entity_id, []).append(
            {
                "id": prop.get("property_id") or prop.get("property-id"),
                "name": prop.get("property_name") or prop.get("property-name"),
                "type": prop.get("property_type") or prop.get("property-type"),
                "value": prop.get("property_value") or prop.get("property-value"),
                "raw": _json_field(prop.get("raw")),
            }
        )

    entity_node_ids: dict[str, str] = {}
    for index, entity in enumerate(entities):
        entity_id = str(entity.get("entity_id") or entity.get("entity-id") or "").strip()
        if not entity_id:
            continue
        node_id = _semantic_entity_node_id(paper_id, entity_id)
        entity_node_ids[entity_id] = node_id
        raw = _json_field(entity.get("raw"))
        entity_type = str(entity.get("entity_type") or entity.get("entity-type") or "").strip()
        comment = str(entity.get("entity_comment") or entity.get("entity-comment") or "").strip()
        nodes.append(
            GraphNode(
                id=node_id,
                nodeType="semantic_entity",
                label=str(entity.get("entity_name") or entity.get("entity-name") or entity_id),
                summary=comment or entity_type or "Semantic entity",
                metric=1.0,
                paperIds=[paper_id],
                position={"x": 180 + (index % 5) * 150, "y": -160 + (index // 5) * 130},
                data={
                    "paperId": paper_id,
                    "entityId": entity_id,
                    "entityType": entity_type,
                    "comment": comment,
                    "source": entity.get("entity_source") or entity.get("entity-source"),
                    "properties": properties_by_entity.get(entity_id, []),
                    "raw": raw,
                },
            )
        )

    for relation in relations:
        relation_id = str(relation.get("relation_id") or relation.get("relation-id") or "").strip()
        relation_name = str(relation.get("relation_name") or relation.get("relation-name") or "semantic_relation").strip()
        head_id = str(relation.get("head_entity_id") or relation.get("head-entity-id") or "").strip()
        tail_id = str(relation.get("tail_entity_id") or relation.get("tail-entity-id") or "").strip()
        if head_id not in entity_node_ids:
            warnings.append(f"Semantic relation {relation_id or 'unknown'} skipped: missing head entity {head_id or 'empty'}.")
            continue
        if tail_id not in entity_node_ids:
            warnings.append(f"Semantic relation {relation_id or 'unknown'} skipped: missing tail entity {tail_id or 'empty'}.")
            continue
        edges.append(
            {
                "id": f"edge-{paper_id}-semantic-{relation_id or hashlib.sha1(json.dumps(_jsonable(relation), sort_keys=True).encode('utf-8')).hexdigest()[:10]}",
                "source": entity_node_ids[head_id],
                "target": entity_node_ids[tail_id],
                "type": "semantic_relation",
                "label": relation_name,
                "data": {
                    "paperId": paper_id,
                    "relationId": relation_id,
                    "relationName": relation_name,
                    "headEntityId": head_id,
                    "tailEntityId": tail_id,
                    "raw": _json_field(relation.get("raw")),
                },
            }
        )

    return GraphResponse(
        graphId=_new_graph_id("paper_semantic_graph", paper_id),
        workspaceId=workspace_id,
        title=f"Semantic Graph: {seed.title}",
        queryContext="Paper semantic graph",
        seedNodeId=seed.id,
        seedPaperId=seed.id,
        defaultViewMode="graph",
        nodes=nodes,
        papers=[seed],
        edges=_dedupe_edges(edges),
        warnings=_dedupe_warnings(warnings),
    )


def _semantic_entity_node_id(paper_id: str, entity_id: str) -> str:
    return f"{paper_id}:semantic_entity:{entity_id}"


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return _jsonable(value)


def _edge(source: str, target: str, edge_type: str, source_year: int | None, target_year: int | None) -> dict[str, Any]:
    year_diff = None
    if source_year and target_year:
        year_diff = source_year - target_year
    return {
        "id": f"edge-{source}-{target}-{edge_type}",
        "source": source,
        "target": target,
        "type": edge_type,
        "label": edge_type,
        "data": {"year": source_year, "refYear": target_year, "yearDiff": year_diff},
    }


def _dedupe_papers(papers: list[PaperSearchItem]) -> list[PaperSearchItem]:
    seen: set[str] = set()
    unique: list[PaperSearchItem] = []
    for paper in papers:
        if paper.id in seen:
            continue
        seen.add(paper.id)
        unique.append(paper)
    return unique


def _dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for edge in edges:
        edge_id = str(edge.get("id"))
        if edge_id in seen:
            continue
        seen.add(edge_id)
        unique.append(edge)
    return unique


def _limit(value: Any, default: int = 20) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 1), MAX_LIMIT)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _slug(value: str) -> str:
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]
    safe = "".join(char if char.isalnum() else "-" for char in str(value).lower()).strip("-")
    return f"{safe[:36] or 'unknown'}-{digest}"


def _new_graph_id(prefix: str, value: str | None) -> str:
    digest = hashlib.sha1(f"{prefix}:{value}:{time.time()}".encode("utf-8")).hexdigest()[:24]
    return f"graph_{digest}"


def _known_institution_basis(paper: PaperSearchItem) -> str | None:
    for value in [paper.topic, paper.venue]:
        basis = (value or "").strip()
        if not basis:
            continue
        if any(term in basis.lower() for term in ["university", "institute", "institution", "academy", "college", "school", "lab", "laboratory", "center", "centre"]):
            return basis
    return None


def _institution_label_from_basis(basis: str) -> str:
    return basis.strip()


def _institution_label(paper: PaperSearchItem) -> str:
    return _institution_label_from_basis(_known_institution_basis(paper) or "")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
