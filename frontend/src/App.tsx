import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Home, Loader2 } from "lucide-react";
import { AgentChatPanel } from "./components/AgentChatPanel";
import { GraphCanvas } from "./components/GraphCanvas";
import { HomePage } from "./components/HomePage";
import { LoginPage } from "./components/LoginPage";
import { PaperDetailPanel } from "./components/PaperDetailPanel";
import { PaperList } from "./components/PaperList";
import { SearchContextBar } from "./components/SearchContextBar";
import { TopNav } from "./components/TopNav";
import { mockGraph } from "./data/mockPapers";
import { getCurrentUser, getWorkspace, loadStoredSession } from "./api/authHomeClient";
import { expandGlobalSemanticGraph, getGraphState, listGraphStates, queryGraph, saveGraphState } from "./api/graphClient";
import type {
  AuthSession,
  ExploreMode,
  GlobalSemanticGraphExpandResponse,
  GlobalSemanticGraphStats,
  GraphEdge,
  GraphNode,
  GraphResponse,
  GraphStateSummary,
  NodeTypeFilter,
  Paper,
  PaperNode,
  ViewMode,
  SemanticEntityNode,
  WorkspaceDetail,
} from "./types";
import { getExplorationIds, globalSemanticDatasetForPaperId, modeLabel, nodeTypeLabel, paperCanonicalId, paperLabel } from "./utils/graph";

const ROOT_GRAPH_TITLE = "Root graph";
const GLOBAL_EXPANSION_ENTITY_LIMIT = 12;
const GLOBAL_EXPANSION_RELATION_LIMIT = 20;
const GLOBAL_EXPANSION_EVIDENCE_LIMIT = 5;

const DEMO_AGENT_MESSAGE = `你好！我是 BigDipper Scholar，一个专注于学术文献和机构研究的分析助手。我可以帮你做以下事情：

---

📚 文献与论文
查论文ID：通过论文标题查找论文ID和摘要
查参考文献：查看某篇论文引用了哪些文献
查作者与机构：获取论文的作者、所属机构信息
推荐合作者：基于论文领域推荐潜在合作者
🏛️ 机构分析
机构排名：查询某机构在特定学科领域的作者排名
机构统计：按领域统计中国机构的发文量和被引情况
机构报告：生成机构的详细分析报告
机构重叠：查看目标论文同领域候选论文中的机构分布
👨‍🔬 作者分析
查作者ID：通过作者姓名查询ID和主要机构
作者报告：生成作者的详细分析报告
领域高被引作者：查询某领域的高被引作者
🔬 学科领域
查询所有学科领域
热门子领域：近三年热门研究方向
跨学科分析：领域间的交叉融合情况
领域合作网络：机构间的论文合作网络
---

举个例子，你可以问我：

"查一下论文《Attention is All You Need》的信息"
"清华大学在人工智能领域的排名如何？"
"推荐一下计算机视觉领域的潜在合作者"
"近三年最热门的子领域有哪些？"
有什么我可以帮你的吗？😊`;

type RouteState =
  | { name: "login" }
  | { name: "home" }
  | { name: "workspace"; workspaceId: string };

type GlobalSemanticGraphView = {
  seedEntityName: string;
  stats: GlobalSemanticGraphStats;
  addedNodeCount: number;
  addedEdgeCount: number;
  topConnectionsOnly: boolean;
  displayedNodeCount: number;
  displayedEdgeCount: number;
  evidenceLimit: number;
};

type GraphViewState = {
  graphData: GraphResponse;
  queryContext: string;
  filteredIds: Set<string> | null;
  nodeTypeFilter: NodeTypeFilter;
  selectedNodeId: string | null;
  viewMode: ViewMode;
  listCollapsed: boolean;
  graphWarning: string | null;
  globalSemanticGraph: GlobalSemanticGraphView | null;
  globalSemanticBaseState: GraphViewState | null;
};

function parseRoute(): RouteState {
  const path = window.location.pathname;
  const workspaceMatch = path.match(/^\/workspace\/([^/]+)$/);
  if (workspaceMatch?.[1]) return { name: "workspace", workspaceId: decodeURIComponent(workspaceMatch[1]) };
  if (path === "/home") return { name: "home" };
  return { name: "login" };
}

function pushRoute(path: string, setRoute: (route: RouteState) => void) {
  window.history.pushState(null, "", path);
  setRoute(parseRoute());
}

function layoutGraphNodes(
  nodes: GraphNode[],
  edges: GraphResponse["edges"] = [],
  seedPaperId?: string | null,
  title?: string,
  queryContext?: string,
  seedNodeId?: string | null,
) {
  const isConnectionsGraph = queryContext === "Paper connections" || title?.startsWith("Connections:");
  if (isConnectionsGraph) {
    return layoutConnectionsGraphNodes(nodes, edges, seedPaperId);
  }
  if (nodes.some((node) => node.nodeType === "global_semantic_entity")) {
    return layoutGlobalSemanticGraphNodes(nodes, seedNodeId);
  }

  const groups = {
    paper: nodes.filter((node) => node.nodeType === "paper"),
    author: nodes.filter((node) => node.nodeType === "author"),
    institution: nodes.filter((node) => node.nodeType === "institution"),
    field: nodes.filter((node) => node.nodeType === "field"),
    semanticEntity: nodes.filter((node) => node.nodeType === "semantic_entity"),
    unknown: nodes.filter((node) => node.nodeType === "unknown"),
  };

  const positions = new Map<string, { x: number; y: number }>();
  const placeRing = (
    group: GraphNode[],
    center: { x: number; y: number },
    baseRadius: number,
    startAngle = -Math.PI / 2,
  ) => {
    if (!group.length) return;
    if (group.length === 1) {
      positions.set(group[0].id, center);
      return;
    }

    const radius = Math.max(baseRadius, Math.min(group.length * 28, baseRadius + 160));
    group.forEach((node, index) => {
      const angle = startAngle + (index / group.length) * Math.PI * 2;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius,
      });
    });
  };

  placeRing(groups.paper, { x: 560, y: 380 }, 160);
  placeRing(groups.author, { x: 560, y: 420 }, 250, -Math.PI / 2 + 0.35);
  placeRing(groups.institution, { x: 560, y: 460 }, 330, -Math.PI / 2 + 0.7);
  placeRing(groups.field, { x: 560, y: 400 }, 410, -Math.PI / 2 + 1.05);
  placeRing(groups.semanticEntity, { x: 560, y: 420 }, 300, -Math.PI / 2 + 0.2);
  placeRing(groups.unknown, { x: 560, y: 400 }, 470, -Math.PI / 2 + 1.4);

  return nodes.map((node, index) => ({
    ...node,
    position: positions.get(node.id) ?? {
      x: 560 + (index % 8) * 90,
      y: 360 + Math.floor(index / 8) * 90,
    },
  }));
}

function layoutGlobalSemanticGraphNodes(nodes: GraphNode[], seedNodeId?: string | null) {
  const center = { x: 560, y: 420 };
  const globalNodes = nodes.filter((node) => node.nodeType === "global_semantic_entity");
  const seedNode = globalNodes.find((node) => node.id === seedNodeId) ?? globalNodes[0];
  const positions = new Map<string, { x: number; y: number }>();

  if (seedNode) positions.set(seedNode.id, center);

  const neighbors = globalNodes.filter((node) => node.id !== seedNode?.id);
  const nodesPerRing = 24;
  neighbors.forEach((node, index) => {
    const ringIndex = Math.floor(index / nodesPerRing);
    const ringStart = ringIndex * nodesPerRing;
    const ringNodeCount = Math.min(nodesPerRing, neighbors.length - ringStart);
    const indexInRing = index - ringStart;
    const radius = 205 + ringIndex * 135;
    const angle = -Math.PI / 2 + (indexInRing / Math.max(ringNodeCount, 1)) * Math.PI * 2;
    positions.set(node.id, {
      x: center.x + Math.cos(angle) * radius,
      y: center.y + Math.sin(angle) * radius,
    });
  });

  return nodes.map((node) => ({
    ...node,
    position: positions.get(node.id) ?? node.position,
  }));
}

function mergedNodeData(base?: Record<string, unknown>, incoming?: Record<string, unknown>) {
  return Object.keys({ ...(base ?? {}), ...(incoming ?? {}) }).length
    ? { ...(base ?? {}), ...(incoming ?? {}) }
    : undefined;
}

