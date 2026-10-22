"""Render selected observations without making new database queries."""
from dataclasses import dataclass, field
from typing import Any
from app.evidence_provenance import ground_bundle
from app.models import GraphNode, GraphResponse, PaperSearchItem


@dataclass
class EvidenceGraphBuildResult:
    graph: GraphResponse | None
    missing_report_entities: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EvidenceGraphBuilder:
    def __init__(self, graph_service=None):
        # Accepted for compatibility only; never used during evidence generation.
        pass

    def build(self, *, run_id, workspace_id, evidence_bundle, selected_observations,
              report="", max_nodes=80, include_authors=True,
              include_institutions=True, include_citation_edges=True):
        intent = ground_bundle(selected_observations, evidence_bundle)["graphIntent"]
        candidates = [node for node in intent["nodes"]
                      if (include_authors or node["nodeType"] != "author")
                      and (include_institutions or node["nodeType"] != "institution")]
        candidates.sort(key=lambda node: (0 if node["id"] in report else 1,
                                         {"paper": 0, "field": 1, "author": 2, "institution": 3}[node["nodeType"]]))
        cap = min(200, max(1, int(max_nodes)))
        warnings = [f"Graph limited to {cap} nodes; full evidence remains in the run artifact."] if len(candidates) > cap else []
        nodes = []
        for index, node in enumerate(candidates[:cap]):
            values = {k: v for k, v in node.items() if k in {"id", "nodeType", "label", "title", "year", "abstract", "doi", "venue", "citationCount"}}
            nodes.append(GraphNode(**values, position={"x": index % 6 * 220, "y": index // 6 * 160}, data=node))
        node_ids = {node.id for node in nodes}
        edges = [{**edge, "data": edge} for edge in intent["edges"]
                 if edge["source"] in node_ids and edge["target"] in node_ids
                 and (include_citation_edges or edge["type"] not in {"cites", "reference", "cited_by"})]
        if not nodes:
            return EvidenceGraphBuildResult(None, warnings=["No graphable facts in selected observations."])
        papers = [PaperSearchItem(id=n.id, label=n.label, title=n.title or n.label,
                                 year=n.year or 0, authors=n.authors, venue=n.venue or "",
                                 abstract=n.abstract or "", citationCount=n.citation_count or 0,
                                 topic=n.topic or "", summary=n.summary or "", position=n.position)
                  for n in nodes if n.node_type == "paper"]
        graph = GraphResponse(graphId=f"graph_evidence_{run_id}", workspaceId=workspace_id,
                              title=intent["title"], queryContext="Agent report evidence graph",
                              nodes=nodes, edges=edges, papers=papers, warnings=warnings, defaultViewMode="graph")
        return EvidenceGraphBuildResult(graph, warnings=warnings)


def _assemble_deterministic_graph_intent(evidence_bundle, selected_observations, **kwargs):
    return ground_bundle(selected_observations, evidence_bundle)["graphIntent"]
