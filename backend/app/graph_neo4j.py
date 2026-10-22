from __future__ import annotations

import time
from typing import Any

from app.config import get_env
from app.logging_config import get_logger
from app.resource_usage import finish_resource_span, start_resource_span
from app.models import GraphNode, GraphResponse, PaperSearchItem


logger = get_logger("bigdipper.graph_neo4j")


class Neo4jUnavailable(RuntimeError):
    pass


class Neo4jGraphService:
    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None) -> None:
        self.uri = uri or get_env("BIGDIPPER_NEO4J_URI", "bolt://127.0.0.1:7778")
        self.user = user or get_env("BIGDIPPER_NEO4J_USER", "neo4j")
        self.password = password or get_env("BIGDIPPER_NEO4J_PASSWORD", "")
        self.enabled = (get_env("BIGDIPPER_NEO4J_ENABLED", "false") or "false").lower() == "true"
        self.ensure_indexes = (get_env("BIGDIPPER_NEO4J_ENSURE_INDEXES", "true") or "true").lower() == "true"
        self.index_await_seconds = int(get_env("BIGDIPPER_NEO4J_INDEX_AWAIT_SECONDS", "30") or "30")
        self._driver = None
        self._indexes_checked = False

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        try:
            self._ensure_driver().verify_connectivity()
            return True
        except Exception as exc:
            logger.info("neo4j=unavailable error=%s", exc)
            return False

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def paper_references(self, paper_id: str, workspace_id: str | None, limit: int) -> GraphResponse:
        papers = self._run(_REF_PAPERS, {"paperid": paper_id, "limit": limit})
        paper_ids = _paper_ids(papers, paper_id)
        edges = self._citation_edges(paper_ids)
        return _graph_response("paper_references", workspace_id, paper_id, papers, edges, f"References: {paper_id}", f"References for {paper_id}")

    def paper_citations(self, paper_id: str, workspace_id: str | None, limit: int) -> GraphResponse:
        papers = self._run(_CITE_PAPERS, {"paperid": paper_id, "limit": limit})
        paper_ids = _paper_ids(papers, paper_id)
        edges = self._citation_edges(paper_ids)
        return _graph_response("paper_citations", workspace_id, paper_id, papers, edges, f"Cited By: {paper_id}", f"Citations for {paper_id}")

    def paper_neighborhood(
        self,
        paper_id: str,
        workspace_id: str | None,
        limit: int,
        include_references: bool = True,
        include_citations: bool = True,
        include_authors: bool = True,
        include_institutions: bool = True,
        include_fields: bool = True,
    ) -> GraphResponse:
        papers: list[dict[str, Any]] = []
        if include_references:
            papers.extend(self._run(_REF_PAPERS, {"paperid": paper_id, "limit": limit}))
        if include_citations:
            papers.extend(self._run(_CITE_PAPERS, {"paperid": paper_id, "limit": limit}))
        if not papers:
            papers = self._run(_SEED_PAPER, {"paperid": paper_id})
        papers = _dedupe_by(papers, "paperid")
        paper_ids = _paper_ids(papers, paper_id)
        edges = self._citation_edges(paper_ids)
        extra_nodes: list[GraphNode] = []
        extra_edges: list[dict[str, Any]] = []
        if include_authors:
            authors, author_edges, institutions, institution_edges = self._paper_authors_and_institutions(paper_id, include_institutions)
            extra_nodes.extend(authors)
            extra_nodes.extend(institutions)
            extra_edges.extend(author_edges)
            extra_edges.extend(institution_edges)
        if include_fields:
            fields, field_edges = self._paper_fields(paper_id)
            extra_nodes.extend(fields)
            extra_edges.extend(field_edges)
        graph = _graph_response("paper_neighborhood", workspace_id, paper_id, papers, edges + extra_edges, f"Neighborhood: {paper_id}", f"References and citations for {paper_id}")
        graph.nodes.extend(extra_nodes)
        graph.nodes = _dedupe_nodes(graph.nodes)
        graph.edges = _dedupe_edges(graph.edges)
        return graph

    def paper_connections(self, paper_id: str, workspace_id: str | None, limit: int) -> GraphResponse:
        safe_limit = min(max(int(limit or 10), 1), 10)
        warnings: list[str] = []

        started = time.monotonic()
        seed_rows = self._run(_SEED_PAPER, {"paperid": paper_id})
        logger.info("paper_connections_seed=end elapsed=%.3f rows=%d", time.monotonic() - started, len(seed_rows))
        if not seed_rows:
            return _graph_response(
                "paper_connections",
                workspace_id,
                paper_id,
                [],
                [],
                f"Connections: {paper_id}",
                "Paper connections",
            )

        started = time.monotonic()
        authors, author_edges, institutions, institution_edges = self._paper_authors_and_institutions(paper_id, True)
        logger.info(
            "paper_connections_authors_institutions=end elapsed=%.3f authors=%d institutions=%d",
            time.monotonic() - started,
            len(authors),
            len(institutions),
        )
        if not authors:
            warnings.append("Some paper connection details were unavailable.")

        started = time.monotonic()
        ref_rows = self._run(_CONNECTION_REF_PAPERS, {"paperid": paper_id, "limit": safe_limit})
        logger.info("paper_connections_references=end elapsed=%.3f rows=%d", time.monotonic() - started, len(ref_rows))

        started = time.monotonic()
        cite_rows = self._run(_CONNECTION_CITE_PAPERS, {"paperid": paper_id, "limit": safe_limit})
        logger.info("paper_connections_citations=end elapsed=%.3f rows=%d", time.monotonic() - started, len(cite_rows))

        papers = _dedupe_by(seed_rows + ref_rows + cite_rows, "paperid")
        paper_ids = _paper_ids(papers, paper_id)
        edges: list[dict[str, Any]] = []
        for ref in ref_rows:
            ref_id = str(ref.get("paperid") or "")
            if ref_id and ref_id != paper_id:
                edges.append({"id": f"edge-{paper_id}-{ref_id}-cites", "source": paper_id, "target": ref_id, "type": "cites", "label": "cites"})
        for citing in cite_rows:
            citing_id = str(citing.get("paperid") or "")
            if citing_id and citing_id != paper_id:
                edges.append({"id": f"edge-{citing_id}-{paper_id}-cites", "source": citing_id, "target": paper_id, "type": "cites", "label": "cites"})

        started = time.monotonic()
        induced_edges = self._citation_edges(paper_ids)
        logger.info("paper_connections_induced_edges=end elapsed=%.3f edges=%d", time.monotonic() - started, len(induced_edges))
        edges.extend(induced_edges)
        edges.extend(author_edges)
        edges.extend(institution_edges)
        edges.extend(_coauthor_edges(authors, paper_id))

        seed_title = str(seed_rows[0].get("title") or paper_id)
        graph = _graph_response(
            "paper_connections",
            workspace_id,
            paper_id,
            papers,
            edges,
            f"Connections: {seed_title}",
            "Paper connections",
        )
        graph.nodes.extend(authors)
        graph.nodes.extend(institutions)
        graph.nodes = _dedupe_nodes(graph.nodes)
        graph.edges = _dedupe_edges(graph.edges)
        graph.warnings = warnings
        return graph

    def citation_edges_among(self, paper_ids: list[str]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise Neo4jUnavailable("Neo4j graph expansion is disabled")
        return self._citation_edges(paper_ids)

    def induced_citation_edges(self, paper_ids: list[str]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise Neo4jUnavailable("Neo4j graph expansion is disabled")
        return self._citation_edges(paper_ids)

    def _citation_edges(self, paper_ids: list[str]) -> list[dict[str, Any]]:
        if len(paper_ids) < 2:
            return []
        rows = self._run(_INDUCED_CITES, {"paperids": paper_ids})
        return [_cite_edge(row) for row in rows if str(row.get("source")) != str(row.get("target"))]

    def _paper_authors_and_institutions(self, paper_id: str, include_institutions: bool) -> tuple[list[GraphNode], list[dict[str, Any]], list[GraphNode], list[dict[str, Any]]]:
        rows = self._run(_AUTHORS_INSTITUTIONS, {"paperid": paper_id})
        authors: list[GraphNode] = []
        author_edges: list[dict[str, Any]] = []
        institutions: list[GraphNode] = []
        institution_edges: list[dict[str, Any]] = []
        seen_authors: set[str] = set()
        seen_inst: set[str] = set()
        for index, row in enumerate(rows):
            author_id = str(row.get("authorid") or "")
            if author_id and author_id not in seen_authors:
                node_id = f"author-{author_id}"
                authors.append(GraphNode(id=node_id, nodeType="author", label=str(row.get("author_name") or author_id), summary="Author on seed paper", metric=float(_int(row.get("h_index") or row.get("productivity"))), paperIds=[paper_id], publicationCount=1, institutionId=f"institution-{row.get('institution_id')}" if row.get("institution_id") else None, institution=row.get("institution_name"), position={"x": index * 42, "y": 170}, data=dict(row)))
                author_edges.append({"id": f"edge-{paper_id}-{node_id}", "source": paper_id, "target": node_id, "type": "authored_by", "label": "authored by"})
                seen_authors.add(author_id)
            institution_id = str(row.get("institution_id") or "")
            if include_institutions and institution_id:
                inst_node_id = f"institution-{institution_id}"
                if inst_node_id not in seen_inst:
                    institutions.append(GraphNode(id=inst_node_id, nodeType="institution", label=str(row.get("institution_name") or institution_id), summary="Paper-scoped affiliation", metric=1.0, country=row.get("country_code") or "Unknown", authorIds=[f"author-{author_id}"] if author_id else [], paperIds=[paper_id], paperCount=1, position={"x": index * 80, "y": 350}, data=dict(row)))
                    seen_inst.add(inst_node_id)
                if author_id:
                    institution_edges.append({"id": f"edge-author-{author_id}-institution-{institution_id}-{paper_id}", "source": f"author-{author_id}", "target": inst_node_id, "type": "affiliated_with", "label": "affiliated with", "data": {"paperid": paper_id, "rawAffiliation": row.get("raw_affiliation_string")}})
        return authors, author_edges, institutions, institution_edges

    def _paper_fields(self, paper_id: str) -> tuple[list[GraphNode], list[dict[str, Any]]]:
        rows = self._run(_FIELDS, {"paperid": paper_id})
        nodes: list[GraphNode] = []
        edges: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            field_id = str(row.get("fieldid") or "")
            if not field_id:
                continue
            node_id = f"field-{field_id}"
            nodes.append(GraphNode(id=node_id, nodeType="field", label=str(row.get("field_name") or field_id), summary="Field", metric=float(row.get("score_openalex") or row.get("cited_by_count") or 0), position={"x": index * 110, "y": -170}, data=dict(row)))
            edges.append({"id": f"edge-{paper_id}-{node_id}", "source": paper_id, "target": node_id, "type": "has_field", "label": "has field", "data": {"score": row.get("score_openalex")}})
        return nodes, edges

    def _run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        started = time.monotonic()
        resource_span = start_resource_span("Neo4j query", "graph", backend="neo4j")
        try:
            with self._ensure_driver().session() as session:
                rows = [dict(record) for record in session.run(cypher, params)]
            logger.info("neo4j_query=end elapsed=%.3f rows=%s", time.monotonic() - started, len(rows))
            finish_resource_span(resource_span, status="success")
            return rows
        except Exception as exc:
            finish_resource_span(resource_span, status="failed")
            logger.exception("neo4j_query=error error=%s", exc)
            raise Neo4jUnavailable(str(exc)) from exc

    def _ensure_driver(self):
        if self._driver is not None:
            self._ensure_indexes()
            return self._driver
        try:
            from neo4j import GraphDatabase
        except Exception as exc:
            raise Neo4jUnavailable("neo4j Python driver is not installed") from exc
        self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password), connection_timeout=2)
        self._ensure_indexes()
        return self._driver

    def _ensure_indexes(self) -> None:
        if self._indexes_checked or not self.enabled or not self.ensure_indexes or self._driver is None:
            return
        statements = [
            "CREATE INDEX paper_paperid IF NOT EXISTS FOR (p:Paper) ON (p.paperid)",
            "CREATE INDEX author_authorid IF NOT EXISTS FOR (a:Author) ON (a.authorid)",
            "CREATE INDEX institution_institution_id IF NOT EXISTS FOR (i:Institution) ON (i.institution_id)",
            "CREATE INDEX field_fieldid IF NOT EXISTS FOR (f:Field) ON (f.fieldid)",
        ]
        started = time.monotonic()
        try:
            with self._driver.session() as session:
                for statement in statements:
                    session.run(statement).consume()
                if self.index_await_seconds > 0:
                    session.run("CALL db.awaitIndexes($seconds)", {"seconds": self.index_await_seconds}).consume()
            logger.info("neo4j_indexes=ready elapsed=%.3f", time.monotonic() - started)
        except Exception as exc:
            logger.warning("neo4j_indexes=unavailable error=%s", exc)
        finally:
            self._indexes_checked = True