function mergeExpansionNode(existing: GraphNode, incoming: GraphNode): GraphNode {
  if (existing.nodeType === "paper" && incoming.nodeType === "paper") {
    const incomingTitle = incoming.title && paperIdentityKey(incoming.title) !== paperIdentityKey(paperCanonicalId(incoming))
      && paperIdentityKey(incoming.title) !== "untitled paper"
      ? incoming.title
      : undefined;
    const incomingAbstract = incoming.abstract && incoming.abstract !== incoming.title
      ? incoming.abstract
      : undefined;
    const incomingSummary = incoming.summary && incoming.summary !== incoming.title
      ? incoming.summary
      : undefined;
    const data = mergedNodeData(existing.data, incoming.data);
    const canonicalPaperId = paperCanonicalId({
      id: existing.id,
      paperId: incoming.paperId ?? existing.paperId,
      data,
    });

    return {
      ...existing,
      ...incoming,
      id: existing.id,
      paperId: canonicalPaperId,
      position: existing.position,
      title: incomingTitle || existing.title,
      authors: incoming.authors.length ? incoming.authors : existing.authors,
      year: incoming.year || existing.year,
      venue: incoming.venue || existing.venue,
      abstract: incomingAbstract || existing.abstract,
      doi: incoming.doi || existing.doi,
      citationCount: incoming.citationCount || existing.citationCount,
      topic: incoming.topic || existing.topic,
      metric: incoming.metric || existing.metric,
      summary: incomingSummary || existing.summary,
      label: incomingTitle || existing.title || existing.label,
      data: { ...(data ?? {}), paperId: canonicalPaperId },
    };
  }

  if (existing.nodeType === "global_semantic_entity" && incoming.nodeType === "global_semantic_entity") {
    return {
      ...existing,
      ...incoming,
      id: existing.id,
      position: existing.position,
      datasets: [...new Set([...existing.datasets, ...incoming.datasets])],
      examplePapers: [...new Set([...(existing.examplePapers ?? []), ...(incoming.examplePapers ?? [])])],
      examplePaperDetails: incoming.examplePaperDetails?.length
        ? incoming.examplePaperDetails
        : existing.examplePaperDetails,
      data: mergedNodeData(existing.data, incoming.data),
    };
  }

  return {
    ...existing,
    data: mergedNodeData(existing.data, incoming.data),
  };
}

function expansionEdgeKey(edge: GraphEdge) {
  const endpoints = edge.type === "co_occurrence"
    ? [edge.source, edge.target].sort().join("|")
    : `${edge.source}|${edge.target}`;
  const relation = edge.type === "semantic_relation"
    ? edge.relationName ?? edge.label ?? ""
    : "";
  return `${edge.type}|${endpoints}|${relation}`;
}

type ExpansionDisplaySelection = {
  expansion: GlobalSemanticGraphExpandResponse;
  limited: boolean;
};

function expansionEdgeScore(edge: GraphEdge) {
  const candidates = [
    edge.weight,
    edge.paperCount,
    typeof edge.data?.paperCount === "number" ? edge.data.paperCount : undefined,
    typeof edge.data?.mentionCount === "number" ? edge.data.mentionCount : undefined,
  ].filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return candidates.length ? Math.max(...candidates) : 0;
}

function paperIdentityKey(value: string) {
  return value.trim().replace(/^(?:paper_)+/i, "").toLowerCase();
}

function paperNodeIdForPaperId(nodes: GraphNode[], paperId?: string | null) {
  if (!paperId) return undefined;
  const canonicalKey = paperIdentityKey(paperId);
  return nodes.find((node): node is PaperNode =>
    node.nodeType === "paper" && paperIdentityKey(paperCanonicalId(node)) === canonicalKey)?.id;
}

function enrichExpansionPaperNodes(expansion: GlobalSemanticGraphExpandResponse) {
  const paperRecordsById = new Map<string, Paper>();
  expansion.papers.forEach((paper) => {
    paperRecordsById.set(paperIdentityKey(paperCanonicalId(paper)), paper);
  });
  const nodes = expansion.nodes.map((node) => {
    if (node.nodeType !== "paper") return node;
    const record = paperRecordsById.get(paperIdentityKey(paperCanonicalId(node)));
    if (!record) return node;
    return mergeExpansionNode(node, {
      ...record,
      nodeType: "paper",
      label: record.title,
      summary: record.abstract || record.title,
      metric: record.citationCount,
      position: node.position,
    });
  });
  return { ...expansion, nodes };
}

/**
 * Keep the first paint readable even when the backend returns a broad ASTRO
 * neighborhood. Ranking is deterministic and uses the same paper/edge signals
 * that are visible in the global graph; no follow-up paper lookup is needed.
 */
function selectExpansionForDisplay(expansion: GlobalSemanticGraphExpandResponse): ExpansionDisplaySelection {
  const enrichedExpansion = enrichExpansionPaperNodes(expansion);
  const nodeById = new Map(enrichedExpansion.nodes.map((node) => [node.id, node]));
  const globalNodes = enrichedExpansion.nodes.filter((node) => node.nodeType === "global_semantic_entity");
  const seedNode = globalNodes.find((node) =>
    node.nodeType === "global_semantic_entity"
      && (node.id === expansion.seed.globalEntityId || node.globalEntityId === expansion.seed.globalEntityId),
  );
  const seedId = seedNode?.id || globalNodes[0]?.id;
  const relationEdges = expansion.edges.filter((edge) =>
    (edge.type === "co_occurrence" || edge.type === "semantic_relation")
      && nodeById.get(edge.source)?.nodeType === "global_semantic_entity"
      && nodeById.get(edge.target)?.nodeType === "global_semantic_entity",
  );
  const relationWeightByNode = new Map<string, number>();
  relationEdges.forEach((edge) => {
    const score = expansionEdgeScore(edge);
    relationWeightByNode.set(edge.source, Math.max(relationWeightByNode.get(edge.source) ?? 0, score));
    relationWeightByNode.set(edge.target, Math.max(relationWeightByNode.get(edge.target) ?? 0, score));
  });
  const globalRank = (node: GraphNode) => node.nodeType === "global_semantic_entity"
    ? node.paperCount * 2 + (relationWeightByNode.get(node.id) ?? 0)
    : 0;
  const rankedGlobals = [...globalNodes].sort((left, right) => {
    if (left.id === seedId) return -1;
    if (right.id === seedId) return 1;
    return globalRank(right) - globalRank(left) || left.label.localeCompare(right.label);
  });
  const keptGlobalNodes = rankedGlobals.slice(0, GLOBAL_EXPANSION_ENTITY_LIMIT);
  const keptGlobalIds = new Set(keptGlobalNodes.map((node) => node.id));

  const rankedRelations = relationEdges
    .filter((edge) => keptGlobalIds.has(edge.source) && keptGlobalIds.has(edge.target))
    .sort((left, right) => {
      const leftTouchesSeed = left.source === seedId || left.target === seedId;
      const rightTouchesSeed = right.source === seedId || right.target === seedId;
      return expansionEdgeScore(right) - expansionEdgeScore(left)
        || Number(rightTouchesSeed) - Number(leftTouchesSeed)
        || left.id.localeCompare(right.id);
    });
  const requiredRelationIds = new Set<string>();
  keptGlobalIds.forEach((globalId) => {
    if (globalId === seedId) return;
    const connection = rankedRelations.find((edge) => edge.source === globalId || edge.target === globalId);
    if (connection) requiredRelationIds.add(connection.id);
  });
  const keptRelationIds = new Set([
    ...requiredRelationIds,
    ...rankedRelations
      .filter((edge) => !requiredRelationIds.has(edge.id))
      .slice(0, Math.max(0, GLOBAL_EXPANSION_RELATION_LIMIT - requiredRelationIds.size))
      .map((edge) => edge.id),
  ]);

  const paperNodes = enrichedExpansion.nodes.filter((node): node is PaperNode => node.nodeType === "paper");
  const mentionEdges = expansion.edges.filter((edge) => edge.type === "entity_mentioned_in_paper");
  const paperScoreById = new Map<string, number>();
  paperNodes.forEach((paper) => {
    const keptMentionEdges = mentionEdges
      .filter((edge) => edge.source === paper.id || edge.target === paper.id)
      .filter((edge) => {
        const globalEndpoint = nodeById.get(edge.source)?.nodeType === "global_semantic_entity"
          ? edge.source
          : nodeById.get(edge.target)?.nodeType === "global_semantic_entity" ? edge.target : undefined;
        return Boolean(globalEndpoint && keptGlobalIds.has(globalEndpoint));
      });
    if (keptMentionEdges.length) {
      const score = keptMentionEdges.reduce((max, edge) => Math.max(max, expansionEdgeScore(edge)), 0);
      paperScoreById.set(paper.id, score * 10 + paper.citationCount / 1000);
    }
  });
  const evidenceLimit = Math.max(1, Math.min(GLOBAL_EXPANSION_EVIDENCE_LIMIT, expansion.stats.evidenceLimit || GLOBAL_EXPANSION_EVIDENCE_LIMIT));
  const rankedPapers = paperNodes
    .filter((paper) => paperScoreById.has(paper.id))
    .sort((left, right) =>
      (paperScoreById.get(right.id) ?? 0) - (paperScoreById.get(left.id) ?? 0)
        || left.title.localeCompare(right.title),
    );
  const keptPaperNodes = rankedPapers.slice(0, evidenceLimit);
  const seedPaperKey = paperIdentityKey(expansion.seed.paperId);
  const seedPaper = paperNodes.find((paper) => paperIdentityKey(paperCanonicalId(paper)) === seedPaperKey);
  if (seedPaper && paperScoreById.has(seedPaper.id) && !keptPaperNodes.some((paper) => paper.id === seedPaper.id)) {
    keptPaperNodes.splice(Math.max(0, keptPaperNodes.length - 1), 1, seedPaper);
  }
  const keptPaperIds = new Set(keptPaperNodes.map((node) => node.id));
  const keptNodeIds = new Set([...keptGlobalIds, ...keptPaperIds]);

  const nodes = enrichedExpansion.nodes.filter((node) => {
    if (node.nodeType === "global_semantic_entity" || node.nodeType === "paper") return keptNodeIds.has(node.id);
    return true;
  });
  const edges = expansion.edges.filter((edge) => {
    const sourceNode = nodeById.get(edge.source);
    const targetNode = nodeById.get(edge.target);
    if (edge.type === "local_to_global") {
      return (sourceNode?.nodeType === "global_semantic_entity" && keptGlobalIds.has(edge.source))
        || (targetNode?.nodeType === "global_semantic_entity" && keptGlobalIds.has(edge.target));
    }
    if (edge.type === "co_occurrence" || edge.type === "semantic_relation") {
      return keptRelationIds.has(edge.id);
    }
    if (edge.type === "entity_mentioned_in_paper") {
      const globalId = sourceNode?.nodeType === "global_semantic_entity"
        ? sourceNode.id
        : targetNode?.nodeType === "global_semantic_entity" ? targetNode.id : undefined;
      const paperId = sourceNode?.nodeType === "paper"
        ? sourceNode.id
        : targetNode?.nodeType === "paper" ? targetNode.id : undefined;
      return Boolean(globalId && paperId && keptGlobalIds.has(globalId) && keptPaperIds.has(paperId));
    }
    return (!sourceNode || !["global_semantic_entity", "paper"].includes(sourceNode.nodeType) || keptNodeIds.has(sourceNode.id))
      && (!targetNode || !["global_semantic_entity", "paper"].includes(targetNode.nodeType) || keptNodeIds.has(targetNode.id));
  });
  const papers = expansion.papers.filter((paper) => keptPaperNodes.some((node) =>
    paperIdentityKey(paperCanonicalId(paper)) === paperIdentityKey(paperCanonicalId(node)),
  ));
  const limited = expansion.stats.truncated
    || nodes.length < expansion.nodes.length
    || edges.length < expansion.edges.length;

  return {
    expansion: { ...enrichedExpansion, nodes, edges, papers },
    limited,
  };
}

