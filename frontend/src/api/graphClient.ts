import { API_BASE_URL } from "./chatClient";
import { normalizePaperId, paperCanonicalId } from "../utils/graph";
import type {
  GraphEdge,
  GlobalSemanticEntityNode,
  GlobalSemanticGraphExpandRequest,
  GlobalSemanticGraphExpandResponse,
  GlobalSemanticGraphExpandStats,
  GlobalSemanticGraphQueryRequest,
  GlobalSemanticGraphResponse,
  GlobalSemanticGraphStats,
  GraphNode,
  GraphQueryRequest,
  GraphResponse,
  GraphStateSummary,
  FieldNode,
  Paper,
  PaperNode,
  SemanticEntityNode,
} from "../types";

type JsonRecord = Record<string, unknown>;

function authHeaders(token?: string) {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 310000);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      signal: options.signal ?? controller.signal,
    });

    if (!response.ok) {
      const errorPayload = await response.json().catch(() => null) as unknown;
      const detail = isRecord(errorPayload) ? errorPayload.detail : undefined;
      const nestedError = isRecord(detail) && isRecord(detail.error) ? detail.error : undefined;
      const message = isRecord(nestedError) && typeof nestedError.message === "string"
        ? nestedError.message
        : isRecord(errorPayload) && typeof errorPayload.message === "string"
          ? errorPayload.message
          : typeof detail === "string"
            ? detail
            : `${path} failed with ${response.status}`;
      throw new Error(message);
    }

    return response.json() as Promise<T>;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(source: JsonRecord, key: string, fallback = "") {
  const value = source[key];
  return typeof value === "string" ? value : fallback;
}

function firstStringValue(source: JsonRecord, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return fallback;
}

function numberValue(source: JsonRecord, key: string, fallback = 0) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function isMeaningfulValue(value: unknown) {
  return value !== null && value !== undefined && (typeof value !== "string" || Boolean(value.trim()));
}

function mergeDefinedFields(base: JsonRecord, override: JsonRecord) {
  return Object.entries(override).reduce<JsonRecord>(
    (merged, [key, value]) => {
      if (isMeaningfulValue(value)) {
        merged[key] = value;
      }
      return merged;
    },
    { ...base },
  );
}