_SEED_PAPER = """
MATCH (p:Paper {paperid: $paperid})
RETURN p.paperid AS paperid, p.title AS title, p.year AS year, p.doi AS doi, p.cited_by_count AS cited_by_count
"""

_REF_PAPERS = """
MATCH (p:Paper {paperid: $paperid})
OPTIONAL MATCH (p)-[:CITES]->(ref:Paper)
WITH p, collect(ref)[0..$limit] AS refs
UNWIND [p] + refs AS paper
WITH DISTINCT paper WHERE paper IS NOT NULL
RETURN paper.paperid AS paperid, paper.title AS title, paper.year AS year, paper.doi AS doi, paper.cited_by_count AS cited_by_count
"""

_CITE_PAPERS = """
MATCH (p:Paper {paperid: $paperid})
OPTIONAL MATCH (citing:Paper)-[:CITES]->(p)
WITH p, collect(citing)[0..$limit] AS citing_papers
UNWIND [p] + citing_papers AS paper
WITH DISTINCT paper WHERE paper IS NOT NULL
RETURN paper.paperid AS paperid, paper.title AS title, paper.year AS year, paper.doi AS doi, paper.cited_by_count AS cited_by_count
"""

_CONNECTION_REF_PAPERS = """
MATCH (p:Paper {paperid: $paperid})-[:CITES]->(ref:Paper)
RETURN ref.paperid AS paperid, ref.title AS title, ref.year AS year, ref.doi AS doi, ref.cited_by_count AS cited_by_count
ORDER BY coalesce(ref.cited_by_count, 0) DESC
LIMIT $limit
"""