function mergeGlobalSemanticExpansion(
  baseGraph: GraphResponse,
  expansion: GlobalSemanticGraphExpandResponse,
  localSeedNodeId: string,
) {
  const nodes = [...baseGraph.nodes];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const paperByCanonicalId = new Map<string, GraphNode>();
  nodes.forEach((node) => {
    const canonicalPaperId = node.nodeType === "paper" ? paperCanonicalId(node) : undefined;
    if (canonicalPaperId) paperByCanonicalId.set(canonicalPaperId.toLowerCase(), node);
  });

  const expansionNodeIdMap = new Map<string, string>();
  const expansionPaperIdMap = new Map<string, string>();
  const addedNodeIds = new Set<string>();
  const setExpansionAlias = (alias: string, mappedId: string) => {
    if (!alias) return;
    expansionNodeIdMap.set(alias, mappedId);
    expansionNodeIdMap.set(alias.toLowerCase(), mappedId);
  };
  const replaceMergedNode = (existing: GraphNode, incoming: GraphNode) => {
    const merged = mergeExpansionNode(existing, incoming);
    const index = nodes.findIndex((node) => node.id === existing.id);
    if (index >= 0) nodes[index] = merged;
    nodeById.set(merged.id, merged);
    return merged;
  };

  expansion.nodes.forEach((incoming) => {
    let mappedId = incoming.id;
    let existing: GraphNode | undefined;
    const canonicalPaperId = incoming.nodeType === "paper" ? paperCanonicalId(incoming) : undefined;
    const canonicalPaperKey = canonicalPaperId?.toLowerCase();

    if (incoming.nodeType === "paper" && canonicalPaperId && canonicalPaperKey) {
      const duplicateMappedId = expansionPaperIdMap.get(canonicalPaperKey);
      if (duplicateMappedId) {
        mappedId = duplicateMappedId;
        existing = nodeById.get(mappedId);
        setExpansionAlias(incoming.id, mappedId);
        setExpansionAlias(canonicalPaperId, mappedId);
        setExpansionAlias(`paper_${canonicalPaperId}`, mappedId);
        if (existing?.nodeType === "paper") {
          const merged = replaceMergedNode(existing, incoming);
          paperByCanonicalId.set(canonicalPaperKey, merged);
        }
        return;
      }
      existing = paperByCanonicalId.get(canonicalPaperKey);
      if (existing) mappedId = existing.id;
    } else {
      existing = nodeById.get(incoming.id);
      if (existing?.nodeType !== incoming.nodeType) existing = undefined;
    }

    setExpansionAlias(incoming.id, mappedId);
    if (incoming.nodeType === "paper" && canonicalPaperId) {
      setExpansionAlias(canonicalPaperId, mappedId);
      setExpansionAlias(`paper_${canonicalPaperId}`, mappedId);
      if (canonicalPaperKey) expansionPaperIdMap.set(canonicalPaperKey, mappedId);
    }
    if (incoming.nodeType === "global_semantic_entity" && incoming.globalEntityId) {
      setExpansionAlias(incoming.globalEntityId, mappedId);
    }
    if (existing) {
      const merged = replaceMergedNode(existing, incoming);
      if (incoming.nodeType === "paper" && canonicalPaperKey) {
        paperByCanonicalId.set(canonicalPaperKey, merged);
      }
      return;
    }

    nodes.push(incoming);
    nodeById.set(mappedId, incoming);
    if (incoming.nodeType === "paper" && canonicalPaperKey) {
      paperByCanonicalId.set(canonicalPaperKey, incoming);
    }
    addedNodeIds.add(mappedId);
  });

  const localSeed = nodes.find((node) => node.id === localSeedNodeId)
    ?? nodes.find((node) => node.nodeType === "semantic_entity" && (
      node.entityId === localSeedNodeId
      || node.id.endsWith(`:semantic_entity:${localSeedNodeId}`)
    ));
  const localEntityIds = new Map<string, string>();
  nodes.forEach((node) => {
    if (node.nodeType !== "semantic_entity") return;
    localEntityIds.set(node.id, node.id);
    localEntityIds.set(node.id.toLowerCase(), node.id);
    if (node.entityId) {
      localEntityIds.set(node.entityId, node.id);
      localEntityIds.set(node.entityId.toLowerCase(), node.id);
    }
    const suffix = node.id.split(":semantic_entity:")[1];
    if (suffix) {
      localEntityIds.set(suffix, node.id);
      localEntityIds.set(suffix.toLowerCase(), node.id);
    }
  });

  const resolveExpansionNodeId = (id: string) => {
    const mapped = expansionNodeIdMap.get(id) ?? id;
    if (nodeById.has(mapped)) return mapped;
    return localEntityIds.get(id) ?? localEntityIds.get(id.toLowerCase());
  };

  const edges = [...baseGraph.edges];
  const edgeKeys = new Set(edges.map(expansionEdgeKey));
  const addedEdgeIds = new Set<string>();
  expansion.edges.forEach((incoming) => {
    const source = resolveExpansionNodeId(incoming.source);
    const target = resolveExpansionNodeId(incoming.target);
    if (!source || !target || source === target) return;
    const edge = source === incoming.source && target === incoming.target
      ? incoming
      : { ...incoming, source, target };
    const key = expansionEdgeKey(edge);
    if (edgeKeys.has(key)) return;
    let edgeId = edge.id;
    if (edges.some((existing) => existing.id === edgeId)) edgeId = `${edgeId}-global`;
    const normalizedEdge = edgeId === edge.id ? edge : { ...edge, id: edgeId };
    edges.push(normalizedEdge);
    edgeKeys.add(key);
    addedEdgeIds.add(edgeId);
  });

  const papersById = new Map<string, Paper>();
  const normalizedPaperNodes = nodes.filter((node): node is PaperNode => node.nodeType === "paper");
  const paperRecords: Paper[] = [
    ...baseGraph.papers,
    ...normalizedPaperNodes.map((node) => ({ ...node })),
  ];
  paperRecords.forEach((paper) => {
    const canonicalId = paperCanonicalId(paper).toLowerCase();
    const existing = papersById.get(canonicalId);
    if (!existing || (existing.id.startsWith("paper_") && !paper.id.startsWith("paper_"))) {
      papersById.set(canonicalId, paper);
    }
  });

  const incomingGlobalSeed = expansion.nodes.find((node) =>
    node.nodeType === "global_semantic_entity"
      && (node.id === expansion.seed.globalEntityId || node.globalEntityId === expansion.seed.globalEntityId),
  ) ?? expansion.nodes.find((node) => node.nodeType === "global_semantic_entity");
  const globalSeedId = incomingGlobalSeed
    ? expansionNodeIdMap.get(incomingGlobalSeed.id)
      ?? (incomingGlobalSeed.nodeType === "global_semantic_entity"
        ? expansionNodeIdMap.get(incomingGlobalSeed.globalEntityId)
        : undefined)
      ?? incomingGlobalSeed.id
    : undefined;

  return {
    graph: {
      ...baseGraph,
      nodes,
      papers: [...papersById.values()],
      edges,
      warnings: [...new Set([...(baseGraph.warnings ?? []), ...(expansion.warnings ?? [])])],
      seedPaperId: baseGraph.seedPaperId ?? expansion.seed.paperId,
    },
    addedNodeIds,
    addedEdgeIds,
    globalSeedId,
    localSeedId: localSeed?.id ?? localSeedNodeId,
  };
}

