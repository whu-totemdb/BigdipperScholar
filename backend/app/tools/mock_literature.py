from __future__ import annotations

from typing import Any

from app.models import Paper, PaperEdge, ToolSpec
from app.tools.base import ToolProvider


MOCK_PAPERS = [
    Paper(
        id="p-data-agents-2024",
        title="Data Agents for Scientific Discovery",
        year=2024,
        authors=["Mira Chen", "Jonas Patel", "Elena Rossi"],
        venue="Journal of AI for Science",
        abstract=(
            "Introduces agentic workflows for literature search, hypothesis "
            "tracking, and experiment planning over heterogeneous scientific data."
        ),
        doi="10.5555/bigdipper.2024.001",
        citationCount=87,
        topic="data agents",
    ),
    Paper(
        id="p-polystore-2023",
        title="A Polystore Architecture for Research Knowledge Systems",
        year=2023,
        authors=["Anika Rao", "Felix Martin"],
        venue="CIDR Workshops",
        abstract=(
            "Describes a polystore design that combines relational metadata, "
            "graph relationships, and vector indexes for scholarly applications."
        ),
        doi="10.5555/bigdipper.2023.014",
        citationCount=112,
        topic="polystore",
    ),
    Paper(
        id="p-graph-rag-2025",
        title="Graph-RAG for Scholarly Question Answering",
        year=2025,
        authors=["Noah Smith", "Priya Kapoor", "Lena Hofmann"],
        venue="ACL Findings",
        abstract=(
            "Evaluates graph-augmented retrieval for answering citation-aware "
            "questions over scientific corpora."
        ),
        doi="10.5555/bigdipper.2025.022",
        citationCount=39,
        topic="graph rag",
    ),
    Paper(
        id="p-citation-map-2022",
        title="Interactive Citation Maps for Literature Review",
        year=2022,
        authors=["Hannah Lee", "Mateo Garcia"],
        venue="IEEE VIS",
        abstract=(
            "Presents interaction techniques for exploring citation networks, "
            "topic clusters, and evidence trails during literature review."
        ),
        doi="10.5555/bigdipper.2022.007",
        citationCount=164,
        topic="citation graph",
    ),
    Paper(
        id="p-llm-tools-2024",
        title="Tool-Augmented Language Models for Academic Search",
        year=2024,
        authors=["Sofia Nguyen", "Theo Becker"],
        venue="EMNLP",
        abstract=(
            "Studies how language models call search, ranking, and summarization "
            "tools to support academic information seeking."
        ),
        doi="10.5555/bigdipper.2024.033",
        citationCount=73,
        topic="tool use",
    ),
    Paper(
        id="p-evidence-report-2025",
        title="Evidence-Centered Report Generation from Paper Graphs",
        year=2025,
        authors=["Iris Wang", "Samir Haddad"],
        venue="NAACL",
        abstract=(
            "Builds structured research reports from paper graphs while preserving "
            "claim-to-source provenance."
        ),
        doi="10.5555/bigdipper.2025.041",
        citationCount=21,
        topic="evidence reports",
    ),
]

MOCK_EDGES = [
    PaperEdge(id="e1", source="p-graph-rag-2025", target="p-citation-map-2022", type="cites"),
    PaperEdge(id="e2", source="p-graph-rag-2025", target="p-polystore-2023", type="uses_method"),
    PaperEdge(id="e3", source="p-data-agents-2024", target="p-llm-tools-2024", type="extends"),
    PaperEdge(id="e4", source="p-evidence-report-2025", target="p-graph-rag-2025", type="extends"),
    PaperEdge(id="e5", source="p-evidence-report-2025", target="p-citation-map-2022", type="cites"),
    PaperEdge(id="e6", source="p-polystore-2023", target="p-citation-map-2022", type="similar"),
]


