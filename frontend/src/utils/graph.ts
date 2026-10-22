import type { ExploreMode, GlobalSemanticDataset, GraphEdge, GraphNode, Paper, PaperEdge } from "../types";

type PaperIdentity = Pick<Paper, "id" | "paperId" | "data">;

export function normalizePaperId(value: unknown) {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  if (!text) return undefined;
  const normalized = text.replace(/^(?:paper_)+/i, "").trim();
  return normalized || undefined;
}

/**
 * Return the backend paper identity without the display-only `paper_` prefix.
 * Expansion payloads have used both camelCase and snake_case keys over time,
 * so keep this small mapping in one place for node/record deduplication.
 */
export function paperCanonicalId(paper: PaperIdentity) {
  return normalizePaperId(paper.data?.paperId)
    ?? normalizePaperId(paper.data?.paper_id)
    ?? normalizePaperId(paper.data?.paperid)
    ?? normalizePaperId(paper.paperId)
    ?? normalizePaperId(paper.id)
    ?? paper.id;
}

function cleanPaperLabel(value?: string) {
  const text = value?.trim();
  if (!text) return undefined;
  if (/^unknown(?: author| authors)?(?:,\s*\d{4})?$/i.test(text)) return undefined;
  return text;
}

export function paperLabel(paper: Paper) {
  const firstAuthor = cleanPaperLabel(paper.authors[0]?.split(" ").at(-1));
  if (firstAuthor && paper.year) return `${firstAuthor}, ${paper.year}`;
  if (firstAuthor) return firstAuthor;
  return paper.title || (paper.year ? String(paper.year) : "Untitled paper");
}

export function nodeTypeLabel(nodeType: GraphNode["nodeType"]) {
  if (nodeType === "paper") return "Paper";
  if (nodeType === "author") return "Author";
  if (nodeType === "institution") return "Institution";
  if (nodeType === "field") return "Field";
  if (nodeType === "semantic_entity") return "Semantic Entity";
  if (nodeType === "global_semantic_entity") return "Global Semantic Entity";
  return "Node";
}

export function nodeTitle(node: GraphNode) {
  if (node.nodeType === "paper") return node.title;
  if (node.nodeType === "global_semantic_entity") return node.canonicalName;
  if (node.nodeType === "semantic_entity") return node.entityName || node.label;
  return node.label;
}

export function nodeSubtitle(node: GraphNode) {
  if (node.nodeType === "paper") {
    return [
      node.year ? String(node.year) : undefined,
      node.venue ? shortVenue(node.venue) : undefined,
      paperCanonicalId(node),
    ].filter(Boolean).join(" · ") || "Paper metadata not listed";
  }
  if (node.nodeType === "author") return node.institution || "Institution not listed";
  if (node.nodeType === "institution") return node.country || "Country not listed";
  if (node.nodeType === "field") return node.level ? `Level ${node.level}` : "Research field";
  if (node.nodeType === "global_semantic_entity") return `${node.entityType ?? "Unknown type"} · ${node.paperCount.toLocaleString()} papers`;
  if (node.nodeType === "semantic_entity") return node.entityType || node.comment || node.summary;
  return node.originalNodeType ? `Unsupported type: ${node.originalNodeType}` : "Unsupported node";
}

export function nodeMetricLabel(node: GraphNode) {
  if (node.nodeType === "paper") return `${node.citationCount.toLocaleString()} cites`;
  if (node.nodeType === "author") return `${node.publicationCount} papers`;
  if (node.nodeType === "institution") return `${node.paperCount} papers`;
  if (node.nodeType === "field") return `${(node.worksCount ?? node.metric).toLocaleString()} works`;
  if (node.nodeType === "global_semantic_entity") return `${node.paperCount.toLocaleString()} papers`;
  if (node.nodeType === "semantic_entity") return node.entityType || `${node.metric.toLocaleString()} links`;
  return `${node.metric.toLocaleString()} score`;
}

export function shortVenue(venue: string) {
  if (venue.length <= 42) {
    return venue;
  }

  return `${venue.slice(0, 39)}...`;
}

export function globalSemanticDatasetForPaperId(paperId?: string | null): GlobalSemanticDataset | null {
  const normalized = normalizePaperId(paperId)?.toUpperCase();
  if (normalized?.startsWith("DBLP-KG-")) return "dblp";
  if (normalized?.startsWith("ASTRO-KG-")) return "astro";
  return null;
}

export function getExplorationIds(
  paperId: string,
  mode: ExploreMode,
  edges: GraphEdge[],
) {
  const ids = new Set<string>([paperId]);
  const paperEdges = edges.filter((edge): edge is PaperEdge =>
    edge.type === "reference" || edge.type === "cites" || edge.type === "cited_by" || edge.type === "similar",
  );

  paperEdges.forEach((edge) => {
    if (mode === "similar" && edge.type === "similar") {
      if (edge.source === paperId) ids.add(edge.target);
      if (edge.target === paperId) ids.add(edge.source);
    }

    if (mode === "refs" && (edge.type === "reference" || edge.type === "cites") && edge.source === paperId) {
      ids.add(edge.target);
    }

    if (mode === "cited_by") {
      if (edge.type === "cited_by" && edge.source === paperId) ids.add(edge.target);
      if ((edge.type === "reference" || edge.type === "cites") && edge.target === paperId) ids.add(edge.source);
    }
  });

  return ids;
}

export function modeLabel(mode: ExploreMode) {
  if (mode === "refs") return "References";
  if (mode === "cited_by") return "Cited by";
  if (mode === "connections") return "Connections";
  if (mode === "semantic_graph") return "Semantic Graph";
  return "Similar";
}