_CONNECTION_CITE_PAPERS = """
MATCH (citing:Paper)-[:CITES]->(p:Paper {paperid: $paperid})
RETURN citing.paperid AS paperid, citing.title AS title, citing.year AS year, citing.doi AS doi, citing.cited_by_count AS cited_by_count
ORDER BY coalesce(citing.cited_by_count, 0) DESC
LIMIT $limit
"""

_INDUCED_CITES = """
UNWIND $paperids AS source_id
MATCH (source:Paper {paperid: source_id})-[r:CITES]->(target:Paper)
WHERE target.paperid IN $paperids AND source.paperid <> target.paperid
RETURN source.paperid AS source, target.paperid AS target
"""

_AUTHORS_INSTITUTIONS = """
MATCH (a:Author)-[auth:AUTHORED]->(p:Paper {paperid: $paperid})
OPTIONAL MATCH (a)-[aff:AFFILIATED_WITH {paperid: $paperid}]->(i:Institution)
RETURN a.authorid AS authorid, a.display_name AS author_name, a.h_index AS h_index, a.productivity AS productivity,
       auth.author_position AS author_position, i.institution_id AS institution_id, i.display_name AS institution_name,
       i.country_code AS country_code, i.type AS institution_type, aff.raw_affiliation_string AS raw_affiliation_string
LIMIT 100
"""