function optionalStringValue(source: JsonRecord, key: string) {
  const value = source[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

/** Last-line protection for every API that accepts a backend paper identity. */
export function paperIdForApi(value: string) {
  const paperId = normalizePaperId(value);
  if (!paperId) throw new Error("Paper ID is required");
  return paperId;
}

function optionalNumberValue(source: JsonRecord, key: string) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stringArrayValue(source: JsonRecord, key: string) {
  const value = source[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function examplePaperDetailsValue(source: JsonRecord) {
  const value = source.examplePaperDetails;
  if (!Array.isArray(value)) return [];

  return value.filter(isRecord).flatMap((detail) => {
    const paperId = optionalStringValue(detail, "paperId");
    if (!paperId) return [];

    return [{
      paperId,
      title: optionalStringValue(detail, "title"),
      year: optionalNumberValue(detail, "year"),
    }];
  });
}

function stringArrayLengthValue(source: JsonRecord, key: string) {
  const value = source[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").length : undefined;
}

function strippedInstitutionId(value?: string) {
  return value?.replace(/^institution-/i, "");
}

function firstInstitutionValue(source: JsonRecord) {
  const data = isRecord(source.data) ? source.data : {};
  const value = Array.isArray(source.institutions) ? source.institutions : data.institutions;
  if (!Array.isArray(value)) return null;
  return value.find(isRecord) ?? null;
}

function affiliationValues(source: JsonRecord) {
  const data = isRecord(source.data) ? source.data : {};
  const value = Array.isArray(source.affiliations)
    ? source.affiliations
    : Array.isArray(data.affiliations)
      ? data.affiliations
      : Array.isArray(source.institutions)
        ? source.institutions
        : data.institutions;
  if (!Array.isArray(value)) return undefined;

  const affiliations = value.filter(isRecord).map((affiliation) => ({
    institutionId: strippedInstitutionId(
      optionalStringValue(affiliation, "institutionId")
        ?? optionalStringValue(affiliation, "institutionid")
        ?? optionalStringValue(affiliation, "id"),
    ),
    affiliationName: optionalStringValue(affiliation, "affiliationName") ?? optionalStringValue(affiliation, "label"),
    rawAffiliationString: optionalStringValue(affiliation, "rawAffiliationString") ?? optionalStringValue(affiliation, "rawAffiliation"),
    authorPosition: optionalStringValue(affiliation, "authorPosition"),
  }));

  return affiliations.length ? affiliations : undefined;
}

function positionValue(source: JsonRecord, index: number) {
  const value = source.position;
  if (isRecord(value) && typeof value.x === "number" && typeof value.y === "number") {
    return { x: value.x, y: value.y };
  }

  const radius = 320;
  return {
    x: 560 + Math.cos((index / 12) * Math.PI * 2) * radius,
    y: 420 + Math.sin((index / 12) * Math.PI * 2) * radius,
  };
}

function paperTitleValue(source: JsonRecord, data: JsonRecord, nodeId: string, paperId: string) {
  const identities = new Set(
    [nodeId, paperId]
      .map((value) => normalizePaperId(value)?.toLowerCase())
      .filter((value): value is string => Boolean(value)),
  );
  const candidates = [
    source.title,
    data.title,
    source.displayName,
    data.displayName,
    source.display_name,
    data.display_name,
    source.name,
    data.name,
    source.label,
    data.label,
    source.summary,
    data.summary,
  ];

  for (const candidate of candidates) {
    if (typeof candidate !== "string") continue;
    const text = candidate.trim();
    if (!text || text.toLowerCase() === "untitled paper") continue;
    const identity = normalizePaperId(text)?.toLowerCase();
    if (!identity || !identities.has(identity)) return text;
  }

  return "Untitled paper";
}

function normalizePaper(raw: unknown, index = 0): Paper {
  const source = isRecord(raw) ? raw : {};
  const data = isRecord(source.data) ? source.data : {};
  const merged = mergeDefinedFields(data, source);
  const rawPaperId = firstStringValue(
    data,
    ["paperId", "paper_id", "paperid"],
    firstStringValue(source, ["paperId", "paper_id", "paperid"]),
  );
  const paperId = normalizePaperId(rawPaperId)
    ?? normalizePaperId(firstStringValue(source, ["id"], firstStringValue(data, ["id"])))
    ?? String(index);
  const id = firstStringValue(
    source,
    ["id"],
    firstStringValue(data, ["id"], `paper_${paperId}`),
  );
  const title = paperTitleValue(source, data, id, paperId);
  const normalizedData = { ...data, paperId };

  return {
    id,
    paperId,
    title,
    year: numberValue(merged, "year", 0),
    authors: stringArrayValue(merged, "authors"),
    venue: stringValue(merged, "venue", stringValue(merged, "source")),
    abstract: stringValue(merged, "abstract", stringValue(merged, "summary", title)),
    doi: stringValue(merged, "doi"),
    citationCount: numberValue(merged, "citationCount", numberValue(merged, "metric", 0)),
    topic: stringValue(merged, "topic"),
    position: positionValue(merged, index),
    data: normalizedData,
  };
}

function cleanPaperLabel(value?: string) {
  const text = value?.trim();
  if (!text) return undefined;
  if (/^unknown(?: author| authors)?(?:,\s*\d{4})?$/i.test(text)) return undefined;
  return text;
}

function paperLabel(paper: Paper) {
  const firstAuthor = cleanPaperLabel(paper.authors[0]?.split(" ").at(-1));
  if (firstAuthor && paper.year) return `${firstAuthor}, ${paper.year}`;
  if (firstAuthor) return firstAuthor;
  return paper.title || (paper.year ? String(paper.year) : "Untitled paper");
}

function normalizeNode(raw: unknown, index: number): GraphNode | null {
  const source = isRecord(raw) ? raw : {};
  const nodeType = stringValue(source, "nodeType", stringValue(source, "node_type"));

  if (nodeType === "paper" || nodeType === "article" || !nodeType) {
    const paper = normalizePaper(source, index);
    return {
      ...paper,
      nodeType: "paper",
      // Paper/article nodes use the title as their primary label. Keep the
      // author/year fallback only for genuinely title-less legacy records.
      label: paper.title || cleanPaperLabel(optionalStringValue(source, "label")) || paperLabel(paper),
      summary: stringValue(source, "summary", paper.abstract || paper.title),
      metric: numberValue(source, "metric", paper.citationCount),
    } satisfies PaperNode;
  }

  if (nodeType === "author") {
    const data = isRecord(source.data) ? source.data : {};
    const merged = mergeDefinedFields(data, source);
    const nestedInstitution = firstInstitutionValue(merged);
    const fallbackInstitutionId = nestedInstitution ? stringValue(nestedInstitution, "id", "institution-unknown") : "institution-unknown";
    const fallbackInstitution = nestedInstitution ? stringValue(nestedInstitution, "label", "Unknown institution") : "Unknown institution";
    const paperIds = stringArrayValue(merged, "paperIds");
    const displayName = optionalStringValue(merged, "displayName") ?? optionalStringValue(merged, "label");
    const authorName = optionalStringValue(merged, "authorName") ?? optionalStringValue(merged, "label");
    const worksCount = optionalNumberValue(merged, "worksCount") ?? optionalNumberValue(merged, "publicationCount");
    const currentGraphPaperCount = optionalNumberValue(merged, "currentGraphPaperCount") ?? stringArrayLengthValue(merged, "paperIds");

    return {
      id: stringValue(merged, "id", `author_${index}`),
      nodeType: "author",
      label: stringValue(merged, "label", stringValue(merged, "displayName", stringValue(merged, "authorName", "Unknown author"))),
      summary: stringValue(merged, "summary", "Author"),
      metric: numberValue(merged, "metric", numberValue(merged, "publicationCount", 0)),
      position: positionValue(merged, index),
      institutionId: stringValue(merged, "institutionId", fallbackInstitutionId),
      institution: stringValue(merged, "institution", fallbackInstitution),
      paperIds,
      publicationCount: numberValue(merged, "publicationCount", paperIds.length),
      authorId: optionalStringValue(merged, "authorId") ?? optionalStringValue(merged, "authorid"),
      displayName,
      authorName,
      orcid: optionalStringValue(merged, "orcid"),
      worksCount,
      citedByCount: optionalNumberValue(merged, "citedByCount"),
      hIndex: optionalNumberValue(merged, "hIndex"),
      currentGraphPaperCount,
      affiliations: affiliationValues(merged),
      data,
    };
  }

  if (nodeType === "institution") {
    const data = isRecord(source.data) ? source.data : {};
    const merged = mergeDefinedFields(data, source);
    const authorIds = stringArrayValue(merged, "authorIds");
    const affiliationName = optionalStringValue(merged, "affiliationName") ?? optionalStringValue(merged, "label");
    const countryCode = optionalStringValue(merged, "countryCode") ?? optionalStringValue(merged, "country");
    const type = optionalStringValue(merged, "type") ?? optionalStringValue(merged, "institutionType");
    const currentGraphAuthorCount = optionalNumberValue(merged, "currentGraphAuthorCount") ?? stringArrayLengthValue(merged, "authorIds");
    const currentGraphPaperCount = optionalNumberValue(merged, "currentGraphPaperCount")
      ?? stringArrayLengthValue(merged, "paperIds")
      ?? optionalNumberValue(merged, "paperCount");

    return {
      id: stringValue(merged, "id", `institution_${index}`),
      nodeType: "institution",
      label: stringValue(merged, "label", stringValue(merged, "affiliationName", "Unknown institution")),
      summary: stringValue(merged, "summary", "Institution"),
      metric: numberValue(merged, "metric", numberValue(merged, "paperCount", 0)),
      position: positionValue(merged, index),
      country: stringValue(merged, "country", stringValue(merged, "countryCode", "Unknown")),
      authorIds,
      paperCount: numberValue(merged, "paperCount", 0),
      institutionId: optionalStringValue(merged, "institutionId") ?? optionalStringValue(merged, "institutionid"),
      affiliationName,
      countryCode,
      type,
      homepageUrl: optionalStringValue(merged, "homepageUrl"),
      ror: optionalStringValue(merged, "ror"),
      hIndex: optionalNumberValue(merged, "hIndex"),
      productivity: optionalNumberValue(merged, "productivity"),
      currentGraphAuthorCount,
      currentGraphPaperCount,
      data,
    };
  }

  if (nodeType === "field") {
    const data = isRecord(source.data) ? source.data : {};
    const merged = mergeDefinedFields(data, source);
    return {
      id: stringValue(merged, "id", stringValue(merged, "fieldid", `field_${index}`)),
      nodeType: "field",
      label: stringValue(merged, "label", stringValue(merged, "displayName", "Research field")),
      summary: stringValue(merged, "summary", "Research field"),
      metric: numberValue(merged, "metric", numberValue(merged, "worksCount", numberValue(merged, "works_count", 0))),
      position: positionValue(merged, index),
      fieldId: stringValue(merged, "fieldId", stringValue(merged, "fieldid")) || undefined,
      level: numberValue(merged, "level", 0),
      worksCount: numberValue(merged, "worksCount", numberValue(merged, "works_count", 0)),
      citedByCount: numberValue(merged, "citedByCount", numberValue(merged, "cited_by_count", 0)),
      data,
    } satisfies FieldNode;
  }

  if (nodeType === "semantic_entity") {
    const data = isRecord(source.data) ? source.data : {};
    const rawProperties = isRecord(data.properties)
      ? data.properties
      : isRecord(source.properties)
        ? source.properties
        : undefined;
    const merged = mergeDefinedFields(rawProperties ? mergeDefinedFields(data, rawProperties) : data, source);
    const entityName = firstStringValue(merged, ["entityName", "entity_name", "label", "name"], stringValue(merged, "id", `semantic_entity_${index}`));
    const entityType = firstStringValue(merged, ["entityType", "entity_type", "type"]);
    const comment = firstStringValue(merged, ["comment", "summary", "description"]);

    return {
      id: firstStringValue(merged, ["id", "entityId", "entity_id"], `semantic_entity_${index}`),
      nodeType: "semantic_entity",
      label: entityName,
      summary: comment || entityType,
      metric: numberValue(merged, "metric", 0),
      position: positionValue(merged, index),
      entityId: firstStringValue(merged, ["entityId", "entity_id"]) || undefined,
      entityName,
      entityType: entityType || undefined,
      comment: comment || undefined,
      properties: rawProperties,
      data: rawProperties ? { ...data, properties: rawProperties } : data,
    } satisfies SemanticEntityNode;
  }

  if (nodeType === "global_semantic_entity") {
    const data = isRecord(source.data) ? source.data : {};
    const merged = mergeDefinedFields(data, source);
    const canonicalName = firstStringValue(merged, ["canonicalName", "canonical_name", "label", "name"], `Global entity ${index + 1}`);
    const entityType = firstStringValue(merged, ["entityType", "entity_type", "type"]);
    const globalEntityId = firstStringValue(merged, ["globalEntityId", "global_entity_id", "id"], `global_semantic_entity_${index}`);
    const paperCount = numberValue(merged, "paperCount", numberValue(merged, "paper_count", numberValue(merged, "metric", 0)));
    const mentionCount = numberValue(merged, "mentionCount", numberValue(merged, "mention_count", 0));
    const typeDistributionSource = isRecord(merged.typeDistribution) ? merged.typeDistribution : {};
    const typeDistribution = Object.fromEntries(
      Object.entries(typeDistributionSource).filter((entry): entry is [string, number] => typeof entry[1] === "number"),
    ) as Record<string, number>;

    return {
      id: globalEntityId,
      nodeType: "global_semantic_entity",
      label: canonicalName,
      summary: entityType ? `${entityType} · ${paperCount.toLocaleString()} papers` : `${paperCount.toLocaleString()} papers`,
      metric: paperCount,
      position: positionValue(merged, index),
      globalEntityId,
      canonicalName,
      entityType: entityType || undefined,
      paperCount,
      mentionCount,
      datasets: stringArrayValue(merged, "datasets"),
      datasetScope: merged.datasetScope === "dblp" || merged.datasetScope === "astro" ? merged.datasetScope : undefined,
      examplePapers: stringArrayValue(merged, "examplePapers"),
      examplePaperDetails: examplePaperDetailsValue(merged),
      typeDistribution: Object.keys(typeDistribution).length ? typeDistribution : undefined,
      data,
    } satisfies GlobalSemanticEntityNode;
  }

  return {
    id: stringValue(source, "id", `${nodeType || "node"}_${index}`),
    nodeType: "unknown",
    originalNodeType: nodeType || undefined,
    label: stringValue(source, "label", stringValue(source, "displayName", nodeType ? `${nodeType} node` : "Unsupported node")),
    summary: stringValue(source, "summary", nodeType ? `Unsupported ${nodeType} node` : "Unsupported node"),
    metric: numberValue(source, "metric", 0),
    position: positionValue(source, index),
    data: isRecord(source.data) ? source.data : source,
  };
}

function normalizeEdgeType(edgeType: unknown): GraphEdge["type"] {
  if (edgeType === "cites" || edgeType === "references") return "reference";
  if (edgeType === "cited_by") return "cited_by";
  if (edgeType === "authored_by") return "authored_by";
  if (edgeType === "affiliated_with") return "affiliated_with";
  if (edgeType === "coauthored_with") return "coauthored_with";
  if (edgeType === "institution_collaboration") return "institution_collaboration";
  if (edgeType === "has_field") return "has_field";
  if (edgeType === "published_in") return "published_in";
  if (edgeType === "local_to_global") return "local_to_global";
  if (edgeType === "entity_mentioned_in_paper") return "entity_mentioned_in_paper";
  if (edgeType === "co_occurrence" || edgeType === "co-occurrence") return "co_occurrence";
  if (edgeType === "semantic_relation" || edgeType === "entity_relation" || edgeType === "related_to") return "semantic_relation";
  return "similar";
}

function normalizeEdge(raw: unknown, index: number): GraphEdge | null {
  if (!isRecord(raw)) return null;
  const source = stringValue(raw, "source");
  const target = stringValue(raw, "target");
  if (!source || !target) return null;
  const data = isRecord(raw.data) ? raw.data : {};
  const properties = isRecord(raw.properties) ? raw.properties : {};
  const metadata = mergeDefinedFields(properties, data);
  const directWeight = optionalNumberValue(raw, "weight");
  const directPaperCount = optionalNumberValue(raw, "paperCount");
  const directMentionCount = optionalNumberValue(raw, "mentionCount");
  const normalizedData = {
    ...data,
    ...(directPaperCount !== undefined && data.paperCount === undefined ? { paperCount: directPaperCount } : {}),
    ...(directWeight !== undefined && data.weight === undefined ? { weight: directWeight } : {}),
    ...(directMentionCount !== undefined && data.mentionCount === undefined ? { mentionCount: directMentionCount } : {}),
  };
  const relationName = firstStringValue(
    raw,
    ["relation_name", "relationName"],
    firstStringValue(metadata, ["relation_name", "relationName"]),
  );
  const label = firstStringValue(
    raw,
    ["label"],
    firstStringValue(metadata, ["label"], relationName || String(raw.type ?? raw.edgeType ?? "")),
  );

  return {
    id: stringValue(raw, "id", `edge_${index}_${source}_${target}`),
    source,
    target,
    type: normalizeEdgeType(raw.type ?? raw.edgeType),
    label,
    relationName: relationName || undefined,
    weight: optionalNumberValue(metadata, "weight") ?? directWeight ?? optionalNumberValue(metadata, "paperCount") ?? directPaperCount,
    paperCount: optionalNumberValue(metadata, "paperCount") ?? directPaperCount ?? optionalNumberValue(metadata, "weight") ?? directWeight,
    data: Object.keys(normalizedData).length ? normalizedData : undefined,
  };
}

function enrichPaperNode(node: PaperNode, record: Paper): PaperNode {
  const paperId = paperCanonicalId(node);
  const title = record.title !== "Untitled paper" ? record.title : node.title;
  const abstract = record.abstract && record.abstract !== record.title
    ? record.abstract
    : node.abstract;

  return {
    ...node,
    paperId,
    title,
    year: record.year || node.year,
    authors: record.authors.length ? record.authors : node.authors,
    venue: record.venue || node.venue,
    abstract,
    doi: record.doi || node.doi,
    citationCount: record.citationCount || node.citationCount,
    topic: record.topic || node.topic,
    label: title,
    summary: abstract || title,
    metric: record.citationCount || node.metric,
    position: node.position,
    data: {
      ...(record.data ?? {}),
      ...(node.data ?? {}),
      paperId,
    },
  };
}

export function normalizeGraphResponse(
  payload: Partial<GraphResponse> | unknown,
  options: { allowDanglingEdgeTypes?: GraphEdge["type"][] } = {},
): GraphResponse {
  const source = isRecord(payload) ? payload : {};
  const rawNodes = Array.isArray(source.nodes) ? source.nodes : [];
  const normalizedNodes = rawNodes
    .map((node, index) => normalizeNode(node, index))
    .filter((node): node is GraphNode => Boolean(node));
  const paperNodes = normalizedNodes.filter((node): node is PaperNode => node.nodeType === "paper");
  const papers = (Array.isArray(source.papers) && source.papers.length
    ? source.papers.map((paper, index) => normalizePaper(paper, index))
    : paperNodes.map((node) => ({ ...node })));
  const paperRecordsById = new Map(
    papers.map((paper) => [paperCanonicalId(paper).toLowerCase(), paper]),
  );
  const nodes = normalizedNodes.map((node) => {
    if (node.nodeType !== "paper") return node;
    const record = paperRecordsById.get(paperCanonicalId(node).toLowerCase());
    return record ? enrichPaperNode(node, record) : node;
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const nodeAliases = new Map<string, string>();
  nodes.forEach((node) => {
    nodeAliases.set(node.id, node.id);
    nodeAliases.set(node.id.toLowerCase(), node.id);
    if (node.nodeType === "paper") {
      const canonicalId = paperCanonicalId(node);
      nodeAliases.set(canonicalId.toLowerCase(), node.id);
      nodeAliases.set(`paper_${canonicalId}`.toLowerCase(), node.id);
    }
    if (node.nodeType === "semantic_entity") {
      if (node.entityId) {
        nodeAliases.set(node.entityId, node.id);
        nodeAliases.set(node.entityId.toLowerCase(), node.id);
      }
      const suffix = node.id.split(":semantic_entity:")[1];
      if (suffix) {
        nodeAliases.set(suffix, node.id);
        nodeAliases.set(suffix.toLowerCase(), node.id);
      }
    }
    if (node.nodeType === "global_semantic_entity") {
      nodeAliases.set(node.globalEntityId, node.id);
      nodeAliases.set(node.globalEntityId.toLowerCase(), node.id);
    }
  });
  const resolveNodeId = (id: string) => nodeIds.has(id)
    ? id
    : nodeAliases.get(id) ?? nodeAliases.get(id.toLowerCase()) ?? id;
  const edges = (Array.isArray(source.edges) ? source.edges : [])
    .map((edge, index) => normalizeEdge(edge, index))
    .map((edge) => edge
      ? { ...edge, source: resolveNodeId(edge.source), target: resolveNodeId(edge.target) }
      : null)
    .filter((edge): edge is GraphEdge => {
      if (!edge) return false;
      const allowDangling = options.allowDanglingEdgeTypes?.includes(edge.type) ?? false;
      return (nodeIds.has(edge.source) && nodeIds.has(edge.target))
        || (allowDangling && (nodeIds.has(edge.source) || nodeIds.has(edge.target)));
    });

  return {
    graphId: typeof source.graphId === "string" ? source.graphId : null,
    workspaceId: typeof source.workspaceId === "string" ? source.workspaceId : null,
    title: typeof source.title === "string" ? source.title : undefined,
    queryContext: typeof source.queryContext === "string" ? source.queryContext : undefined,
    seedNodeId: typeof source.seedNodeId === "string" ? source.seedNodeId : undefined,
    seedPaperId: normalizePaperId(source.seedPaperId),
    defaultViewMode: source.defaultViewMode === "list" || source.defaultViewMode === "graph" || source.defaultViewMode === "default"
      ? source.defaultViewMode
      : undefined,
    warnings: Array.isArray(source.warnings)
      ? source.warnings.filter((warning): warning is string => typeof warning === "string")
      : [],
    nodes,
    papers,
    edges,
  };
}

export async function searchPapers(token: string, query: string, limit = 10) {
  return requestJson<{ query: string; items: PaperNode[]; warnings?: string[] }>("/api/papers/search", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ query, limit }),
  });
}

export async function getPaper(token: string, paperId: string) {
  const requestPaperId = paperIdForApi(paperId);
  const payload = await requestJson<{ paper: PaperNode }>(`/api/papers/${encodeURIComponent(requestPaperId)}`, {
    headers: authHeaders(token),
  });
  return normalizeNode(payload.paper, 0) as PaperNode;
}

export async function queryGraph(token: string, request: GraphQueryRequest) {
  const apiRequest = request.seedPaperId
    ? { ...request, seedPaperId: paperIdForApi(request.seedPaperId) }
    : request;
  const payload = await requestJson<GraphResponse>("/api/graphs/query", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(apiRequest),
  });
  return normalizeGraphResponse(payload);
}

export async function queryGlobalSemanticGraph(
  token: string,
  request: GlobalSemanticGraphQueryRequest,
) {
  const apiRequest = { ...request, seedPaperId: paperIdForApi(request.seedPaperId) };
  const payload = await requestJson<unknown>("/api/semantic-graphs/global/query", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(apiRequest),
  });
  const source = isRecord(payload) ? payload : {};
  const normalizedGraph = normalizeGraphResponse(source);
  const rawStats = isRecord(source.stats) ? source.stats : {};
  const dataset = rawStats.dataset === "dblp" || rawStats.dataset === "astro"
    ? rawStats.dataset
    : request.dataset;
  const nodes = normalizedGraph.nodes
    .filter((node): node is GlobalSemanticEntityNode => node.nodeType === "global_semantic_entity")
    .map((node) => ({ ...node, datasetScope: node.datasetScope ?? dataset }));
  const stats: GlobalSemanticGraphStats = {
    dataset,
    minPaperCount: numberValue(rawStats, "minPaperCount", request.minPaperCount),
    edgeMinWeight: numberValue(rawStats, "edgeMinWeight", request.edgeMinWeight),
    depth: numberValue(rawStats, "depth", request.depth),
    returnedNodeCount: numberValue(rawStats, "returnedNodeCount", nodes.length),
    returnedEdgeCount: numberValue(rawStats, "returnedEdgeCount", normalizedGraph.edges.length),
    availableNeighborCount: numberValue(rawStats, "availableNeighborCount", Math.max(0, nodes.length - 1)),
    truncated: rawStats.truncated === true,
    buildId: typeof rawStats.buildId === "string" ? rawStats.buildId : null,
    builtAt: typeof rawStats.builtAt === "string" ? rawStats.builtAt : null,
  };

  return {
    ...normalizedGraph,
    nodes,
    stats,
  } satisfies GlobalSemanticGraphResponse;
}

export async function expandGlobalSemanticGraph(
  token: string,
  request: GlobalSemanticGraphExpandRequest,
) {
  const apiRequest = { ...request, seedPaperId: paperIdForApi(request.seedPaperId) };
  const payload = await requestJson<unknown>("/api/semantic-graphs/global/expand", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(apiRequest),
  });
  const source = isRecord(payload) ? payload : {};
  const normalizedGraph = normalizeGraphResponse(source, { allowDanglingEdgeTypes: ["local_to_global"] });
  const rawStats = isRecord(source.stats) ? source.stats : {};
  const dataset = rawStats.dataset === "dblp" || rawStats.dataset === "astro"
    ? rawStats.dataset
    : request.dataset;
  const nodes = normalizedGraph.nodes.map((node) => node.nodeType === "global_semantic_entity"
    ? { ...node, datasetScope: node.datasetScope ?? dataset }
    : node);
  const stats: GlobalSemanticGraphExpandStats = {
    dataset,
    minPaperCount: numberValue(rawStats, "minPaperCount", request.minPaperCount),
    edgeMinWeight: numberValue(rawStats, "edgeMinWeight", request.edgeMinWeight),
    depth: numberValue(rawStats, "depth", request.depth),
    evidenceLimit: numberValue(rawStats, "evidenceLimit", request.evidenceLimit),
    nodeCount: numberValue(rawStats, "nodeCount", nodes.length),
    edgeCount: numberValue(rawStats, "edgeCount", normalizedGraph.edges.length),
    paperNodeCount: numberValue(rawStats, "paperNodeCount", nodes.filter((node) => node.nodeType === "paper").length),
    globalEntityNodeCount: numberValue(
      rawStats,
      "globalEntityNodeCount",
      nodes.filter((node) => node.nodeType === "global_semantic_entity").length,
    ),
    availableNeighborCount: numberValue(rawStats, "availableNeighborCount", 0),
    truncated: rawStats.truncated === true,
    buildId: typeof rawStats.buildId === "string" ? rawStats.buildId : null,
    builtAt: typeof rawStats.builtAt === "string" ? rawStats.builtAt : null,
  };
  const rawSeed = isRecord(source.seed) ? source.seed : {};

  return {
    ...normalizedGraph,
    nodes,
    seed: {
      paperId: paperIdForApi(stringValue(rawSeed, "paperId", apiRequest.seedPaperId)),
      localEntityId: stringValue(rawSeed, "localEntityId", request.seedEntityId),
      globalEntityId: stringValue(rawSeed, "globalEntityId"),
      canonicalName: stringValue(rawSeed, "canonicalName"),
      entityType: stringValue(rawSeed, "entityType"),
    },
    stats,
    warnings: Array.isArray(source.warnings)
      ? source.warnings.filter((warning): warning is string => typeof warning === "string")
      : normalizedGraph.warnings ?? [],
  } satisfies GlobalSemanticGraphExpandResponse;
}

export async function listGraphStates(token: string, workspaceId: string) {
  return requestJson<{ items: GraphStateSummary[] }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/graph-states`, {
    headers: authHeaders(token),
  });
}

export async function getGraphState(token: string, workspaceId: string, graphId: string) {
  const payload = await requestJson<GraphResponse>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/graph-states/${encodeURIComponent(graphId)}`,
    { headers: authHeaders(token) },
  );
  return normalizeGraphResponse(payload);
}

export async function saveGraphState(token: string, workspaceId: string, graph: GraphResponse) {
  return requestJson<{ graphId: string }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/graph-states`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      title: graph.title ?? graph.queryContext ?? "Graph state",
      queryContext: graph.queryContext ?? graph.title ?? "Graph state",
      seedNodeId: graph.seedNodeId ?? graph.seedPaperId ?? null,
      graph: {
        nodes: graph.nodes,
        papers: graph.papers,
        edges: graph.edges,
      },
    }),
  });
}
