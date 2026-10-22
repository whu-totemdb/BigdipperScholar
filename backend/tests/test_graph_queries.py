from app.graph_demo import GraphDemoService

from app.graph_neo4j import (
    Neo4jGraphService,
    Neo4jUnavailable,
    _AUTHORS_INSTITUTIONS,
    _CITE_PAPERS,
    _CONNECTION_CITE_PAPERS,
    _CONNECTION_REF_PAPERS,
    _INDUCED_CITES,
    _REF_PAPERS,
    _SEED_PAPER,
)
from app.models import GraphNode, GraphQueryRequest, GraphResponse, PaperSearchItem
from app.tools import MockToolProvider


def _paper(paper_id: str, title: str = "Seed Paper") -> PaperSearchItem:
    return PaperSearchItem(
        id=paper_id,
        nodeType="paper",
        label="Unknown, 2024",
        title=title,
        year=2024,
        authors=[],
        venue="",
        abstract="abstract",
        doi=None,
        citationCount=10,
        topic="",
        summary=title,
        metric=10,
        position={"x": 0, "y": 0},
    )


def test_paper_search_returns_title_candidates_without_graph_expansion() -> None:
    candidates = [_paper("W1", "Matched Title"), _paper("W2", "Another Match")]
    service = GraphDemoService(
        fallback_provider=MockToolProvider(),
        title_provider=FakeTitleProvider(candidates),
        neo4j_service=FakeGraphService(),
        similar_service=FakeSimilarService(),
    )

    graph = service.query_graph("paper_search", "ws_1", query="Matched Title", limit=10)

    assert graph.seed_paper_id is None
    assert graph.seed_node_id is None
    assert graph.default_view_mode == "list"
    assert [paper.id for paper in graph.papers] == ["W1", "W2"]
    assert [node.node_type for node in graph.nodes] == ["paper", "paper"]
    assert graph.edges == []
    assert graph.warnings == []


def test_neo4j_references_maps_cites_edges() -> None:
    service = FakeNeo4jRowsService(mode="references")

    graph = service.paper_references("W1", "ws_1", 10)

    assert {paper.id for paper in graph.papers} == {"W1", "W2"}
    assert graph.seed_paper_id == "W1"
    assert graph.edges == [
        {
            "id": "edge-W1-W2-cites",
            "source": "W1",
            "target": "W2",
            "type": "cites",
            "label": "cites",
            "data": {"year": 2024, "refYear": 2020, "yearDiff": 4},
        }
    ]


def test_neo4j_citations_maps_reverse_cites_edges() -> None:
    service = FakeNeo4jRowsService(mode="citations")

    graph = service.paper_citations("W1", "ws_1", 10)

    assert {paper.id for paper in graph.papers} == {"W1", "W3"}
    assert graph.edges[0]["source"] == "W3"
    assert graph.edges[0]["target"] == "W1"
    assert graph.edges[0]["type"] == "cites"


def test_paper_similar_does_not_fabricate_neighbors_when_unavailable() -> None:
    service = GraphDemoService(
        fallback_provider=MockToolProvider(),
        title_provider=FakeTitleProvider([]),
        neo4j_service=FakeUnavailableGraphService(),
        similar_service=FakeSimilarService(),
    )

    graph = service.query_graph("paper_similar", "ws_1", query="graph RAG", limit=5)

    assert graph.papers == []
    assert graph.default_view_mode == "list"
    assert graph.edges == []
    assert any("No verified similarity results" in warning for warning in graph.warnings)


def test_paper_connections_query_type_is_accepted() -> None:
    request = GraphQueryRequest.model_validate({"queryType": "paper_connections", "seedPaperId": "W1"})

    assert request.query_type == "paper_connections"
    assert request.seed_paper_id == "W1"


def test_neo4j_paper_connections_builds_seed_author_institution_and_citation_edges() -> None:
    service = FakeNeo4jRowsService(mode="connections")

    graph = service.paper_connections("W1", "ws_1", 20)

    assert graph.query_context == "Paper connections"
    assert graph.seed_paper_id == "W1"
    assert {paper.id for paper in graph.papers} == {"W1", "W2", "W3"}
    node_types = {node.node_type for node in graph.nodes}
    assert {"paper", "author", "institution"}.issubset(node_types)
    edge_types = {edge["type"] for edge in graph.edges}
    assert {"cites", "authored_by", "affiliated_with", "coauthored_with"}.issubset(edge_types)
    assert any(edge["source"] == "W1" and edge["target"] == "W2" for edge in graph.edges)
    assert any(edge["source"] == "W3" and edge["target"] == "W1" for edge in graph.edges)