_FIELDS = """
MATCH (p:Paper {paperid: $paperid})-[r:HAS_FIELD]->(f:Field)
RETURN f.fieldid AS fieldid, f.display_name AS field_name, f.level AS level, f.works_count AS works_count,
       f.cited_by_count AS cited_by_count, r.score_openalex AS score_openalex
ORDER BY r.score_openalex DESC
LIMIT 20
"""


def _graph_response(kind: str, workspace_id: str | None, seed_id: str, rows: list[dict[str, Any]], edges: list[dict[str, Any]], title: str, context: str) -> GraphResponse:
    papers = [_paper(row, index) for index, row in enumerate(_dedupe_by(rows, "paperid"))]
    nodes = [_paper_node(paper, index) for index, paper in enumerate(papers)]
    return GraphResponse(graphId=f"graph_neo4j_{kind}_{abs(hash((seed_id, len(nodes), len(edges))))}", workspaceId=workspace_id, title=title, queryContext=context, seedNodeId=seed_id, seedPaperId=seed_id, defaultViewMode="graph", nodes=nodes, papers=papers, edges=_dedupe_edges(edges), warnings=[])


def _paper(row: dict[str, Any], index: int) -> PaperSearchItem:
    year = _int(row.get("year"))
    citations = _int(row.get("cited_by_count"))
    title = str(row.get("title") or "Untitled paper")
    label = title if title and title != "Untitled paper" else str(row.get("paperid") or f"paper_{index}")
    return PaperSearchItem(id=str(row.get("paperid") or f"paper_{index}"), nodeType="paper", label=label, title=title, year=year, authors=[], venue=str(row.get("venue") or ""), abstract=str(row.get("abstract") or ""), doi=row.get("doi"), citationCount=citations, topic=str(row.get("topic") or ""), doctype=str(row.get("doctype") or "") or None, language=str(row.get("language") or "") or None, summary=str(row.get("abstract") or title), metric=float(citations), position={"x": index * 150, "y": 0})


