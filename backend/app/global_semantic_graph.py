from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_env
from app.graph_demo import GraphDemoError
from app.models import (
    GlobalSemanticEntityItem,
    GlobalSemanticEntitySearchResponse,
    GlobalSemanticGraphExpandRequest,
    GlobalSemanticGraphExpandResponse,
    GlobalSemanticGraphExpandSeed,
    GlobalSemanticGraphExpandStats,
    GlobalSemanticGraphQueryRequest,
    GlobalSemanticGraphResponse,
    GlobalSemanticOverviewResponse,
    GraphNode,
)


_SPACE_RE = re.compile(r"\s+")
_SEPARATOR_RE = re.compile(r"[-_]+")


def normalize_entity_name(value: str) -> str:
    text = _SEPARATOR_RE.sub(" ", str(value).strip())
    return _SPACE_RE.sub(" ", text).lower()


def normalize_entity_type(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value).strip()).lower()


class GlobalSemanticGraphService:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self.sqlite_path = Path(sqlite_path) if sqlite_path else None
        self.connection_params = {
            "host": get_env("DB_HOST"),
            "port": int(get_env("DB_PORT", "5432") or "5432"),
            "user": get_env("DB_USER"),
            "password": get_env("DB_PASSWORD"),
            "dbname": get_env("DB_NAME"),
            "options": "-c client_encoding=UTF8 -c statement_timeout=300000",
            "connect_timeout": int(get_env("BIGDIPPER_GRAPH_DB_CONNECT_TIMEOUT", "2") or "2"),
        }

    def overview(self, dataset: str, min_paper_count: int) -> GlobalSemanticOverviewResponse:
        build = self._latest_build(dataset)
        rows = self._fetchall(
            """
            SELECT
                count(*) AS total_entity_count,
                sum(CASE WHEN s.paper_count >= ? THEN 1 ELSE 0 END) AS eligible_entity_count,
                COALESCE(sum(s.mention_count), 0) AS mention_count
            FROM global_semantic_entity_stats s
            WHERE s.dataset_scope = ?
            """,
            (min_paper_count, dataset),
        )
        counts = rows[0] if rows else {}
        edge_rows = self._fetchall(
            """
            SELECT
                e.edge_type,
                count(*) AS edge_count
            FROM global_semantic_edges e
            JOIN global_semantic_entity_stats ss
              ON ss.dataset_scope = e.dataset_scope
             AND ss.global_entity_id = e.source_global_entity_id
            JOIN global_semantic_entity_stats ts
              ON ts.dataset_scope = e.dataset_scope
             AND ts.global_entity_id = e.target_global_entity_id
            WHERE e.dataset_scope = ?
              AND ss.paper_count >= ?
              AND ts.paper_count >= ?
            GROUP BY e.edge_type
            """,
            (dataset, min_paper_count, min_paper_count),
        )
        edges = {str(row["edge_type"]): int(row["edge_count"] or 0) for row in edge_rows}
        warnings: list[str] = []
        if build is None:
            warnings.append(f"Global semantic graph scope '{dataset}' has not been built.")
        elif min_paper_count < int(build.get("min_paper_count") or 1):
            warnings.append(
                "Requested minPaperCount is below the materialized edge threshold; "
                "singleton/lower-frequency edges require a rebuild."
            )
        return GlobalSemanticOverviewResponse(
            dataset=dataset,
            minPaperCount=min_paper_count,
            totalEntityCount=int(counts.get("total_entity_count") or 0),
            eligibleEntityCount=int(counts.get("eligible_entity_count") or 0),
            mentionCount=int(counts.get("mention_count") or 0),
            coOccurrenceEdgeCount=edges.get("co_occurrence", 0),
            semanticRelationEdgeCount=edges.get("semantic_relation", 0),
            buildId=str(build["build_id"]) if build else None,
            builtAt=build.get("completed_at") if build else None,
            buildMinPaperCount=int(build["min_paper_count"]) if build else None,
            warnings=warnings,
        )

    def search_entities(
        self,
        query: str,
        dataset: str,
        min_paper_count: int,
        entity_types: list[str],
        limit: int,
        *,
        hydrate_example_papers: bool = True,
    ) -> GlobalSemanticEntitySearchResponse:
        term = normalize_entity_name(query)
        if not term:
            raise GraphDemoError("invalid_query", "query is required")
        normalized_types = [normalize_entity_type(value) for value in entity_types if value.strip()]
        clauses = [
            "e.dataset_scope = ?",
            "s.paper_count >= ?",
            "(e.normalized_name = ? OR e.normalized_name LIKE ? OR e.normalized_name LIKE ?)",
        ]
        params: list[Any] = [dataset, min_paper_count, term, f"{term}%", f"%{term}%"]
        if normalized_types:
            clauses.append(f"e.normalized_entity_type IN ({_placeholders(len(normalized_types))})")
            params.extend(normalized_types)
        params.extend([term, f"{term}%", limit])
        rows = self._fetchall(
            f"""
            SELECT
                e.global_entity_id, e.name_group_id,
                e.canonical_name, e.normalized_name,
                e.canonical_entity_type, e.normalized_entity_type,
                e.type_distribution,
                s.paper_count, s.mention_count, s.datasets, s.example_papers
            FROM global_semantic_entities e
            JOIN global_semantic_entity_stats s
              ON s.dataset_scope = e.dataset_scope
             AND s.global_entity_id = e.global_entity_id
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE
                    WHEN e.normalized_name = ? THEN 0
                    WHEN e.normalized_name LIKE ? THEN 1
                    ELSE 2
                END,
                s.paper_count DESC,
                e.normalized_name,
                e.global_entity_id
            LIMIT ?
            """,
            tuple(params),
        )
        paper_details = self._load_example_paper_details(rows) if hydrate_example_papers else {}
        return GlobalSemanticEntitySearchResponse(
            query=query.strip(),
            dataset=dataset,
            minPaperCount=min_paper_count,
            items=[_entity_item(row, paper_details) for row in rows],
            warnings=[] if self._latest_build(dataset) else [f"Global semantic graph scope '{dataset}' has not been built."],
        )

    def query_graph(self, request: GlobalSemanticGraphQueryRequest) -> GlobalSemanticGraphResponse:
        if bool(request.seed_paper_id) != bool(request.seed_entity_id):
            raise GraphDemoError(
                "invalid_query",
                "seedPaperId and seedEntityId must be provided together",
            )
        if not request.edge_types:
            raise GraphDemoError("invalid_query", "edgeTypes must not be empty")

        seed_id = self._resolve_seed(request)
        seed_rows = self._entity_rows(
            request.dataset,
            [seed_id],
            request.min_paper_count,
            request.entity_types,
        )
        if not seed_rows:
            raise GraphDemoError(
                "global_entity_not_found",
                "Global semantic entity not found for the selected filters",
                {"globalEntityId": seed_id, "dataset": request.dataset},
            )

        selected_ids: set[str] = {seed_id}
        ordered_ids: list[str] = [seed_id]
        selected_edges: dict[str, dict[str, Any]] = {}
        frontier: set[str] = {seed_id}
        available_neighbors: set[str] = set()
        truncated = False

        for _level in range(request.depth):
            if not frontier or len(selected_ids) >= request.limit:
                break
            candidates = self._neighbor_edges(request, frontier)
            new_frontier: set[str] = set()
            for edge in candidates:
                source = str(edge["source_global_entity_id"])
                target = str(edge["target_global_entity_id"])
                if source in frontier:
                    available_neighbors.add(target)
                if target in frontier:
                    available_neighbors.add(source)
                missing = [node_id for node_id in (source, target) if node_id not in selected_ids]
                if len(selected_ids) + len(missing) > request.limit:
                    truncated = True
                    continue
                for node_id in missing:
                    selected_ids.add(node_id)
                    ordered_ids.append(node_id)
                    new_frontier.add(node_id)
                selected_edges[str(edge["edge_id"])] = edge
            if len(candidates) >= max(200, request.limit * 20):
                truncated = True
            frontier = new_frontier

        node_rows = self._entity_rows(
            request.dataset,
            ordered_ids,
            request.min_paper_count,
            request.entity_types,
        )
        paper_details = self._load_example_paper_details(node_rows)
        rows_by_id = {str(row["global_entity_id"]): row for row in node_rows}
        ordered_ids = [node_id for node_id in ordered_ids if node_id in rows_by_id]
        nodes = [
            _graph_node(rows_by_id[node_id], index, len(ordered_ids), paper_details)
            for index, node_id in enumerate(ordered_ids)
        ]
        valid_ids = set(ordered_ids)
        edge_rows = [
            edge
            for edge in selected_edges.values()
            if str(edge["source_global_entity_id"]) in valid_ids
            and str(edge["target_global_entity_id"]) in valid_ids
        ]
        evidence_paper_details = self._load_evidence_paper_details(edge_rows)
        edges = [
            _graph_edge(edge, evidence_paper_details)
            for edge in edge_rows
        ]
        build = self._latest_build(request.dataset)
        seed_name = str(rows_by_id[seed_id]["canonical_name"])
        warnings: list[str] = []
        if build is None:
            warnings.append(f"Global semantic graph scope '{request.dataset}' has not been built.")
        elif request.min_paper_count < int(build.get("min_paper_count") or 1):
            warnings.append(
                "Requested minPaperCount is below the materialized edge threshold; "
                "returned edges are limited to the built threshold."
            )
        return GlobalSemanticGraphResponse(
            graphId=f"graph_global_{hashlib.sha1(f'{seed_id}:{time.time()}'.encode()).hexdigest()[:24]}",
            workspaceId=None,
            title=f"Global Semantic Graph: {seed_name}",
            queryContext=f"Global semantic neighborhood for {seed_name}",
            seedNodeId=seed_id,
            seedPaperId=request.seed_paper_id,
            defaultViewMode="graph",
            nodes=nodes,
            papers=[],
            edges=edges,
            warnings=warnings,
            stats={
                "dataset": request.dataset,
                "minPaperCount": request.min_paper_count,
                "edgeMinWeight": request.edge_min_weight,
                "depth": request.depth,
                "returnedNodeCount": len(nodes),
                "returnedEdgeCount": len(edges),
                "availableNeighborCount": len(available_neighbors),
                "truncated": truncated,
                "buildId": str(build["build_id"]) if build else None,
                "builtAt": build.get("completed_at") if build else None,
            },
        )

    def expand_graph(
        self,
        request: GlobalSemanticGraphExpandRequest,
    ) -> GlobalSemanticGraphExpandResponse:
        """Return only the nodes and edges that can be merged into a paper graph."""

        if not request.edge_types:
            raise GraphDemoError("invalid_query", "edgeTypes must not be empty")

        local_input = request.seed_entity_id.strip()
        global_id, resolved_local_id = self._resolve_expand_seed(request)
        seed_rows = self._entity_rows(
            request.dataset,
            [global_id],
            request.min_paper_count,
            request.entity_types,
        )
        if not seed_rows:
            raise GraphDemoError(
                "global_entity_not_found",
                "Global semantic entity not found for the selected filters",
                {"globalEntityId": global_id, "dataset": request.dataset},
            )

        ordered_ids, selected_edges, available_neighbors, truncated = (
            self._select_neighborhood(request, global_id)
        )
        node_rows = self._entity_rows(
            request.dataset,
            ordered_ids,
            request.min_paper_count,
            request.entity_types,
        )
        rows_by_id = {str(row["global_entity_id"]): row for row in node_rows}
        ordered_ids = [node_id for node_id in ordered_ids if node_id in rows_by_id]
        valid_ids = set(ordered_ids)
        selected_edges = [
            edge
            for edge in selected_edges
            if str(edge["source_global_entity_id"]) in valid_ids
            and str(edge["target_global_entity_id"]) in valid_ids
        ]

        seed_row = rows_by_id.get(global_id)
        if seed_row is None:
            raise GraphDemoError(
                "global_entity_not_found",
                "Global semantic entity disappeared while building the expansion",
                {"globalEntityId": global_id, "dataset": request.dataset},
            )

        # Seed paper first makes the response useful even when the materialized
        # example list was filled before the currently opened paper was indexed.
        candidate_pairs: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        paper_ids: list[str] = []
        seen_papers: set[str] = set()

        def add_pair(entity_id: str, paper_id: str) -> None:
            nonlocal truncated
            pair = (str(entity_id), str(paper_id))
            if pair[0] not in valid_ids or not pair[1] or pair in seen_pairs:
                return
            if pair[1] not in seen_papers:
                if len(paper_ids) >= request.limit:
                    truncated = True
                    return
                seen_papers.add(pair[1])
                paper_ids.append(pair[1])
            seen_pairs.add(pair)
            candidate_pairs.append(pair)

        seed_papers = [request.seed_paper_id.strip()]
        seed_papers.extend(_json_list(seed_row.get("example_papers")))
        for paper_id in list(dict.fromkeys(seed_papers))[: request.evidence_limit]:
            add_pair(global_id, paper_id)

        for edge in selected_edges:
            evidence_papers = _json_list(edge.get("evidence_papers"))
            if len(evidence_papers) > request.evidence_limit:
                truncated = True
            for paper_id in evidence_papers[: request.evidence_limit]:
                add_pair(str(edge["source_global_entity_id"]), paper_id)
                add_pair(str(edge["target_global_entity_id"]), paper_id)

        detail_paper_ids = list(paper_ids)
        seen_detail_papers = set(detail_paper_ids)
        for row in node_rows:
            for paper_id in _json_list(row.get("example_papers"))[
                : request.evidence_limit
            ]:
                if paper_id not in seen_detail_papers:
                    seen_detail_papers.add(paper_id)
                    detail_paper_ids.append(paper_id)
        paper_details = self._load_expand_paper_details(
            detail_paper_ids,
            request.dataset,
        )
        mention_details = self._load_entity_paper_mentions(
            request.dataset,
            valid_ids,
            paper_ids,
        )

        nodes: list[GraphNode] = []
        for index, node_id in enumerate(ordered_ids):
            nodes.append(
                _expand_entity_node(
                    rows_by_id[node_id],
                    index,
                    len(ordered_ids),
                    request.dataset,
                    request.evidence_limit,
                    paper_details,
                )
            )

        paper_node_ids: set[str] = set()
        for index, paper_id in enumerate(paper_ids):
            detail = paper_details.get(paper_id, {})
            mention_values = [
                mention_details.get((entity_id, paper_id), {})
                for entity_id, candidate_paper in candidate_pairs
                if candidate_paper == paper_id
            ]
            source_dataset = next(
                (
                    _optional_string(value.get("dataset"))
                    for value in mention_values
                    if value.get("dataset")
                ),
                None,
            ) or _optional_string(detail.get("dataset"))
            nodes.append(
                _expand_paper_node(
                    paper_id,
                    detail,
                    index,
                    len(paper_ids),
                    source_dataset or request.dataset,
                )
            )
            paper_node_ids.add(f"paper_{paper_id}")

        edges: list[dict[str, Any]] = []
        local_edge_id = _stable_expand_id(
            "local_to_global",
            request.dataset,
            request.seed_paper_id.strip(),
            resolved_local_id,
            global_id,
        )
        edges.append(
            {
                "id": local_edge_id,
                "source": local_input,
                "target": global_id,
                "type": "local_to_global",
                "edgeType": "local_to_global",
                "label": "normalized to",
                "data": {
                    "paperId": request.seed_paper_id.strip(),
                    "localEntityId": resolved_local_id,
                    "localNodeId": (
                        f"{request.seed_paper_id.strip()}:semantic_entity:{resolved_local_id}"
                    ),
                    "globalEntityId": global_id,
                    "datasetScope": request.dataset,
                },
            }
        )

        for entity_id, paper_id in candidate_pairs:
            paper_node_id = f"paper_{paper_id}"
            if paper_node_id not in paper_node_ids:
                continue
            detail = paper_details.get(paper_id, {})
            mention = mention_details.get((entity_id, paper_id), {})
            edge_id = _stable_expand_id(
                "entity_mentioned_in_paper",
                request.dataset,
                entity_id,
                paper_id,
            )
            edges.append(
                {
                    "id": edge_id,
                    "source": entity_id,
                    "target": paper_node_id,
                    "type": "entity_mentioned_in_paper",
                    "edgeType": "entity_mentioned_in_paper",
                    "label": "mentioned in",
                    "data": {
                        "paperId": paper_id,
                        "mentionCount": int(mention.get("mention_count") or 1),
                        "dataset": _optional_string(
                            mention.get("dataset")
                        )
                        or _optional_string(detail.get("dataset"))
                        or request.dataset,
                        "datasetScope": request.dataset,
                    },
                }
            )

        edges.extend(
            _expand_global_edge(
                edge,
                request.evidence_limit,
                paper_details,
                seen_papers,
                request.dataset,
            )
            for edge in selected_edges
        )

        build = self._latest_build(request.dataset)
        warnings: list[str] = []
        if build is None:
            warnings.append(
                f"Global semantic graph scope '{request.dataset}' has not been built."
            )
        elif request.min_paper_count < int(build.get("min_paper_count") or 1):
            warnings.append(
                "Requested minPaperCount is below the materialized edge threshold; "
                "returned edges are limited to the built threshold."
            )

        return GlobalSemanticGraphExpandResponse(
            seed=GlobalSemanticGraphExpandSeed(
                paperId=request.seed_paper_id.strip(),
                localEntityId=local_input,
                globalEntityId=global_id,
                canonicalName=str(seed_row["canonical_name"]),
                entityType=str(seed_row["canonical_entity_type"]),
            ),
            nodes=nodes,
            edges=edges,
            stats=GlobalSemanticGraphExpandStats(
                dataset=request.dataset,
                minPaperCount=request.min_paper_count,
                edgeMinWeight=request.edge_min_weight,
                depth=request.depth,
                evidenceLimit=request.evidence_limit,
                nodeCount=len(nodes),
                edgeCount=len(edges),
                paperNodeCount=len(paper_ids),
                globalEntityNodeCount=len(ordered_ids),
                availableNeighborCount=available_neighbors,
                truncated=truncated,
                buildId=str(build["build_id"]) if build else None,
                builtAt=build.get("completed_at") if build else None,
            ),
            warnings=warnings,
        )

    def _resolve_expand_seed(
        self,
        request: GlobalSemanticGraphExpandRequest,
    ) -> tuple[str, str]:
        paper_id = request.seed_paper_id.strip()
        supplied_entity_id = request.seed_entity_id.strip()
        candidates = [supplied_entity_id]
        prefix = f"{paper_id}:semantic_entity:"
        if supplied_entity_id.startswith(prefix):
            raw_entity_id = supplied_entity_id[len(prefix) :]
            if raw_entity_id:
                candidates.append(raw_entity_id)
        rows = self._fetchall(
            f"""
            SELECT entity_id, global_entity_id
            FROM global_semantic_entity_mentions
            WHERE dataset_scope = ? AND paperid = ?
              AND entity_id IN ({_placeholders(len(candidates))})
            LIMIT ?
            """,
            (request.dataset, paper_id, *candidates, len(candidates)),
        )
        by_local_id = {str(row["entity_id"]): str(row["global_entity_id"]) for row in rows}
        for candidate in candidates:
            if candidate in by_local_id:
                return by_local_id[candidate], candidate
        raise GraphDemoError(
            "global_entity_not_found",
            "No global entity mapping found for seedPaperId + seedEntityId",
            {
                "paperId": paper_id,
                "entityId": supplied_entity_id,
                "dataset": request.dataset,
            },
        )

    def _select_neighborhood(
        self,
        request: GlobalSemanticGraphQueryRequest | GlobalSemanticGraphExpandRequest,
        seed_id: str,
    ) -> tuple[list[str], list[dict[str, Any]], int, bool]:
        selected_ids: set[str] = {seed_id}
        ordered_ids: list[str] = [seed_id]
        selected_edges: dict[str, dict[str, Any]] = {}
        frontier: set[str] = {seed_id}
        available_neighbors: set[str] = set()
        truncated = False

        for _level in range(request.depth):
            if not frontier:
                break
            if len(selected_ids) >= request.limit:
                if self._neighbor_edges(request, frontier):
                    truncated = True
                break
            candidates = self._neighbor_edges(request, frontier)
            new_frontier: set[str] = set()
            for edge in candidates:
                edge_id = str(edge["edge_id"])
                if edge_id not in selected_edges and len(selected_edges) >= request.limit:
                    truncated = True
                    continue
                source = str(edge["source_global_entity_id"])
                target = str(edge["target_global_entity_id"])
                if source in frontier:
                    available_neighbors.add(target)
                if target in frontier:
                    available_neighbors.add(source)
                missing = [
                    node_id
                    for node_id in (source, target)
                    if node_id not in selected_ids
                ]
                if len(selected_ids) + len(missing) > request.limit:
                    truncated = True
                    continue
                for node_id in missing:
                    selected_ids.add(node_id)
                    ordered_ids.append(node_id)
                    new_frontier.add(node_id)
                selected_edges[edge_id] = edge
            if len(candidates) >= max(200, request.limit * 20):
                truncated = True
            frontier = new_frontier

        return (
            ordered_ids,
            list(selected_edges.values()),
            len(available_neighbors),
            truncated,
        )

    def _load_expand_paper_details(
        self,
        paper_ids: list[str],
        dataset: str,
    ) -> dict[str, dict[str, Any]]:
        details = self._load_paper_details(paper_ids)
        if not paper_ids:
            return details
        try:
            direct_rows = self._fetchall(
                f"""
                SELECT paperid, venue, dataset
                FROM cn_paperdetails
                WHERE paperid IN ({_placeholders(len(paper_ids))})
                """,
                tuple(paper_ids),
            )
        except GraphDemoError:
            # HelmDB deployments keep venue/dataset in the source table.
            direct_rows = []
        for row in direct_rows:
            paper_id = str(row["paperid"])
            details.setdefault(paper_id, {})
            details[paper_id].update(
                {
                    "venue": _optional_string(row.get("venue")),
                    "dataset": _optional_string(row.get("dataset")),
                }
            )
        try:
            source_rows = self._fetchall(
                f"""
                SELECT paperid, MIN(venue) AS venue, MIN(dataset) AS dataset
                FROM paper_kg_sources source
                WHERE source.paperid IN ({_placeholders(len(paper_ids))})
                  AND EXISTS (
                      SELECT 1
                      FROM global_semantic_entity_mentions scoped
                      WHERE scoped.dataset_scope = ?
                        AND scoped.paperid = source.paperid
                  )
                GROUP BY source.paperid
                """,
                (*paper_ids, dataset),
            )
        except GraphDemoError:
            # Older installations may not have the optional source table.
            source_rows = []
        for row in source_rows:
            paper_id = str(row["paperid"])
            details.setdefault(paper_id, {})
            details[paper_id].update(
                {
                    "venue": _optional_string(row.get("venue")),
                    "dataset": _optional_string(row.get("dataset")),
                }
            )
        return details

    def _load_entity_paper_mentions(
        self,
        dataset: str,
        entity_ids: set[str],
        paper_ids: list[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not entity_ids or not paper_ids:
            return {}
        rows = self._fetchall(
            f"""
            SELECT global_entity_id, paperid, count(*) AS mention_count,
                   MIN(dataset) AS dataset
            FROM global_semantic_entity_mentions
            WHERE dataset_scope = ?
              AND global_entity_id IN ({_placeholders(len(entity_ids))})
              AND paperid IN ({_placeholders(len(paper_ids))})
            GROUP BY global_entity_id, paperid
            """,
            (dataset, *sorted(entity_ids), *paper_ids),
        )
        return {
            (str(row["global_entity_id"]), str(row["paperid"])): row
            for row in rows
        }

    def _resolve_seed(self, request: GlobalSemanticGraphQueryRequest) -> str:
        if request.seed_global_entity_id and request.seed_global_entity_id.strip():
            return request.seed_global_entity_id.strip()
        if request.seed_paper_id and request.seed_entity_id:
            rows = self._fetchall(
                """
                SELECT global_entity_id
                FROM global_semantic_entity_mentions
                WHERE dataset_scope = ? AND paperid = ? AND entity_id = ?
                LIMIT 1
                """,
                (request.dataset, request.seed_paper_id.strip(), request.seed_entity_id.strip()),
            )
            if rows:
                return str(rows[0]["global_entity_id"])
            raise GraphDemoError(
                "global_entity_not_found",
                "No global entity mapping found for seedPaperId + seedEntityId",
                {"paperId": request.seed_paper_id, "entityId": request.seed_entity_id},
            )
        if request.query and request.query.strip():
            result = self.search_entities(
                request.query,
                request.dataset,
                request.min_paper_count,
                request.entity_types,
                1,
                hydrate_example_papers=False,
            )
            if result.items:
                return result.items[0].global_entity_id
            raise GraphDemoError(
                "global_entity_not_found",
                "No global entity matched query",
                {"query": request.query},
            )
        raise GraphDemoError(
            "invalid_query",
            "One of seedGlobalEntityId, seedPaperId + seedEntityId, or query is required",
        )

    def _neighbor_edges(
        self,
        request: GlobalSemanticGraphQueryRequest | GlobalSemanticGraphExpandRequest,
        frontier: set[str],
    ) -> list[dict[str, Any]]:
        frontier_values = sorted(frontier)
        edge_types = list(dict.fromkeys(request.edge_types))
        normalized_types = [normalize_entity_type(value) for value in request.entity_types if value.strip()]
        frontier_sql = _placeholders(len(frontier_values))
        edge_type_sql = _placeholders(len(edge_types))
        clauses = [
            "e.dataset_scope = ?",
            "e.paper_count >= ?",
            f"e.edge_type IN ({edge_type_sql})",
            f"(e.source_global_entity_id IN ({frontier_sql}) OR e.target_global_entity_id IN ({frontier_sql}))",
            "ss.paper_count >= ?",
            "ts.paper_count >= ?",
        ]
        params: list[Any] = [request.dataset, request.edge_min_weight, *edge_types, *frontier_values, *frontier_values]
        params.extend([request.min_paper_count, request.min_paper_count])
        if normalized_types:
            type_sql = _placeholders(len(normalized_types))
            clauses.extend(
                [
                    f"se.normalized_entity_type IN ({type_sql})",
                    f"te.normalized_entity_type IN ({type_sql})",
                ]
            )
            params.extend(normalized_types)
            params.extend(normalized_types)
        params.append(max(200, request.limit * 20))
        return self._fetchall(
            f"""
            SELECT
                e.edge_id, e.edge_type,
                e.source_global_entity_id, e.target_global_entity_id,
                e.relation_name, e.normalized_relation_name, e.directed,
                e.paper_count, e.mention_count, e.evidence_papers
            FROM global_semantic_edges e
            JOIN global_semantic_entity_stats ss
              ON ss.dataset_scope = e.dataset_scope
             AND ss.global_entity_id = e.source_global_entity_id
            JOIN global_semantic_entity_stats ts
              ON ts.dataset_scope = e.dataset_scope
             AND ts.global_entity_id = e.target_global_entity_id
            JOIN global_semantic_entities se
              ON se.dataset_scope = e.dataset_scope
             AND se.global_entity_id = e.source_global_entity_id
            JOIN global_semantic_entities te
              ON te.dataset_scope = e.dataset_scope
             AND te.global_entity_id = e.target_global_entity_id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.paper_count DESC, e.edge_type, e.edge_id
            LIMIT ?
            """,
            tuple(params),
        )

    def _entity_rows(
        self,
        dataset: str,
        entity_ids: Iterable[str],
        min_paper_count: int,
        entity_types: list[str],
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return []
        clauses = [
            "e.dataset_scope = ?",
            f"e.global_entity_id IN ({_placeholders(len(ids))})",
            "s.paper_count >= ?",
        ]
        params: list[Any] = [dataset, *ids, min_paper_count]
        normalized_types = [normalize_entity_type(value) for value in entity_types if value.strip()]
        if normalized_types:
            clauses.append(f"e.normalized_entity_type IN ({_placeholders(len(normalized_types))})")
            params.extend(normalized_types)
        return self._fetchall(
            f"""
            SELECT
                e.global_entity_id, e.name_group_id,
                e.canonical_name, e.normalized_name,
                e.canonical_entity_type, e.normalized_entity_type,
                e.type_distribution,
                s.paper_count, s.mention_count, s.datasets, s.example_papers
            FROM global_semantic_entities e
            JOIN global_semantic_entity_stats s
              ON s.dataset_scope = e.dataset_scope
             AND s.global_entity_id = e.global_entity_id
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )

    def _latest_build(self, dataset: str) -> dict[str, Any] | None:
        rows = self._fetchall(
            """
            SELECT build_id, min_paper_count, completed_at
            FROM global_semantic_graph_builds
            WHERE dataset_scope = ? AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (dataset,),
        )
        return rows[0] if rows else None

    def _load_example_paper_details(
        self,
        entity_rows: Iterable[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        paper_ids = list(
            dict.fromkeys(
                paper_id
                for row in entity_rows
                for paper_id in _json_list(row.get("example_papers"))
            )
        )
        return self._load_paper_details(paper_ids)

    def _load_evidence_paper_details(
        self,
        edge_rows: Iterable[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        paper_ids = list(
            dict.fromkeys(
                paper_id
                for row in edge_rows
                for paper_id in _json_list(row.get("evidence_papers"))
            )
        )
        return self._load_paper_details(paper_ids)

    def _load_paper_details(
        self,
        paper_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not paper_ids:
            return {}
        rows = self._fetchall(
            f"""
            SELECT paperid, title, year
            FROM cn_paperdetails
            WHERE paperid IN ({_placeholders(len(paper_ids))})
            """,
            tuple(paper_ids),
        )
        return {str(row["paperid"]): row for row in rows}

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            if self.sqlite_path:
                with sqlite3.connect(self.sqlite_path) as connection:
                    connection.row_factory = sqlite3.Row
                    return [dict(row) for row in connection.execute(sql, params).fetchall()]
            with psycopg2.connect(**self.connection_params) as connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(sql.replace("?", "%s"), params)
                    return [dict(row) for row in cursor.fetchall()]
        except (sqlite3.DatabaseError, psycopg2.Error) as exc:
            raise GraphDemoError(
                "global_graph_not_ready",
                "Global semantic graph is unavailable; run its migration and build first",
                {"error": str(exc)},
            ) from exc


def _entity_item(
    row: dict[str, Any],
    paper_details: dict[str, dict[str, Any]] | None = None,
) -> GlobalSemanticEntityItem:
    example_papers = _json_list(row.get("example_papers"))
    details_by_id = paper_details or {}
    return GlobalSemanticEntityItem(
        globalEntityId=str(row["global_entity_id"]),
        nameGroupId=str(row["name_group_id"]),
        canonicalName=str(row["canonical_name"]),
        normalizedName=str(row["normalized_name"]),
        entityType=str(row["canonical_entity_type"]),
        normalizedEntityType=str(row["normalized_entity_type"]),
        typeDistribution=_json_object(row.get("type_distribution")),
        paperCount=int(row.get("paper_count") or 0),
        mentionCount=int(row.get("mention_count") or 0),
        datasets=_json_list(row.get("datasets")),
        examplePapers=example_papers,
        examplePaperDetails=[
            {
                "paperId": paper_id,
                "title": _optional_string(details_by_id.get(paper_id, {}).get("title")),
                "year": _optional_int(details_by_id.get(paper_id, {}).get("year")),
            }
            for paper_id in example_papers
        ],
    )


def _expand_entity_node(
    row: dict[str, Any],
    index: int,
    total: int,
    dataset: str,
    evidence_limit: int,
    paper_details: dict[str, dict[str, Any]],
) -> GraphNode:
    item = _entity_item(row, paper_details)
    example_papers = item.example_papers[:evidence_limit]
    details = [
        {
            "paperId": paper_id,
            "title": _optional_string(paper_details.get(paper_id, {}).get("title")),
            "year": _optional_int(paper_details.get(paper_id, {}).get("year")),
        }
        for paper_id in example_papers
    ]
    angle = (2 * math.pi * index) / max(total, 1)
    radius = 0 if index == 0 else 240 + 40 * (index // 24)
    return GraphNode(
        id=item.global_entity_id,
        nodeType="global_semantic_entity",
        label=item.canonical_name,
        summary=item.entity_type,
        metric=float(item.paper_count),
        paperIds=example_papers,
        paperCount=item.paper_count,
        position={
            "x": round(radius * math.cos(angle), 2),
            "y": round(radius * math.sin(angle), 2),
        },
        data={
            "globalEntityId": item.global_entity_id,
            "canonicalName": item.canonical_name,
            "nameGroupId": item.name_group_id,
            "normalizedName": item.normalized_name,
            "entityType": item.entity_type,
            "normalizedEntityType": item.normalized_entity_type,
            "typeDistribution": item.type_distribution,
            "paperCount": item.paper_count,
            "mentionCount": item.mention_count,
            "datasetScope": dataset,
            "datasets": item.datasets,
            "examplePapers": example_papers,
            "examplePaperDetails": details,
        },
    )


def _expand_paper_node(
    paper_id: str,
    detail: dict[str, Any],
    index: int,
    total: int,
    dataset: str,
) -> GraphNode:
    title = _optional_string(detail.get("title")) or paper_id
    year = _optional_int(detail.get("year"))
    venue = _optional_string(detail.get("venue"))
    return GraphNode(
        id=f"paper_{paper_id}",
        nodeType="paper",
        label=title,
        title=title,
        year=year,
        venue=venue,
        summary=title,
        metric=0,
        paperIds=[paper_id],
        position={"x": index * 180, "y": 260},
        data={
            "paperId": paper_id,
            "title": title,
            "year": year,
            "venue": venue,
            "dataset": _optional_string(detail.get("dataset")) or dataset,
            "datasetScope": dataset,
        },
    )


def _expand_global_edge(
    row: dict[str, Any],
    evidence_limit: int,
    paper_details: dict[str, dict[str, Any]],
    available_papers: set[str],
    dataset: str,
) -> dict[str, Any]:
    edge_type = str(row["edge_type"])
    relation_name = row.get("relation_name")
    evidence_papers = [
        paper_id
        for paper_id in _json_list(row.get("evidence_papers"))
        if paper_id in available_papers
    ][:evidence_limit]
    return {
        "id": str(row["edge_id"]),
        "source": str(row["source_global_entity_id"]),
        "target": str(row["target_global_entity_id"]),
        "type": edge_type,
        "edgeType": edge_type,
        "label": str(relation_name or "co-occurrence"),
        "data": {
            "directed": bool(row.get("directed")),
            "weight": int(row.get("paper_count") or 0),
            "paperCount": int(row.get("paper_count") or 0),
            "mentionCount": int(row.get("mention_count") or 0),
            "relationName": relation_name,
            "normalizedRelationName": row.get("normalized_relation_name"),
            "datasetScope": dataset,
            "evidencePapers": evidence_papers,
            "evidencePaperDetails": [
                {
                    "paperId": paper_id,
                    "title": _optional_string(
                        paper_details.get(paper_id, {}).get("title")
                    ),
                    "year": _optional_int(
                        paper_details.get(paper_id, {}).get("year")
                    ),
                }
                for paper_id in evidence_papers
            ],
        },
    }


def _stable_expand_id(*parts: str) -> str:
    basis = "".join(str(part) for part in parts)
    return f"edge_expand_{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:24]}"


def _graph_node(
    row: dict[str, Any],
    index: int,
    total: int,
    paper_details: dict[str, dict[str, Any]] | None = None,
) -> GraphNode:
    item = _entity_item(row, paper_details)
    angle = (2 * math.pi * index) / max(total, 1)
    radius = 0 if index == 0 else 240 + 40 * (index // 24)
    return GraphNode(
        id=item.global_entity_id,
        nodeType="global_semantic_entity",
        label=item.canonical_name,
        summary=item.entity_type,
        metric=float(item.paper_count),
        paperIds=item.example_papers,
        paperCount=item.paper_count,
        position={"x": round(radius * math.cos(angle), 2), "y": round(radius * math.sin(angle), 2)},
        data={
            "globalEntityId": item.global_entity_id,
            "nameGroupId": item.name_group_id,
            "normalizedName": item.normalized_name,
            "entityType": item.entity_type,
            "normalizedEntityType": item.normalized_entity_type,
            "typeDistribution": item.type_distribution,
            "paperCount": item.paper_count,
            "mentionCount": item.mention_count,
            "datasets": item.datasets,
            "examplePapers": item.example_papers,
            "examplePaperDetails": [
                detail.model_dump(by_alias=True) for detail in item.example_paper_details
            ],
        },
    )


def _graph_edge(
    row: dict[str, Any],
    paper_details: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    edge_type = str(row["edge_type"])
    relation_name = row.get("relation_name")
    label = str(relation_name or "co-occurrence")
    evidence_papers = _json_list(row.get("evidence_papers"))
    details_by_id = paper_details or {}
    return {
        "id": str(row["edge_id"]),
        "source": str(row["source_global_entity_id"]),
        "target": str(row["target_global_entity_id"]),
        "type": edge_type,
        "label": label,
        "data": {
            "directed": bool(row.get("directed")),
            "weight": int(row.get("paper_count") or 0),
            "paperCount": int(row.get("paper_count") or 0),
            "mentionCount": int(row.get("mention_count") or 0),
            "relationName": relation_name,
            "normalizedRelationName": row.get("normalized_relation_name"),
            "evidencePapers": evidence_papers,
            "evidencePaperDetails": [
                {
                    "paperId": paper_id,
                    "title": _optional_string(details_by_id.get(paper_id, {}).get("title")),
                    "year": _optional_int(details_by_id.get(paper_id, {}).get("year")),
                }
                for paper_id in evidence_papers
            ],
        },
    }


def _json_list(value: Any) -> list[str]:
    parsed = _json_value(value, [])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, int]:
    parsed = _json_value(value, {})
    if not isinstance(parsed, dict):
        return {}
    return {str(key): int(count) for key, count in parsed.items()}


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))