def test_paper_connections_neo4j_unavailable_returns_seed_only_warning() -> None:
    service = GraphDemoService(
        fallback_provider=MockToolProvider(),
        title_provider=FakeTitleProvider([]),
        neo4j_service=FakeUnavailableGraphService(),
        similar_service=FakeSimilarService(),
    )

    graph = service.query_graph("paper_connections", "ws_1", seed_paper_id="p-data-agents-2024", limit=10)

    assert graph.seed_paper_id == "p-data-agents-2024"
    assert [node.node_type for node in graph.nodes] == ["paper"]
    assert any("HelmDB relationship data is temporarily unavailable" in warning for warning in graph.warnings)


class FakeTitleProvider:
    def __init__(self, papers: list[PaperSearchItem]) -> None:
        self.papers = papers

    def search_title(self, query: str, limit: int = 10):
        del query
        return self.papers[:limit], []


class FakeGraphService:
    def paper_neighborhood(self, paper_id, workspace_id, limit, **kwargs):
        del limit, kwargs
        paper = _paper(paper_id, "Matched Title")
        node = GraphNode.model_validate(paper.model_dump(by_alias=True))
        author = GraphNode(id="author-A1", nodeType="author", label="Author One", paperIds=[paper_id], position={"x": 0, "y": 100})
        institution = GraphNode(id="institution-I1", nodeType="institution", label="Institution One", authorIds=["author-A1"], position={"x": 0, "y": 200})
        return GraphResponse(
            graphId="graph_fake",
            workspaceId=workspace_id,
            title=f"Neighborhood: {paper_id}",
            queryContext="fake neo4j",
            seedNodeId=paper_id,
            seedPaperId=paper_id,
            defaultViewMode="graph",
            nodes=[node, author, institution],
            papers=[paper],
            edges=[{"id": "edge-W1-W2-cites", "source": "W1", "target": "W2", "type": "cites", "label": "cites"}],
            warnings=[],
        )


class FakeUnavailableGraphService:
    def paper_connections(self, paper_id, workspace_id, limit):
        del paper_id, workspace_id, limit
        raise Neo4jUnavailable("neo4j unavailable in test")

    def citation_edges_among(self, paper_ids):
        del paper_ids
        raise Neo4jUnavailable("neo4j unavailable in test")


class FakeSimilarService:
    def search(self, query: str, limit: int = 15, seed_paper_id: str | None = None):
        del query, limit, seed_paper_id
        return [], ["HelmDB similar-paper search is temporarily unavailable; returned fallback results."]


class FakeNeo4jRowsService(Neo4jGraphService):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def _run(self, cypher: str, params: dict):
        del params
        if cypher == _REF_PAPERS:
            return [
                {"paperid": "W1", "title": "Seed", "year": 2024, "doi": None, "cited_by_count": 10},
                {"paperid": "W2", "title": "Reference", "year": 2020, "doi": None, "cited_by_count": 5},
            ]
        if cypher == _CITE_PAPERS:
            return [
                {"paperid": "W1", "title": "Seed", "year": 2024, "doi": None, "cited_by_count": 10},
                {"paperid": "W3", "title": "Citing", "year": 2026, "doi": None, "cited_by_count": 2},
            ]
        if cypher == _INDUCED_CITES and self.mode == "references":
            return [{"source": "W1", "target": "W2", "year": 2024, "ref_year": 2020, "year_diff": 4}]
        if cypher == _INDUCED_CITES and self.mode == "citations":
            return [{"source": "W3", "target": "W1", "year": 2026, "ref_year": 2024, "year_diff": 2}]
        if self.mode == "connections":
            if cypher == _SEED_PAPER:
                return [{"paperid": "W1", "title": "Seed", "year": 2024, "doi": None, "cited_by_count": 100}]
            if cypher == _CONNECTION_REF_PAPERS:
                return [{"paperid": "W2", "title": "Top Reference", "year": 2020, "doi": None, "cited_by_count": 50}]
            if cypher == _CONNECTION_CITE_PAPERS:
                return [{"paperid": "W3", "title": "Top Citing", "year": 2025, "doi": None, "cited_by_count": 30}]
            if cypher == _AUTHORS_INSTITUTIONS:
                return [
                    {"authorid": "A1", "author_name": "Author One", "h_index": 10, "productivity": 20, "institution_id": "I1", "institution_name": "Institution One", "country_code": "CN", "raw_affiliation_string": "Inst One"},
                    {"authorid": "A2", "author_name": "Author Two", "h_index": 8, "productivity": 15, "institution_id": "I2", "institution_name": "Institution Two", "country_code": "US", "raw_affiliation_string": "Inst Two"},
                ]
            if cypher == _INDUCED_CITES:
                return [
                    {"source": "W1", "target": "W2", "year": 2024, "ref_year": 2020, "year_diff": 4},
                    {"source": "W3", "target": "W1", "year": 2025, "ref_year": 2024, "year_diff": 1},
                ]
        return []