def _paper_node(paper: PaperSearchItem, index: int) -> GraphNode:
    node = GraphNode.model_validate(paper.model_dump(by_alias=True))
    node.position.x = float(index * 150)
    node.data = paper.model_dump(by_alias=True)
    node.data["source"] = "helmdb"
    return node


def _coauthor_edges(authors: list[GraphNode], paper_id: str) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    author_ids = [author.id for author in authors if author.id]
    for left_index, left_id in enumerate(author_ids):
        for right_id in author_ids[left_index + 1:]:
            edges.append(
                {
                    "id": f"edge-{left_id}-{right_id}-{paper_id}-coauthored_with",
                    "source": left_id,
                    "target": right_id,
                    "type": "coauthored_with",
                    "label": "coauthor",
                    "data": {"paperId": paper_id},
                }
            )
    return edges


def _cite_edge(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source"))
    target = str(row.get("target"))
    edge = {"id": f"edge-{source}-{target}-cites", "source": source, "target": target, "type": "cites", "label": "cites"}
    data = {"year": row.get("year"), "refYear": row.get("ref_year"), "yearDiff": row.get("year_diff")}
    if any(value is not None for value in data.values()):
        edge["data"] = data
    return edge


def _paper_ids(rows: list[dict[str, Any]], seed_id: str) -> list[str]:
    ids = [seed_id]
    for row in rows:
        paper_id = str(row.get("paperid") or "")
        if paper_id and paper_id not in ids:
            ids.append(paper_id)
    return ids


def _dedupe_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        value = str(row.get(key) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(row)
    return unique


def _dedupe_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    seen: set[str] = set()
    unique: list[GraphNode] = []
    for node in nodes:
        if node.id in seen:
            continue
        seen.add(node.id)
        unique.append(node)
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


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