class MockToolProvider(ToolProvider):
    def __init__(self) -> None:
        self._papers = {paper.id: paper for paper in MOCK_PAPERS}

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search_papers",
                description="Search mock scholarly papers by title, abstract, author, venue, or topic.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="get_paper_detail",
                description="Return detailed metadata for one mock paper.",
                parameters={
                    "type": "object",
                    "properties": {"paper_id": {"type": "string"}},
                    "required": ["paper_id"],
                },
            ),
            ToolSpec(
                name="get_related_papers",
                description="Return related mock papers for a seed paper.",
                parameters={
                    "type": "object",
                    "properties": {
                        "paper_id": {"type": "string"},
                        "mode": {"type": "string", "enum": ["similar", "references", "cited_by"]},
                    },
                    "required": ["paper_id"],
                },
            ),
            ToolSpec(
                name="get_mock_graph",
                description="Return a small paper graph for frontend visualization.",
                parameters={
                    "type": "object",
                    "properties": {"seed_paper_id": {"type": "string"}},
                },
            ),
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_papers":
            return self.search_papers(
                query=str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 5)),
            )
        if name == "get_paper_detail":
            return self.get_paper_detail(str(arguments.get("paper_id", "")))
        if name == "get_related_papers":
            return self.get_related_papers(
                paper_id=str(arguments.get("paper_id", "")),
                mode=str(arguments.get("mode", "similar")),
            )
        if name == "get_mock_graph":
            seed = arguments.get("seed_paper_id")
            return self.get_mock_graph(str(seed) if seed else None)
        raise ValueError(f"Unknown tool: {name}")

    def search_papers(self, query: str, limit: int = 5) -> dict[str, Any]:
        safe_limit = min(max(limit, 1), 10)
        query_terms = [term for term in query.lower().split() if term]

        scored: list[tuple[int, Paper]] = []
        for paper in MOCK_PAPERS:
            haystack = " ".join(
                [
                    paper.title,
                    paper.abstract,
                    paper.venue,
                    paper.topic,
                    " ".join(paper.authors),
                ]
            ).lower()
            score = sum(haystack.count(term) for term in query_terms)
            if score > 0 or not query_terms:
                scored.append((score, paper))

        if not scored:
            scored = [(0, paper) for paper in MOCK_PAPERS]

        scored.sort(key=lambda item: (item[0], item[1].citation_count, item[1].year), reverse=True)
        papers = [paper for _, paper in scored[:safe_limit]]
        return {
            "papers": [paper.model_dump(by_alias=True) for paper in papers],
            "summary": f"returned {len(papers)} papers",
        }

    def get_paper_detail(self, paper_id: str) -> dict[str, Any]:
        paper = self._papers.get(paper_id)
        if not paper:
            return {"paper": None, "summary": f"paper {paper_id} was not found"}
        return {
            "paper": paper.model_dump(by_alias=True),
            "summary": f"returned details for {paper.title}",
        }

    def get_related_papers(self, paper_id: str, mode: str = "similar") -> dict[str, Any]:
        seed = self._papers.get(paper_id)
        if not seed:
            return {"papers": [], "edges": [], "summary": f"paper {paper_id} was not found"}

        if mode == "references":
            edges = [edge for edge in MOCK_EDGES if edge.source == paper_id]
            related_ids = {edge.target for edge in edges}
        elif mode == "cited_by":
            edges = [edge for edge in MOCK_EDGES if edge.target == paper_id]
            related_ids = {edge.source for edge in edges}
        else:
            edges = [
                edge
                for edge in MOCK_EDGES
                if edge.source == paper_id or edge.target == paper_id or edge.type == "similar"
            ]
            related_ids = {
                edge.target if edge.source == paper_id else edge.source
                for edge in edges
                if paper_id in {edge.source, edge.target}
            }
            topic_matches = {
                paper.id
                for paper in MOCK_PAPERS
                if paper.id != paper_id and _shares_topic(seed.topic, paper.topic)
            }
            related_ids.update(topic_matches)

        papers = [self._papers[paper_id] for paper_id in related_ids if paper_id in self._papers]
        return {
            "papers": [paper.model_dump(by_alias=True) for paper in papers],
            "edges": [edge.model_dump(by_alias=True) for edge in edges],
            "summary": f"returned {len(papers)} related papers",
        }

    def get_mock_graph(self, seed_paper_id: str | None = None) -> dict[str, Any]:
        if seed_paper_id and seed_paper_id in self._papers:
            related = self.get_related_papers(seed_paper_id)
            paper_ids = {seed_paper_id, *(paper["id"] for paper in related["papers"])}
            papers = [paper for paper in MOCK_PAPERS if paper.id in paper_ids]
            edges = [
                edge
                for edge in MOCK_EDGES
                if edge.source in paper_ids and edge.target in paper_ids
            ]
        else:
            papers = MOCK_PAPERS[:5]
            paper_ids = {paper.id for paper in papers}
            edges = [
                edge
                for edge in MOCK_EDGES
                if edge.source in paper_ids and edge.target in paper_ids
            ]
            seed_paper_id = papers[0].id if papers else None

        return {
            "papers": [paper.model_dump(by_alias=True) for paper in papers],
            "edges": [edge.model_dump(by_alias=True) for edge in edges],
            "seedPaperId": seed_paper_id,
            "summary": f"returned graph with {len(papers)} papers and {len(edges)} edges",
        }


def _shares_topic(left: str, right: str) -> bool:
    left_terms = set(left.lower().split())
    right_terms = set(right.lower().split())
    return bool(left_terms & right_terms)