function layoutExpandedSemanticGraphNodes(
  nodes: GraphNode[],
  edges: GraphResponse["edges"],
  localSeedNodeId: string,
  globalSeedId: string | undefined,
  addedNodeIds: Set<string>,
) {
  // Start with every existing position and only assign coordinates to nodes
  // introduced by this expansion. The original Semantic Graph therefore keeps
  // its layout exactly as it was before the request.
  const positions = new Map(nodes.map((node) => [node.id, node.position]));
  const baseNodes = nodes.filter((node) => !addedNodeIds.has(node.id));
  const localSeed = nodes.find((node) => node.id === localSeedNodeId)
    ?? nodes.find((node) => node.nodeType === "semantic_entity" && (
      node.entityId === localSeedNodeId
      || node.id.endsWith(`:semantic_entity:${localSeedNodeId}`)
    ));
  const localCenter = localSeed?.position ?? { x: 560, y: 420 };
  const centroid = baseNodes.length
    ? baseNodes.reduce((sum, node) => ({ x: sum.x + node.position.x, y: sum.y + node.position.y }), { x: 0, y: 0 })
    : { x: 560, y: 420 };
  if (baseNodes.length) {
    centroid.x /= baseNodes.length;
    centroid.y /= baseNodes.length;
  }
  const vector = { x: localCenter.x - centroid.x, y: localCenter.y - centroid.y };
  const vectorLength = Math.hypot(vector.x, vector.y);
  const outward = vectorLength > 1
    ? { x: vector.x / vectorLength, y: vector.y / vectorLength }
    : { x: 1, y: 0 };
  const outwardAngle = Math.atan2(outward.y, outward.x);

  const effectiveGlobalSeedId = globalSeedId
    ?? nodes.find((node) => node.nodeType === "global_semantic_entity")?.id;
  const globalSeed = effectiveGlobalSeedId
    ? nodes.find((node) => node.id === effectiveGlobalSeedId)
    : undefined;
  const globalWeightById = new Map<string, number>();
  edges.forEach((edge) => {
    if (edge.type !== "co_occurrence" && edge.type !== "semantic_relation") return;
    const score = expansionEdgeScore(edge);
    globalWeightById.set(edge.source, Math.max(globalWeightById.get(edge.source) ?? 0, score));
    globalWeightById.set(edge.target, Math.max(globalWeightById.get(edge.target) ?? 0, score));
  });
  const newGlobalNodes = nodes
    .filter((node) => addedNodeIds.has(node.id)
      && node.nodeType === "global_semantic_entity"
      && node.id !== effectiveGlobalSeedId)
    .sort((left, right) => {
      const leftScore = left.nodeType === "global_semantic_entity"
        ? left.paperCount * 2 + (globalWeightById.get(left.id) ?? 0)
        : 0;
      const rightScore = right.nodeType === "global_semantic_entity"
        ? right.paperCount * 2 + (globalWeightById.get(right.id) ?? 0)
        : 0;
      return rightScore - leftScore || left.id.localeCompare(right.id);
    });
  const ringCapacity = 7;
  const ringCount = Math.max(1, Math.ceil(newGlobalNodes.length / ringCapacity));
  const baseRadius = 210;
  const globalOffset = Math.min(500, 350 + Math.max(0, ringCount - 1) * 40);
  const globalCenter = globalSeed && !addedNodeIds.has(globalSeed.id)
    ? globalSeed.position
    : {
        x: localCenter.x + outward.x * globalOffset,
        y: localCenter.y + outward.y * globalOffset,
      };

  if (globalSeed && addedNodeIds.has(globalSeed.id)) positions.set(globalSeed.id, globalCenter);

  newGlobalNodes.forEach((node, index) => {
    const ringIndex = Math.floor(index / ringCapacity);
    const ringStart = ringIndex * ringCapacity;
    const ringNodeCount = Math.min(ringCapacity, newGlobalNodes.length - ringStart);
    const indexInRing = index - ringStart;
    const radius = baseRadius + ringIndex * 150;
    const arcSpan = Math.PI * 1.35;
    const angle = ringNodeCount === 1
      ? outwardAngle
      : outwardAngle - arcSpan / 2 + (indexInRing / (ringNodeCount - 1)) * arcSpan;
    positions.set(node.id, {
      x: globalCenter.x + Math.cos(angle) * radius,
      y: globalCenter.y + Math.sin(angle) * radius,
    });
  });

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const newPaperNodes = nodes.filter((node): node is PaperNode =>
    addedNodeIds.has(node.id) && node.nodeType === "paper",
  );
  const papersByAnchor = new Map<string, PaperNode[]>();
  newPaperNodes.forEach((paper) => {
    const mentionEdges = edges
      .filter((edge) => edge.type === "entity_mentioned_in_paper"
        && (edge.source === paper.id || edge.target === paper.id))
      .map((edge) => ({
        edge,
        anchor: nodeById.get(edge.source)?.nodeType === "global_semantic_entity"
          ? edge.source
          : nodeById.get(edge.target)?.nodeType === "global_semantic_entity" ? edge.target : undefined,
      }))
      .filter((item): item is { edge: GraphEdge; anchor: string } => Boolean(item.anchor))
      .sort((left, right) => expansionEdgeScore(right.edge) - expansionEdgeScore(left.edge));
    const anchor = mentionEdges[0]?.anchor ?? effectiveGlobalSeedId ?? localSeedNodeId;
    const group = papersByAnchor.get(anchor) ?? [];
    group.push(paper);
    papersByAnchor.set(anchor, group);
  });

  papersByAnchor.forEach((group, anchorId) => {
    const anchorPosition = positions.get(anchorId) ?? nodeById.get(anchorId)?.position ?? globalCenter;
    const anchorVector = { x: anchorPosition.x - globalCenter.x, y: anchorPosition.y - globalCenter.y };
    const anchorLength = Math.hypot(anchorVector.x, anchorVector.y);
    const radial = anchorLength > 1 ? { x: anchorVector.x / anchorLength, y: anchorVector.y / anchorLength } : outward;
    const perpendicular = { x: -radial.y, y: radial.x };
    const distance = 180 + Math.min(80, group.length * 14);
    const spacing = 130;
    group.forEach((paper, index) => {
      const offset = (index - (group.length - 1) / 2) * spacing;
      positions.set(paper.id, {
        x: anchorPosition.x + radial.x * distance + perpendicular.x * offset,
        y: anchorPosition.y + radial.y * distance + perpendicular.y * offset,
      });
    });
  });

  return nodes.map((node) => ({
    ...node,
    position: positions.get(node.id) ?? node.position,
  }));
}

function layoutConnectionsGraphNodes(
  nodes: GraphNode[],
  edges: GraphResponse["edges"],
  seedPaperId?: string | null,
) {
  const center = { x: 560, y: 420 };
  const positions = new Map<string, { x: number; y: number }>();
  const paperNodes = nodes.filter((node) => node.nodeType === "paper");
  const seedId = paperNodeIdForPaperId(paperNodes, seedPaperId)
    || paperNodes.find((node) =>
    edges.some((edge) => edge.type === "authored_by" && edge.source === node.id),
  )?.id || paperNodes[0]?.id;

  if (seedId) {
    positions.set(seedId, center);
  }

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const uniqueNodes = (ids: string[]) => ids
    .filter((id, index, values) => values.indexOf(id) === index)
    .map((id) => nodeById.get(id))
    .filter((node): node is GraphNode => Boolean(node));

  const authors = uniqueNodes(edges
    .filter((edge) => edge.type === "authored_by" && edge.source === seedId)
    .map((edge) => edge.target));
  const institutions = uniqueNodes(edges
    .filter((edge) => edge.type === "affiliated_with")
    .map((edge) => edge.target));
  const references = uniqueNodes(edges
    .filter((edge) => edge.type === "cites" && edge.source === seedId && edge.target !== seedId)
    .map((edge) => edge.target));
  const citingPapers = uniqueNodes(edges
    .filter((edge) => edge.type === "cites" && edge.target === seedId && edge.source !== seedId)
    .map((edge) => edge.source));

  const placeRing = (group: GraphNode[], radius: number, startAngle = -Math.PI / 2) => {
    if (!group.length) return;
    group.forEach((node, index) => {
      const angle = startAngle + (index / group.length) * Math.PI * 2;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius,
      });
    });
  };

  const placeHalfArc = (group: GraphNode[], radius: number, upper: boolean) => {
    if (!group.length) return;
    if (group.length === 1) {
      positions.set(group[0].id, { x: center.x, y: center.y + (upper ? -radius : radius) });
      return;
    }

    const startAngle = upper ? -Math.PI + 0.24 : 0.24;
    const endAngle = upper ? -0.24 : Math.PI - 0.24;
    group.forEach((node, index) => {
      const ratio = index / (group.length - 1);
      const angle = startAngle + ratio * (endAngle - startAngle);
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius,
      });
    });
  };

  placeRing(authors, 150, -Math.PI / 2);
  placeRing(institutions, 265, -Math.PI / 2 + 0.28);
  placeHalfArc(references, 430, true);
  placeHalfArc(citingPapers, 430, false);

  const alreadyPlaced = new Set(positions.keys());
  const remainingPapers = paperNodes.filter((node) => !alreadyPlaced.has(node.id));
  placeRing(remainingPapers, 470, -Math.PI / 2 + 0.6);

  return nodes.map((node, index) => ({
    ...node,
    position: positions.get(node.id) ?? {
      x: center.x + (index % 6) * 95 - 240,
      y: center.y + Math.floor(index / 6) * 95 + 500,
    },
  }));
}

