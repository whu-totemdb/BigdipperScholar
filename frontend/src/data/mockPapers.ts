// Compatibility exports for the optional offline UI. The public application
// ships no fabricated literature, institution affiliations or similarity edges.
import type { GraphEdge, GraphNode, GraphResponse, Paper, PaperEdge } from "../types";

export const papers: Paper[] = [];
export const paperEdges: PaperEdge[] = [];
export const nodes: GraphNode[] = [];
export const edges: GraphEdge[] = [];
export const mockGraph: GraphResponse = {
  title: "No data loaded", queryContext: "No data loaded", defaultViewMode: "graph",
  nodes, papers, edges, warnings: ["Connect a database to retrieve literature."],
};