function meaningfulGraphText(value?: string) {
  const text = value?.trim();
  if (!text) return undefined;
  const normalized = text.toLowerCase();
  if (
    normalized === "unknown"
    || normalized === "n/a"
    || normalized === "unknown author"
    || normalized === "unknown authors"
  ) return undefined;
  return text;
}

function graphDataString(data: Record<string, unknown> | undefined, ...keys: string[]) {
  if (!data) return undefined;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string") {
      const text = meaningfulGraphText(value);
      if (text) return text;
    }
  }
  return undefined;
}

function isSemanticGraphDisplay(graph: Pick<GraphResponse, "title" | "queryContext">, fallbackTitle?: string) {
  const context = [graph.title, graph.queryContext, fallbackTitle]
    .filter((value): value is string => Boolean(value))
    .join(" ")
    .toLowerCase();
  return context.includes("semantic graph") || context.includes("semantic_graph") || context.includes("dblp-kg");
}

function isPseudoSemanticAuthor(node: GraphNode) {
  if (node.nodeType !== "author") return false;

  const normalizedLabel = node.label.trim().toLowerCase();
  if (normalizedLabel === "unknown author" || normalizedLabel === "unknown authors") return true;

  const data = node.data;
  const authorId = meaningfulGraphText(node.authorId)
    ?? graphDataString(data, "authorId", "authorid", "author_id", "openalexId", "openalex_id", "orcid");
  const authorName = meaningfulGraphText(node.displayName)
    ?? meaningfulGraphText(node.authorName)
    ?? graphDataString(data, "displayName", "display_name", "authorName", "author_name", "name");
  return !authorId && !authorName;
}

function isOrphanPlaceholderInstitution(node: GraphNode, connectedIds: Set<string>) {
  if (node.nodeType !== "institution" || connectedIds.has(node.id)) return false;

  const label = node.label.trim().toLowerCase();
  const institutionId = meaningfulGraphText(node.institutionId)
    ?? graphDataString(node.data, "institutionId", "institutionid", "institution_id", "ror", "homepageUrl");

  return !institutionId && (label === "icde research institute" || label.endsWith(" research institute"));
}

function prepareGraphForDisplay(graph: GraphResponse, fallbackTitle?: string, forceSemanticGraph = false): GraphResponse {
  if (!forceSemanticGraph && !isSemanticGraphDisplay(graph, fallbackTitle)) return graph;

  const hiddenAuthorIds = new Set(graph.nodes
    .filter(isPseudoSemanticAuthor)
    .map((node) => node.id));

  if (!hiddenAuthorIds.size) return graph;

  const edgesWithoutPseudoAuthors = graph.edges.filter((edge) =>
    !hiddenAuthorIds.has(edge.source) && !hiddenAuthorIds.has(edge.target),
  );
  const connectedIds = new Set<string>();
  edgesWithoutPseudoAuthors.forEach((edge) => {
    connectedIds.add(edge.source);
    connectedIds.add(edge.target);
  });

  const displayNodes = graph.nodes
    .filter((node) => !hiddenAuthorIds.has(node.id))
    .filter((node) => !isOrphanPlaceholderInstitution(node, connectedIds));
  const nodeIds = new Set(displayNodes.map((node) => node.id));
  const edges = edgesWithoutPseudoAuthors.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  const nodePaperIds = new Set(displayNodes
    .filter((node) => node.nodeType === "paper")
    .map((node) => node.id));
  const papers = graph.papers.filter((paper) =>
    nodePaperIds.has(paper.id) || !graph.nodes.some((node) => node.nodeType === "paper" && node.id === paper.id),
  );
  const seedPaperNodeId = paperNodeIdForPaperId(displayNodes, graph.seedPaperId);
  const seedNodeId = graph.seedNodeId && nodeIds.has(graph.seedNodeId)
    ? graph.seedNodeId
    : seedPaperNodeId;

  return {
    ...graph,
    nodes: displayNodes,
    papers,
    edges,
    seedNodeId,
  };
}

function graphFromWorkspace(detail: WorkspaceDetail): GraphResponse {
  const displayGraph = prepareGraphForDisplay({
    graphId: detail.initialGraph.graphId,
    workspaceId: detail.workspace.id,
    title: detail.initialGraph.title || detail.workspace.title,
    nodes: detail.initialGraph.nodes ?? [],
    papers: detail.initialGraph.papers ?? [],
    edges: detail.initialGraph.edges ?? [],
    seedPaperId: detail.initialGraph.seedPaperId ?? undefined,
    seedNodeId: detail.initialGraph.seedNodeId ?? undefined,
    queryContext: detail.initialGraph.queryContext || detail.initialGraph.title || detail.workspace.title,
    defaultViewMode: detail.initialGraph.defaultViewMode ?? detail.initialViewMode,
    warnings: detail.initialGraph.warnings ?? [],
  }, detail.workspace.title);
  const nodes = layoutGraphNodes(
    displayGraph.nodes,
    displayGraph.edges,
    displayGraph.seedPaperId,
    displayGraph.title,
    displayGraph.queryContext,
    displayGraph.seedNodeId,
  );
  const nodePapers = nodes.filter((node): node is PaperNode => node.nodeType === "paper");

  return {
    ...displayGraph,
    nodes,
    papers: displayGraph.papers.length ? displayGraph.papers : nodePapers,
  };
}

function directMockWorkspace(): WorkspaceDetail {
  return {
    workspace: {
      id: "local-root",
      entryType: "paper_search",
      title: "BigDipper Scholar Demo Graph",
      preview: "Mock papers, authors, institutions, citations, and collaboration",
      conversationId: null,
      activeGraphStateId: "local-root-graph",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    },
    initialViewMode: "default",
    initialGraph: {
      graphId: "local-root-graph",
      title: ROOT_GRAPH_TITLE,
      queryContext: ROOT_GRAPH_TITLE,
      seedNodeId: mockGraph.seedNodeId ?? null,
      seedPaperId: mockGraph.seedPaperId ?? null,
      defaultViewMode: mockGraph.defaultViewMode,
      papers: mockGraph.papers,
      warnings: mockGraph.warnings ?? [],
      nodes: mockGraph.nodes,
      edges: mockGraph.edges,
    },
    initialChatMessages: [
      {
        role: "assistant",
        content: DEMO_AGENT_MESSAGE,
        createdAt: new Date().toISOString(),
      },
    ],
  };
}

function WorkspaceView({
  detail,
  displayName,
  accessToken,
  onHome,
  onConversationResolved,
}: {
  detail: WorkspaceDetail;
  displayName: string;
  accessToken: string;
  onHome: () => void;
  onConversationResolved: (workspaceId: string, conversationId: string) => void;
}) {
  const [rootGraph] = useState<GraphResponse>(() => graphFromWorkspace(detail));
  const initialViewMode = rootGraph.defaultViewMode ?? detail.initialViewMode;
  const [graphData, setGraphData] = useState<GraphResponse>(() => rootGraph);
  const { nodes, papers, edges, seedPaperId } = graphData;
  const seedPaper = seedPaperId
    ? papers.find((paper) => paper.id === seedPaperId)
      ?? papers.find((paper) => paperIdentityKey(paperCanonicalId(paper)) === paperIdentityKey(seedPaperId))
      ?? null
    : null;

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(rootGraph.seedNodeId ?? null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [queryContext, setQueryContext] = useState(rootGraph.title || ROOT_GRAPH_TITLE);
  const [filteredIds, setFilteredIds] = useState<Set<string> | null>(null);
  const [nodeTypeFilter, setNodeTypeFilter] = useState<NodeTypeFilter>("all");
  const [viewMode, setViewMode] = useState<ViewMode>(initialViewMode);
  const [listCollapsed, setListCollapsed] = useState(initialViewMode === "graph");
  const [graphHistory, setGraphHistory] = useState<GraphViewState[]>([]);
  const [graphStates, setGraphStates] = useState<GraphStateSummary[]>([]);
  const [isGraphLoading, setIsGraphLoading] = useState(false);
  const [exploringMode, setExploringMode] = useState<ExploreMode | null>(null);
  const [graphWarning, setGraphWarning] = useState<string | null>(
    rootGraph.warnings?.length ? rootGraph.warnings.join(" ") : null,
  );
  const [globalSemanticGraph, setGlobalSemanticGraph] = useState<GlobalSemanticGraphView | null>(null);
  const [globalSemanticBaseState, setGlobalSemanticBaseState] = useState<GraphViewState | null>(null);

  useEffect(() => {
    if (detail.workspace.id === "local-root") return;

    void listGraphStates(accessToken, detail.workspace.id)
      .then((response) => setGraphStates(response.items ?? []))
      .catch(() => setGraphStates([]));
  }, [accessToken, detail.workspace.id]);

  const selectedNode = nodes.find((node) => node.id === selectedNodeId) ?? null;
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId) ?? null;
  const selectedEdgeSourceNode = selectedEdge
    ? nodes.find((node) => node.id === selectedEdge.source) ?? null
    : null;
  const selectedEdgeTargetNode = selectedEdge
    ? nodes.find((node) => node.id === selectedEdge.target) ?? null
    : null;
  const selectedPaper = selectedNode?.nodeType === "paper" ? selectedNode : null;
  const effectiveListCollapsed = viewMode === "graph" || listCollapsed;
  const hasMixedRootNodes = nodes.some((node) => node.nodeType !== "paper");
  const isConnectionsGraph = graphData.queryContext === "Paper connections" || Boolean(graphData.title?.startsWith("Connections:"));
  const firstPaper = nodes.find((node): node is PaperNode => node.nodeType === "paper");
  const globalDataset = globalSemanticDatasetForPaperId(
    seedPaperId ?? (firstPaper ? paperCanonicalId(firstPaper) : undefined),
  );

  const visibleNodes = useMemo(() => {
    const typeMatched = nodeTypeFilter === "all"
      ? nodes
      : nodes.filter((node) => node.nodeType === nodeTypeFilter);

    if (!filteredIds) return typeMatched;
    return typeMatched.filter((node) => filteredIds.has(node.id));
  }, [filteredIds, nodeTypeFilter, nodes]);

  const visibleEdges = useMemo(() => {
    const visibleIds = new Set(visibleNodes.map((node) => node.id));
    return edges.filter((edge) => edge.source !== edge.target && visibleIds.has(edge.source) && visibleIds.has(edge.target));
  }, [edges, visibleNodes]);

  const selectNode = useCallback((nodeId: string | null) => {
    setSelectedNodeId(nodeId);
    if (nodeId) setSelectedEdgeId(null);
  }, []);

  const selectEdge = useCallback((edgeId: string | null) => {
    if (edgeId) {
      const edge = edges.find((candidate) => candidate.id === edgeId);
      const hasGlobalEndpoint = edge
        ? nodes.some((node) => node.nodeType === "global_semantic_entity" && (node.id === edge.source || node.id === edge.target))
        : false;
      if (edge?.type === "semantic_relation" && !hasGlobalEndpoint) return;
    }
    setSelectedEdgeId(edgeId);
    if (edgeId) setSelectedNodeId(null);
  }, [edges, nodes]);

  const clearSelection = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  }, []);

  useEffect(() => {
    setSelectedEdgeId(null);
  }, [graphData]);

  const restoreGraphView = useCallback((state: GraphViewState) => {
    setGraphData(state.graphData);
    setQueryContext(state.queryContext);
    setFilteredIds(state.filteredIds ? new Set(state.filteredIds) : null);
    setNodeTypeFilter(state.nodeTypeFilter);
    setSelectedNodeId(state.selectedNodeId);
    setViewMode(state.viewMode);
    setListCollapsed(state.listCollapsed);
    setGraphWarning(state.graphWarning);
    setGlobalSemanticGraph(state.globalSemanticGraph);
    setGlobalSemanticBaseState(state.globalSemanticBaseState);
    setExploringMode(null);
  }, []);

  const resetGlobalExpansion = useCallback(() => {
    if (!globalSemanticBaseState) return;
    restoreGraphView(globalSemanticBaseState);
    setGlobalSemanticBaseState(null);
    setGlobalSemanticGraph(null);
    setSelectedEdgeId(null);
  }, [globalSemanticBaseState, restoreGraphView]);

  const returnToPreviousGraph = useCallback(() => {
    if (globalSemanticBaseState) {
      resetGlobalExpansion();
      return;
    }

    setGraphHistory((history) => {
      const previous = history.at(-1);
      if (!previous) return history;

      restoreGraphView(previous);
      return history.slice(0, -1);
    });
  }, [globalSemanticBaseState, resetGlobalExpansion, restoreGraphView]);

  const changeNodeTypeFilter = useCallback((nextFilter: NodeTypeFilter) => {
    setNodeTypeFilter(nextFilter);
    setFilteredIds(null);
    setSelectedNodeId(null);
    setQueryContext(
      nextFilter === "all" ? detail.initialGraph.title || ROOT_GRAPH_TITLE : `${nodeTypeLabel(nextFilter)} nodes`,
    );
  }, [detail.initialGraph.title]);

  const changeViewMode = useCallback((mode: ViewMode) => {
    setViewMode(mode);
    setListCollapsed(mode === "graph");
  }, []);

  const collapsePaperList = useCallback(() => {
    setViewMode("graph");
    setListCollapsed(true);
  }, []);

  const expandPaperList = useCallback(() => {
    setViewMode("default");
    setListCollapsed(false);
  }, []);

  const rememberCurrentGraph = useCallback((): GraphViewState => ({
    graphData,
    queryContext,
    filteredIds: filteredIds ? new Set(filteredIds) : null,
    nodeTypeFilter,
    selectedNodeId,
    viewMode,
    listCollapsed,
    graphWarning,
    globalSemanticGraph,
    globalSemanticBaseState,
  }), [filteredIds, globalSemanticBaseState, globalSemanticGraph, graphData, graphWarning, listCollapsed, nodeTypeFilter, queryContext, selectedNodeId, viewMode]);

  const applyGraphState = useCallback((nextGraph: GraphResponse, fallbackTitle: string, options: { semanticGraph?: boolean; globalSemanticGraph?: GlobalSemanticGraphView | null } = {}) => {
    const graphForDisplay = prepareGraphForDisplay(nextGraph, fallbackTitle, options.semanticGraph);
    const nextContext = graphForDisplay.title ?? graphForDisplay.queryContext ?? fallbackTitle;
    const nextNodes = layoutGraphNodes(
      graphForDisplay.nodes,
      graphForDisplay.edges,
      graphForDisplay.seedPaperId,
      graphForDisplay.title,
      graphForDisplay.queryContext,
      graphForDisplay.seedNodeId,
    );
    const nextPapers = nextNodes.filter((node): node is PaperNode => node.nodeType === "paper");
    const hasMixedNodes = nextNodes.some((node) => node.nodeType !== "paper");
    const nextSeedNodeId = graphForDisplay.seedNodeId
      ?? paperNodeIdForPaperId(nextNodes, graphForDisplay.seedPaperId);

    setGraphData({
      ...graphForDisplay,
      nodes: nextNodes,
      papers: nextPapers.length ? nextPapers : graphForDisplay.papers,
      title: graphForDisplay.title ?? nextContext,
      queryContext: graphForDisplay.queryContext ?? nextContext,
      seedPaperId: graphForDisplay.seedPaperId,
      seedNodeId: nextSeedNodeId,
    });
    setQueryContext(nextContext);
    setFilteredIds(null);
    setSelectedNodeId(nextSeedNodeId ?? null);
    setNodeTypeFilter(hasMixedNodes ? "all" : "paper");
    setViewMode(graphForDisplay.defaultViewMode ?? "graph");
    setListCollapsed((graphForDisplay.defaultViewMode ?? "graph") === "graph");
    setGraphWarning(graphForDisplay.warnings?.length ? graphForDisplay.warnings.join(" ") : null);
    setGlobalSemanticGraph(options.globalSemanticGraph ?? null);
    setGlobalSemanticBaseState(null);
  }, []);

  const restoreGraphState = useCallback(async (graphId: string) => {
    if (!graphId || isGraphLoading) return;

    setGraphHistory((history) => [...history, rememberCurrentGraph()]);
    setSelectedNodeId(null);
    setIsGraphLoading(true);
    setGraphWarning(null);
    try {
      const nextGraph = await getGraphState(accessToken, detail.workspace.id, graphId);
      applyGraphState(nextGraph, "Saved graph");
    } catch (error) {
      setGraphWarning(error instanceof Error ? error.message : "Failed to restore saved graph");
    } finally {
      setIsGraphLoading(false);
    }
  }, [accessToken, applyGraphState, detail.workspace.id, isGraphLoading, rememberCurrentGraph]);

  const applyEvidenceGraph = useCallback((nextGraph: GraphResponse, sourceRunId: string) => {
    const fallbackTitle = nextGraph.title ?? `Evidence graph ${sourceRunId.slice(0, 8)}`;
    const graphWithContext = {
      ...nextGraph,
      title: nextGraph.title ?? fallbackTitle,
      queryContext: nextGraph.queryContext ?? fallbackTitle,
      workspaceId: nextGraph.workspaceId ?? detail.workspace.id,
    };

    setGraphHistory((history) => [...history, rememberCurrentGraph()]);
    applyGraphState(graphWithContext, fallbackTitle);

    if (graphWithContext.graphId) {
      setGraphStates((current) => current.some((state) => state.id === graphWithContext.graphId)
        ? current
        : [{
          id: graphWithContext.graphId ?? "",
          title: graphWithContext.title ?? fallbackTitle,
          queryContext: graphWithContext.queryContext ?? fallbackTitle,
          seedNodeId: graphWithContext.seedNodeId ?? null,
          createdAt: new Date().toISOString(),
        }, ...current]);
    }
  }, [applyGraphState, detail.workspace.id, rememberCurrentGraph]);

  const exploreFromPaper = useCallback(async (mode: ExploreMode, paperId?: string) => {
    const sourcePaper = paperId
      ? nodes.find((node): node is PaperNode => node.nodeType === "paper" && (
        node.id === paperId
        || paperIdentityKey(paperCanonicalId(node)) === paperIdentityKey(paperId)
      )) ?? null
      : selectedPaper;
    if (!sourcePaper || isGraphLoading) return;
    // id belongs to the rendered graph. Every paper-level backend operation
    // must use the canonical identity stored on the paper node instead.
    const sourcePaperId = paperCanonicalId(sourcePaper);
    const previousGraphState = rememberCurrentGraph();
    setSelectedNodeId(null);
    setIsGraphLoading(true);
    setExploringMode(mode);
    setGraphWarning(null);

    const queryType = mode === "refs"
      ? "paper_references"
      : mode === "cited_by"
        ? "paper_citations"
        : mode === "connections"
          ? "paper_connections"
          : mode === "semantic_graph"
            ? "paper_semantic_graph"
            : "paper_similar";
    const includeConnections = mode === "connections";

    try {
      const nextGraph = await queryGraph(accessToken, mode === "semantic_graph" ? {
        queryType,
        workspaceId: detail.workspace.id,
        seedPaperId: sourcePaperId,
        limit: 50,
      } : {
        queryType,
        workspaceId: detail.workspace.id,
        seedPaperId: sourcePaperId,
        query: sourcePaper.title,
        limit: mode === "connections" ? 5 : mode === "similar" ? 10 : 50,
        includeReferences: true,
        includeCitations: true,
        includeAuthors: includeConnections,
        includeInstitutions: includeConnections,
        includeFields: false,
      });
      const nextContext = nextGraph.title ?? nextGraph.queryContext ?? `${modeLabel(mode)}: ${paperLabel(sourcePaper)}`;
      const responseSeedPaperId = nextGraph.seedPaperId ?? sourcePaperId;
      const responseSeedNodeId = nextGraph.seedNodeId && nextGraph.nodes.some((node) => node.id === nextGraph.seedNodeId)
        ? nextGraph.seedNodeId
        : paperNodeIdForPaperId(nextGraph.nodes, responseSeedPaperId);
      const graphWithSeed = {
        ...nextGraph,
        title: nextGraph.title ?? nextContext,
        queryContext: nextGraph.queryContext ?? nextContext,
        seedPaperId: responseSeedPaperId,
        seedNodeId: responseSeedNodeId,
      };

      setGraphHistory((history) => [...history, previousGraphState]);
      applyGraphState(graphWithSeed, nextContext, { semanticGraph: mode === "semantic_graph" });

      if (graphWithSeed.graphId) {
        setGraphStates((current) => current.some((state) => state.id === graphWithSeed.graphId)
          ? current
          : [{
            id: graphWithSeed.graphId ?? "",
            title: graphWithSeed.title ?? nextContext,
            queryContext: graphWithSeed.queryContext ?? nextContext,
            seedNodeId: graphWithSeed.seedNodeId ?? null,
            createdAt: new Date().toISOString(),
          }, ...current]);
      } else {
        void saveGraphState(accessToken, detail.workspace.id, graphWithSeed)
          .then((saved) => {
            setGraphStates((current) => [{
              id: saved.graphId,
              title: graphWithSeed.title ?? nextContext,
              queryContext: graphWithSeed.queryContext ?? nextContext,
              seedNodeId: graphWithSeed.seedNodeId ?? null,
              createdAt: new Date().toISOString(),
            }, ...current]);
          })
          .catch(() => undefined);
      }
    } catch (error) {
      const ids = getExplorationIds(sourcePaper.id, mode, edges);
      setGraphHistory((history) => [...history, previousGraphState]);
      setGraphData(graphData);
      setFilteredIds(ids);
      setNodeTypeFilter("paper");
      setViewMode("graph");
      setListCollapsed(true);
      setQueryContext(`${modeLabel(mode)}: ${paperLabel(sourcePaper)}`);
      setGraphWarning(error instanceof Error
        ? `${error.message}; showing local fallback graph`
        : "Graph request failed; showing local fallback graph");
    } finally {
      setIsGraphLoading(false);
      setExploringMode(null);
    }
  }, [accessToken, applyGraphState, detail.workspace.id, edges, graphData, isGraphLoading, rememberCurrentGraph, nodes, selectedPaper]);
  const expandSemanticEntityGlobally = useCallback(async (entity: SemanticEntityNode) => {
    const seedPaperNode = nodes.find((node): node is PaperNode => node.nodeType === "paper" && (
      !graphData.seedPaperId
      || paperIdentityKey(paperCanonicalId(node)) === paperIdentityKey(graphData.seedPaperId)
    )) ?? nodes.find((node): node is PaperNode => node.nodeType === "paper");
    const paperId = seedPaperNode ? paperCanonicalId(seedPaperNode) : graphData.seedPaperId;
    const dataset = globalSemanticDatasetForPaperId(paperId);
    if (!paperId || !dataset || isGraphLoading) return;

    // Keep the established local entity ID request contract. The backend also
    // accepts the composite node ID; use it only when no bare entity ID exists.
    const localEntityId = entity.entityId
      ?? graphDataString(entity.data, "entityId", "entity_id")
      ?? entity.id;
    const previousGraphState = rememberCurrentGraph();

    setIsGraphLoading(true);
    setGraphWarning(null);

    try {
      const nextGraph = await expandGlobalSemanticGraph(accessToken, {
        seedPaperId: paperId,
        seedEntityId: localEntityId,
        dataset,
        minPaperCount: 2,
        edgeMinWeight: 2,
        edgeTypes: ["co_occurrence", "semantic_relation"],
        depth: 1,
        limit: 100,
        evidenceLimit: 5,
      });

      const displaySelection = selectExpansionForDisplay(nextGraph);
      const displayGraph = displaySelection.expansion;
      const hasGlobalEntity = displayGraph.nodes.some((node) => node.nodeType === "global_semantic_entity");
      const hasExpansionEdge = displayGraph.edges.some((edge) => edge.type !== "local_to_global");
      if (!hasGlobalEntity || !hasExpansionEdge) {
        setGraphWarning("No global expansion found");
        return;
      }

      const merged = mergeGlobalSemanticExpansion(graphData, displayGraph, entity.id);
      const laidOutNodes = layoutExpandedSemanticGraphNodes(
        merged.graph.nodes,
        merged.graph.edges,
        merged.localSeedId,
        merged.globalSeedId,
        merged.addedNodeIds,
      );
      const laidOutPapers = laidOutNodes.filter((node): node is PaperNode => node.nodeType === "paper");
      const mergedPapers = new Map<string, Paper>();
      merged.graph.papers.forEach((paper) => mergedPapers.set(paperCanonicalId(paper).toLowerCase(), paper));
      laidOutPapers.forEach((paper) => mergedPapers.set(paperCanonicalId(paper).toLowerCase(), paper));
      const mergedGraph: GraphResponse = {
        ...merged.graph,
        nodes: laidOutNodes,
        papers: [...mergedPapers.values()],
      };
      const seedEntity = merged.graph.nodes.find((node) =>
        node.nodeType === "global_semantic_entity" && node.id === merged.globalSeedId,
      ) ?? displayGraph.nodes.find((node) => node.nodeType === "global_semantic_entity");
      const seedEntityName = seedEntity?.nodeType === "global_semantic_entity"
        ? seedEntity.canonicalName
        : nextGraph.seed.canonicalName || entity.entityName || entity.label;
      const expansionStats: GlobalSemanticGraphStats = {
        dataset: nextGraph.stats.dataset,
        minPaperCount: nextGraph.stats.minPaperCount,
        edgeMinWeight: nextGraph.stats.edgeMinWeight,
        depth: nextGraph.stats.depth,
        returnedNodeCount: nextGraph.stats.nodeCount,
        returnedEdgeCount: nextGraph.stats.edgeCount,
        availableNeighborCount: nextGraph.stats.availableNeighborCount,
        truncated: nextGraph.stats.truncated,
        buildId: nextGraph.stats.buildId,
        builtAt: nextGraph.stats.builtAt,
      };

      setGlobalSemanticBaseState((base) => base ?? previousGraphState);
      setGraphData(mergedGraph);
      setFilteredIds(null);
      setNodeTypeFilter("all");
      setSelectedNodeId(merged.globalSeedId ?? seedEntity?.id ?? null);
      setGraphWarning(nextGraph.warnings.length ? nextGraph.warnings.join(" ") : null);
      setGlobalSemanticGraph({
        seedEntityName,
        stats: expansionStats,
        addedNodeCount: merged.addedNodeIds.size,
        addedEdgeCount: merged.addedEdgeIds.size,
        topConnectionsOnly: displaySelection.limited,
        displayedNodeCount: displayGraph.nodes.length,
        displayedEdgeCount: displayGraph.edges.length,
        evidenceLimit: Math.max(1, Math.min(GLOBAL_EXPANSION_EVIDENCE_LIMIT, nextGraph.stats.evidenceLimit || GLOBAL_EXPANSION_EVIDENCE_LIMIT)),
      });
    } catch (error) {
      const message = error instanceof Error ? error.message.toLowerCase() : "";
      setGraphWarning(message.includes("not found") || message.includes("no global")
        ? "No global expansion found"
        : "Unable to load global connections. The paper graph is unchanged.");
    } finally {
      setIsGraphLoading(false);
    }
  }, [accessToken, expandGlobalSemanticGraph, graphData, isGraphLoading, nodes, rememberCurrentGraph]);

  return (
    <div className="app-shell">
      <TopNav displayName={displayName} onHome={onHome} />

      <main className="workspace">
        <section className="exploration-surface">
          <SearchContextBar
            context={queryContext}
            resultCount={visibleNodes.length}
            totalCount={nodes.length}
            nodeTypeFilter={nodeTypeFilter}
            viewMode={viewMode}
            listCollapsed={effectiveListCollapsed}
            canGoBack={graphHistory.length > 0 || Boolean(globalSemanticBaseState)}
            showNodeTypeFilter={isConnectionsGraph && hasMixedRootNodes}
            graphStates={graphStates}
            activeGraphId={graphData.graphId ?? null}
            onGraphStateSelect={restoreGraphState}
            onBack={returnToPreviousGraph}
            onNodeTypeFilterChange={changeNodeTypeFilter}
            onToggleList={effectiveListCollapsed ? expandPaperList : collapsePaperList}
            onViewModeChange={changeViewMode}
          />

          {globalSemanticGraph ? (
            <div className="global-graph-status" role="status">
              <button className="global-graph-back" type="button" onClick={resetGlobalExpansion}>
                <ArrowLeft size={16} />
                Reset Expansion
              </button>
              <div className="global-graph-summary">
                <strong>{globalSemanticGraph.stats.dataset.toUpperCase()} Global Expansion</strong>
                <span>Seed: {globalSemanticGraph.seedEntityName}</span>
              </div>
              <div className="global-graph-stats">
                <span>+{globalSemanticGraph.addedNodeCount.toLocaleString()} nodes</span>
                <span>+{globalSemanticGraph.addedEdgeCount.toLocaleString()} edges</span>
                <span>Truncated: {globalSemanticGraph.stats.truncated ? "yes" : "no"}</span>
                <span>{globalSemanticGraph.topConnectionsOnly ? "Top connections" : "Returned connections"}</span>
              </div>
              {globalSemanticGraph.topConnectionsOnly ? (
                <p>
                  Showing Top connections ranked by paper count and edge weight; evidence papers are capped at {globalSemanticGraph.evidenceLimit}.
                </p>
              ) : null}
            </div>
          ) : null}

          {isGraphLoading || graphWarning ? (
            <div className={`graph-status ${globalSemanticGraph ? "global-offset" : ""} ${graphWarning ? "warning" : ""}`}>
              {isGraphLoading ? (
                <>
                  <Loader2 className="spin" size={15} />
                  Loading graph from backend...
                </>
              ) : graphWarning}
            </div>
          ) : null}

          <button className="workspace-home-button" type="button" onClick={onHome} aria-label="Back to home">
            <Home size={17} />
          </button>

          <div className={`workspace-grid mode-${viewMode} ${effectiveListCollapsed ? "list-collapsed" : ""}`}>
            {viewMode !== "graph" && !effectiveListCollapsed ? (
              <PaperList
                nodes={visibleNodes}
                selectedNodeId={selectedNodeId}
                nodeTypeFilter={nodeTypeFilter}
                onSelectNode={selectNode}
                onCollapse={viewMode === "default" ? collapsePaperList : undefined}
              />
            ) : null}

            {viewMode !== "list" ? (
              <GraphCanvas
                nodes={visibleNodes}
                edges={visibleEdges}
                selectedNodeId={selectedNodeId}
                selectedEdgeId={selectedEdgeId}
                onSelectNode={selectNode}
                onSelectEdge={selectEdge}
                isEvidenceGraph={
                  queryContext === "Agent report evidence graph"
                  || graphData.queryContext === "Agent report evidence graph"
                  || graphData.title?.startsWith("Evidence Graph") === true
                }
              />
            ) : null}

            {viewMode === "graph" && effectiveListCollapsed ? (
              <button className="paper-dock-button" type="button" onClick={expandPaperList}>
                Nodes {visibleNodes.length}
              </button>
            ) : null}
          </div>

          {selectedNode || selectedEdge ? (
            <div className="floating-detail">
              <PaperDetailPanel
                node={selectedNode}
                edge={selectedEdge}
                edgeSourceNode={selectedEdgeSourceNode}
                edgeTargetNode={selectedEdgeTargetNode}
                seedLabel={seedPaper ? paperLabel(seedPaper) : detail.workspace.title}
                onClose={clearSelection}
                onExplore={exploreFromPaper}
                onExpandGlobal={expandSemanticEntityGlobally}
                globalDataset={globalDataset}
                isExploring={isGraphLoading}
                exploringMode={exploringMode}
              />
            </div>
          ) : null}
        </section>

        <AgentChatPanel
          workspaceId={detail.workspace.id}
          accessToken={accessToken}
          initialConversationId={detail.workspace.conversationId}
          initialMessages={detail.initialChatMessages}
          onConversationResolved={(conversationId) => onConversationResolved(detail.workspace.id, conversationId)}
          onApplyEvidenceGraph={applyEvidenceGraph}
        />
      </main>
    </div>
  );
}

function App() {
  const [route, setRoute] = useState<RouteState>(() => parseRoute());
  const [session, setSession] = useState<AuthSession | null>(() => loadStoredSession());
  const [authChecked, setAuthChecked] = useState(false);
  const [workspaceCache, setWorkspaceCache] = useState<Record<string, WorkspaceDetail>>({});
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);

  useEffect(() => {
    const handlePopState = () => setRoute(parseRoute());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    const storedSession = loadStoredSession();
    if (!storedSession) {
      setAuthChecked(true);
      if (route.name === "workspace" && route.workspaceId === "local-root") return;
      if (route.name !== "login") pushRoute("/login", setRoute);
      return;
    }

    void getCurrentUser(storedSession.accessToken)
      .then(({ user }) => {
        setSession({ ...storedSession, user });
        if (route.name === "login") pushRoute("/home", setRoute);
      })
      .catch(() => {
        setSession(null);
        pushRoute("/login", setRoute);
      })
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    if (!authChecked || !session || route.name !== "workspace") return;
    if (workspaceCache[route.workspaceId]) return;

    setWorkspaceError(null);
    void getWorkspace(session.accessToken, route.workspaceId)
      .then((detail) => {
        setWorkspaceCache((current) => ({ ...current, [detail.workspace.id]: detail }));
      })
      .catch((error) => {
        setWorkspaceError(error instanceof Error ? error.message : "Failed to load workspace");
      });
  }, [authChecked, route, session, workspaceCache]);

  const navigateHome = () => pushRoute("/home", setRoute);

  if (!authChecked) {
    return <main className="loading-page">Loading BigDipper Scholar...</main>;
  }

  if (import.meta.env.VITE_ENABLE_MOCK === "true" && !session && route.name === "workspace" && route.workspaceId === "local-root") {
    return (
      <WorkspaceView
        detail={directMockWorkspace()}
        displayName="Demo User"
        accessToken="demo-token"
        onHome={() => pushRoute("/login", setRoute)}
        onConversationResolved={() => undefined}
      />
    );
  }

  if (!session) {
    return (
      <LoginPage
        onAuthenticated={(nextSession) => {
          setSession(nextSession);
          pushRoute("/home", setRoute);
        }}
      />
    );
  }

  if (route.name === "home" || route.name === "login") {
    return (
      <HomePage
        session={session}
        onLogout={() => {
          setSession(null);
          setWorkspaceCache({});
          pushRoute("/login", setRoute);
        }}
        onOpenWorkspace={(workspaceId) => {
          setWorkspaceCache((current) => {
            const next = { ...current };
            delete next[workspaceId];
            return next;
          });
          pushRoute(`/workspace/${encodeURIComponent(workspaceId)}`, setRoute);
        }}
        onWorkspaceCreated={(detail) => {
          setWorkspaceCache((current) => ({ ...current, [detail.workspace.id]: detail }));
          pushRoute(`/workspace/${encodeURIComponent(detail.workspace.id)}`, setRoute);
        }}
      />
    );
  }

  const detail = workspaceCache[route.workspaceId] ?? (route.workspaceId === "local-root" ? directMockWorkspace() : null);

  if (!detail) {
    return (
      <main className="loading-page">
        {workspaceError ? (
          <div>
            <p>{workspaceError}</p>
            <button type="button" onClick={navigateHome}>Back home</button>
          </div>
        ) : "Loading workspace..."}
      </main>
    );
  }

  return (
    <WorkspaceView
      key={detail.workspace.id}
      detail={detail}
      displayName={session.user.displayName}
      accessToken={session.accessToken}
      onHome={navigateHome}
      onConversationResolved={(workspaceId, conversationId) => {
        setWorkspaceCache((current) => {
          const cached = current[workspaceId];
          if (!cached || cached.workspace.conversationId === conversationId) return current;
          return {
            ...current,
            [workspaceId]: {
              ...cached,
              workspace: { ...cached.workspace, conversationId },
            },
          };
        });
      }}
    />
  );
}

export default App;
