import cytoscape, { type Core, type EventObject, type StylesheetJson } from "cytoscape";
import { Focus, LocateFixed, MousePointer2, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphEdge, GraphNode } from "../types";
import {
  getSemanticEntityVisual,
  semanticEntityIconDataUri,
  semanticEntityType,
  semanticEntityTypeLabel,
} from "../utils/semanticEntityStyle";
import { paperCanonicalId, shortVenue } from "../utils/graph";

type GraphCanvasProps = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selectedNodeId: string | null;
  selectedEdgeId?: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onSelectEdge?: (edgeId: string | null) => void;
  isEvidenceGraph?: boolean;
};

type EvidenceGraphMode = "papers" | "evidence";

type EvidenceNodeVisibility = {
  authors: boolean;
  institutions: boolean;
  tools: boolean;
};

const PAPER_EDGE_TYPES = new Set<GraphEdge["type"]>([
  "reference",
  "cites",
  "cited_by",
  "similar",
]);

function edgePairKey(source: string, target: string) {
  return [source, target].sort().join("::");
}

function paperGraphLayout(nodes: GraphNode[], edges: GraphEdge[], seedId?: string | null) {
  const papers = nodes.filter((node) => node.nodeType === "paper");
  const paperIds = new Set(papers.map((node) => node.id));
  const adjacency = new Map<string, Set<string>>();
  papers.forEach((node) => adjacency.set(node.id, new Set()));
  edges.forEach((edge) => {
    if (!paperIds.has(edge.source) || !paperIds.has(edge.target)) return;
    adjacency.get(edge.source)?.add(edge.target);
    adjacency.get(edge.target)?.add(edge.source);
  });

  const chosenSeed = (seedId && paperIds.has(seedId) ? seedId : papers[0]?.id) ?? "";
  const distance = new Map<string, number>();
  if (chosenSeed) {
    distance.set(chosenSeed, 0);
    const queue = [chosenSeed];
    while (queue.length) {
      const current = queue.shift()!;
      const nextDistance = (distance.get(current) ?? 0) + 1;
      adjacency.get(current)?.forEach((neighbor) => {
        if (distance.has(neighbor)) return;
        distance.set(neighbor, nextDistance);
        queue.push(neighbor);
      });
    }
  }
  papers.forEach((node, index) => {
    if (!distance.has(node.id)) distance.set(node.id, 3 + Math.floor(index / 12));
  });

  const positions = new Map<string, { x: number; y: number }>();
  const center = { x: 690, y: 460 };
  if (chosenSeed) positions.set(chosenSeed, center);
  const rings = new Map<number, GraphNode[]>();
  papers.forEach((node) => {
    const ring = distance.get(node.id) ?? 3;
    if (ring === 0) return;
    const group = rings.get(ring) ?? [];
    group.push(node);
    rings.set(ring, group);
  });
  [...rings.entries()].sort(([a], [b]) => a - b).forEach(([ring, group]) => {
    const radius = 230 + (ring - 1) * 185;
    const start = -Math.PI / 2 + (ring % 2 ? 0.16 : -0.12);
    group.forEach((node, index) => {
      const angle = start + (index / Math.max(group.length, 1)) * Math.PI * 2;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius,
      });
    });
  });
  return positions;
}

function buildPaperDisplayModel(
  nodes: GraphNode[],
  edges: GraphEdge[],
  mode: EvidenceGraphMode,
  selectedNodeId?: string | null,
  visibility: EvidenceNodeVisibility = { authors: true, institutions: true, tools: true },
) {
  const papers = nodes.filter((node) => node.nodeType === "paper");
  const paperIds = new Set(papers.map((node) => node.id));
  const paperEdges = edges.filter((edge) =>
    paperIds.has(edge.source) && paperIds.has(edge.target) && PAPER_EDGE_TYPES.has(edge.type),
  );
  const pairKeys = new Set(paperEdges.map((edge) => edgePairKey(edge.source, edge.target)));
  const displayEdges = [...paperEdges];

  // Preserve common-author evidence as a paper-to-paper relationship in the
  // papers-only view. The author itself is shown in the expanded view.
  const authorPapers = new Map<string, string[]>();
  edges.forEach((edge) => {
    if (edge.type !== "authored_by") return;
    if (!paperIds.has(edge.source)) return;
    const list = authorPapers.get(edge.target) ?? [];
    list.push(edge.source);
    authorPapers.set(edge.target, list);
  });
  authorPapers.forEach((linkedPapers, authorId) => {
    for (let i = 0; i < linkedPapers.length; i += 1) {
      for (let j = i + 1; j < linkedPapers.length; j += 1) {
        const source = linkedPapers[i];
        const target = linkedPapers[j];
        const key = edgePairKey(source, target);
        if (pairKeys.has(key)) continue;
        pairKeys.add(key);
        displayEdges.push({
          id: `display-coauthor-${source}-${target}-${authorId}`,
          source,
          target,
          type: "coauthored_with",
          label: "共同作者",
          relationName: "共同作者",
          data: { derivedFrom: "authored_by", sharedAuthorId: authorId },
        });
      }
    }
  });

  const paperPositions = paperGraphLayout(papers, displayEdges, selectedNodeId);
  if (mode === "papers") {
    return {
      nodes: papers.map((node) => ({ ...node, position: paperPositions.get(node.id) ?? node.position })),
      edges: displayEdges,
      positions: paperPositions,
    };
  }

  const entityNodes = nodes.filter((node) => {
    if (node.nodeType === "author") return visibility.authors;
    if (node.nodeType === "institution") return visibility.institutions;
    if (node.nodeType === "field") {
      return visibility.tools;
    }
    return false;
  });
  const displayNodes = [...papers, ...entityNodes];
  const displayNodeIds = new Set(displayNodes.map((node) => node.id));
  const displayNodeEdges = edges.filter((edge) => displayNodeIds.has(edge.source) && displayNodeIds.has(edge.target));
  const edgeKeys = new Set(displayNodeEdges.map((edge) => `${edge.id}:${edge.source}:${edge.target}`));
  displayEdges.forEach((edge) => {
    const key = `${edge.id}:${edge.source}:${edge.target}`;
    if (!edgeKeys.has(key)) {
      displayNodeEdges.push(edge);
      edgeKeys.add(key);
    }
  });

  const positions = new Map(paperPositions);
  const entitiesByType = new Map<string, GraphNode[]>();
  entityNodes.forEach((node) => {
    const group = entitiesByType.get(node.nodeType) ?? [];
    group.push(node);
    entitiesByType.set(node.nodeType, group);
  });
  const radii: Record<string, number> = { author: 125, institution: 215, field: 300, semantic_entity: 280, global_semantic_entity: 320, unknown: 350 };
  entitiesByType.forEach((group, type) => {
    group.forEach((node, index) => {
      const linkedPaper = displayNodeEdges.find((edge) =>
        (edge.source === node.id && paperIds.has(edge.target)) || (edge.target === node.id && paperIds.has(edge.source)),
      );
      const anchorId = linkedPaper?.source === node.id ? linkedPaper.target : linkedPaper?.source;
      const linkedPosition = anchorId ? positions.get(anchorId) : undefined;
      const anchor = linkedPosition ?? { x: 690, y: 460 };
      const angle = -Math.PI / 2 + (index / Math.max(group.length, 1)) * Math.PI * 2;
      const radius = radii[type] ?? 270;
      positions.set(node.id, { x: anchor.x + Math.cos(angle) * radius, y: anchor.y + Math.sin(angle) * radius });
    });
  });

  return {
    nodes: displayNodes.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position })),
    edges: displayNodeEdges,
    positions,
  };
}

function graphDisplayLabel(node: GraphNode) {
  if (node.nodeType === "paper") {
    const dataDisplayName = typeof node.data?.displayName === "string" ? node.data.displayName.trim() : "";
    const title = node.title || dataDisplayName || node.label;
    const subtitle = [node.year ? String(node.year) : undefined, node.venue ? shortVenue(node.venue) : undefined, paperCanonicalId(node)]
      .filter(Boolean)
      .join(" · ");
    return subtitle ? `${title}\n${subtitle}` : title;
  }

  if (node.nodeType === "global_semantic_entity") {
    const subtitle = `${node.entityType ?? "Unknown type"} · ${node.paperCount.toLocaleString()} papers`;
    return `${node.canonicalName}\n${subtitle}`;
  }

  if (node.nodeType === "semantic_entity") {
    const entityType = semanticEntityType(node);
    return entityType ? `${node.label}\n${entityType}` : node.label;
  }

  if (node.nodeType === "field" && node.data?.displayRole !== "evidence-source") {
    const toolName = typeof node.data?.toolName === "string" ? node.data.toolName.trim() : "";
    if (toolName) return toolName;
  }

  return node.label;
}

function graphEdgeWidth(edge: GraphEdge) {
  const weight = edge.weight ?? edge.paperCount ?? 0;
  if (!weight) return edge.type === "semantic_relation" ? 2.2 : 1.7;
  return Math.min(7, 1.4 + Math.log2(weight + 1) * 0.72);
}

const graphStyle: StylesheetJson = [
  {
    selector: "node",
    style: {
      label: "data(label)",
      "text-valign": "bottom",
      "text-halign": "center",
      "text-margin-y": 8,
      "font-size": 14,
      "font-weight": 500,
      color: "#2b3035",
      "background-color": "#f9fafb",
      "border-width": 3,
      "border-color": "#121417",
      width: "mapData(metric, 0, 150000, 42, 78)",
      height: "mapData(metric, 0, 150000, 42, 78)",
      "overlay-padding": 12,
      "transition-property": "background-color, border-color, opacity, line-color, target-arrow-color",
      "transition-duration": 160,
    },
  },
  {
    selector: "node[nodeType = 'paper']",
    style: {
      shape: "ellipse",
      "background-color": "#eef6ff",
      label: "data(displayLabel)",
      "text-wrap": "wrap",
      "text-max-width": "178px",
      "font-size": 11,
      "font-weight": 600,
    },
  },
  {
    selector: "node[nodeType = 'author']",
    style: {
      shape: "round-rectangle",
      "background-color": "#f3fbf7",
    },
  },
  {
    selector: "node[nodeType = 'institution']",
    style: {
      shape: "hexagon",
      "background-color": "#fff7e6",
    },
  },
  {
    selector: "node[nodeType = 'field']",
    style: {
      shape: "diamond",
      "background-color": "#f5f3ff",
      "border-color": "#7c3aed",
    },
  },
  {
    selector: "node[nodeType = 'semantic_entity'], node[nodeType = 'global_semantic_entity']",
    style: {
      shape: "ellipse",
      width: 56,
      height: 56,
      "background-color": "data(entityColor)",
      "background-image": "data(entityIcon)",
      "background-fit": "none",
      "background-width": "27px",
      "background-height": "27px",
      "background-position-x": "50%",
      "background-position-y": "50%",
      "border-color": "data(entityBorderColor)",
      label: "data(displayLabel)",
      "text-wrap": "wrap",
      "text-max-width": "140px",
      "font-weight": 650,
    },
  },
  {
    selector: "node[nodeType = 'global_semantic_entity']",
    style: {
      width: "data(nodeSize)",
      height: "data(nodeSize)",
      "text-max-width": "180px",
      "font-size": 12,
      "text-margin-y": 10,
    },
  },
  {
    selector: "node[nodeType = 'unknown']",
    style: {
      shape: "rectangle",
      "background-color": "#f8fafc",
      "border-style": "dashed",
      "border-color": "#64748b",
    },
  },
  {
    selector: "edge",
    style: {
      label: "data(edgeLabel)",
      width: 1.7,
      "curve-style": "bezier",
      "target-arrow-shape": "triangle",
      "line-color": "#b8c3d1",
      "target-arrow-color": "#b8c3d1",
      "arrow-scale": 1.1,
      opacity: 0.86,
      color: "#526071",
      "font-size": 10,
      "font-weight": 600,
      "text-rotation": "autorotate",
      "text-margin-y": -7,
      "text-background-color": "#ffffff",
      "text-background-opacity": 0.9,
      "text-background-padding": "3px",
      "text-border-opacity": 0,
      "transition-property": "opacity, line-color, target-arrow-color, width",
      "transition-duration": 160,
    },
  },
  {
    selector: "edge[type = 'reference'], edge[type = 'cites']",
    style: {
      width: 3,
      "line-color": "#2f83ff",
      "target-arrow-color": "#2f83ff",
      "z-index": 8,
    },
  },
  {
    selector: "edge[type = 'cited_by']",
    style: {
      width: 2.6,
      "line-color": "#2563eb",
      "target-arrow-color": "#2563eb",
      "z-index": 7,
    },
  },
  {
    selector: "edge[type = 'similar']",
    style: {
      "line-style": "dashed",
      "line-color": "#a9b6c5",
      "target-arrow-color": "#a9b6c5",
    },
  },
  {
    selector: "edge[type = 'authored_by']",
    style: {
      "line-color": "#65a30d",
      "target-arrow-color": "#65a30d",
    },
  },
  {
    selector: "edge[type = 'affiliated_with']",
    style: {
      "line-style": "dotted",
      "line-color": "#d97706",
      "target-arrow-color": "#d97706",
    },
  },
  {
    selector: "edge[type = 'coauthored_with']",
    style: {
      "line-style": "dashed",
      "line-color": "#7c3aed",
      "target-arrow-color": "#7c3aed",
    },
  },
  {
    selector: "edge[type = 'institution_collaboration']",
    style: {
      width: 2.4,
      "line-color": "#0891b2",
      "target-arrow-color": "#0891b2",
    },
  },
  {
    selector: "edge[type = 'has_field'], edge[type = 'published_in']",
    style: {
      "line-style": "dotted",
      "line-color": "#64748b",
      "target-arrow-color": "#64748b",
    },
  },
  {
    selector: "edge[type = 'co_occurrence']",
    style: {
      width: "data(edgeWidth)",
      "target-arrow-shape": "none",
      "line-color": "#64748b",
      "target-arrow-color": "#64748b",
    },
  },
  {
    selector: "edge[type = 'local_to_global']",
    style: {
      width: 2.8,
      "line-color": "#7c3aed",
      "target-arrow-color": "#7c3aed",
      "line-style": "dashed",
      "z-index": 9,
    },
  },
  {
    selector: "edge[type = 'entity_mentioned_in_paper']",
    style: {
      width: 2.2,
      "line-color": "#c2410c",
      "target-arrow-color": "#c2410c",
      "line-style": "dotted",
      "z-index": 8,
    },
  },
  {
    selector: "edge[type = 'semantic_relation']",
    style: {
      "line-color": "#0f766e",
      "target-arrow-color": "#0f766e",
      "line-style": "dashed",
      width: "data(edgeWidth)",
    },
  },
  {
    selector: "edge[type = 'co_occurrence']",
    style: {
      label: "",
    },
  },
  {
    selector: "edge[extensionEdge = 'true']",
    style: {
      opacity: 0.3,
      label: "",
    },
  },
  {
    selector: ".faded",
    style: {
      opacity: 0.14,
    },
  },
  {
    selector: ".neighbor",
    style: {
      opacity: 1,
      "border-color": "#111827",
    },
  },
  {
    selector: ".selected",
    style: {
      "background-color": "#93c5fd",
      "border-color": "#1f7aff",
      "border-width": 5,
      opacity: 1,
    },
  },
  {
    selector: "node[nodeType = 'semantic_entity'].selected, node[nodeType = 'global_semantic_entity'].selected",
    style: {
      "background-color": "data(entityColor)",
      "border-color": "#1f7aff",
    },
  },
  {
    selector: ".active-edge",
    style: {
      width: 3.2,
      "line-color": "#1f7aff",
      "target-arrow-color": "#1f7aff",
      opacity: 1,
      label: "data(fullEdgeLabel)",
    },
  },
  {
    selector: ".selected-edge",
    style: {
      width: 5,
      "line-color": "#1f7aff",
      "target-arrow-color": "#1f7aff",
      opacity: 1,
      label: "data(fullEdgeLabel)",
      "z-index": 12,
    },
  },
];

export function GraphCanvas({
  nodes,
  edges,
  selectedNodeId,
  selectedEdgeId = null,
  onSelectNode,
  onSelectEdge,
  isEvidenceGraph = false,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const [evidenceMode, setEvidenceMode] = useState<EvidenceGraphMode>("papers");
  const [nodeVisibility, setNodeVisibility] = useState<EvidenceNodeVisibility>({
    authors: true,
    institutions: true,
    tools: true,
  });

  useEffect(() => {
    if (!isEvidenceGraph) {
      setEvidenceMode("papers");
      setNodeVisibility({ authors: true, institutions: true, tools: true });
    }
  }, [isEvidenceGraph]);

  const displayModel = useMemo(
    () => isEvidenceGraph
      ? buildPaperDisplayModel(nodes, edges, evidenceMode, selectedNodeId, nodeVisibility)
      : { nodes, edges, positions: new Map<string, { x: number; y: number }>() },
    [edges, evidenceMode, isEvidenceGraph, nodeVisibility, nodes, selectedNodeId],
  );
  const displayNodes = displayModel.nodes;
  const displayEdges = displayModel.edges;

  const semanticLegend = useMemo(() => {
    const visibleTypes = new Map<string, string>();

    displayNodes.forEach((node) => {
      if (node.nodeType !== "semantic_entity" && node.nodeType !== "global_semantic_entity") return;
      const label = semanticEntityTypeLabel(node);
      const key = label.toLowerCase();
      if (!visibleTypes.has(key)) visibleTypes.set(key, label);
    });

    return [...visibleTypes.values()].map((entityType) => ({
      entityType,
      visual: getSemanticEntityVisual(entityType === "Unknown" ? undefined : entityType),
    }));
  }, [displayNodes]);

  const elements = useMemo(
    () => {
      const nodeById = new Map(displayNodes.map((node) => [node.id, node]));
      const hasGlobalExpansion = displayNodes.some((node) => node.nodeType === "global_semantic_entity");
      const isExpansionEdge = (edge: GraphEdge) => edge.type === "local_to_global"
        || edge.type === "entity_mentioned_in_paper"
        || nodeById.get(edge.source)?.nodeType === "global_semantic_entity"
        || nodeById.get(edge.target)?.nodeType === "global_semantic_entity";
      const expansionPaperIds = new Set<string>();
      displayEdges.forEach((edge) => {
        if (edge.type !== "entity_mentioned_in_paper") return;
        const source = nodeById.get(edge.source);
        const target = nodeById.get(edge.target);
        if (source?.nodeType === "paper") expansionPaperIds.add(source.id);
        if (target?.nodeType === "paper") expansionPaperIds.add(target.id);
      });

      return [
        ...displayNodes.map((node) => {
          const entityType = node.nodeType === "semantic_entity" || node.nodeType === "global_semantic_entity"
            ? semanticEntityType(node)
            : undefined;
          const entityVisual = getSemanticEntityVisual(entityType);

          return {
            data: {
              id: node.id,
              label: node.label,
              displayLabel: graphDisplayLabel(node),
              metric: node.metric,
              nodeType: node.nodeType,
              displayRole: node.data?.displayRole,
              extensionNode: node.nodeType === "global_semantic_entity" || expansionPaperIds.has(node.id)
                ? "true"
                : "false",
              nodeSize: node.nodeType === "global_semantic_entity"
                ? Math.min(96, 58 + Math.log2(node.paperCount + 1) * 3)
                : 56,
              entityColor: entityVisual.color,
              entityBorderColor: entityVisual.borderColor,
              entityIcon: semanticEntityIconDataUri(entityVisual.Icon),
            },
            position: node.position,
            grabbable: true,
          };
        }),
        ...displayEdges
          .filter((edge) => edge.source !== edge.target)
          .map((edge) => ({
            data: {
              id: edge.id,
              source: edge.source,
              target: edge.target,
              type: edge.type,
              extensionEdge: isExpansionEdge(edge) ? "true" : "false",
              fullEdgeLabel: edge.relationName || edge.label || "",
              edgeLabel: edge.type === "co_occurrence" || (hasGlobalExpansion && edge.type === "semantic_relation" && evidenceMode === "papers") || isExpansionEdge(edge)
                ? ""
                : edge.relationName || edge.label || "",
              edgeWidth: graphEdgeWidth(edge),
            },
          })),
      ];
    },
    [displayEdges, displayNodes, evidenceMode, isEvidenceGraph],
  );


  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: graphStyle,
      layout: { name: "preset", fit: true, padding: 58 },
      minZoom: 0.42,
      maxZoom: 2.35,
      wheelSensitivity: 0.32,
      autoungrabify: false,
      userPanningEnabled: true,
      userZoomingEnabled: true,
      selectionType: "single",
    });

    cyRef.current = cy;

    const onNodeTap = (event: EventObject) => {
      onSelectNode(event.target.id());
    };

    const onEdgeTap = (event: EventObject) => {
      onSelectEdge?.(event.target.id());
    };

    const onCanvasTap = (event: EventObject) => {
      if (event.target === cy) {
        onSelectNode(null);
        onSelectEdge?.(null);
      }
    };

    const onDragFree = (event: EventObject) => {
      event.target.addClass("neighbor");
    };

    cy.on("tap", "node", onNodeTap);
    if (onSelectEdge) cy.on("tap", "edge", onEdgeTap);
    cy.on("tap", onCanvasTap);
    cy.on("dragfree", "node", onDragFree);

    const resizeObserver = new ResizeObserver(() => {
      cy.resize();
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, [elements, onSelectEdge, onSelectNode]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.elements().removeClass("faded neighbor selected active-edge selected-edge");

    if (selectedEdgeId) {
      const selectedEdge = cy.getElementById(selectedEdgeId);
      if (selectedEdge.nonempty() && selectedEdge.isEdge()) {
        const isExpansionSelection = selectedEdge.data("extensionEdge") === "true";
        if (isExpansionSelection) {
          cy.elements("[extensionNode = 'true'], edge[extensionEdge = 'true']").addClass("faded");
        } else {
          cy.elements().addClass("faded");
        }
        const edgeNeighborhood = selectedEdge.connectedNodes().closedNeighborhood();
        edgeNeighborhood.removeClass("faded").addClass("neighbor");
        selectedEdge.removeClass("faded").addClass("selected-edge");
        selectedEdge.connectedNodes().connectedEdges().addClass("active-edge");

        cy.animate({
          center: { eles: selectedEdge },
          duration: 260,
          easing: "ease-out-cubic",
        });
        return;
      }
    }

    if (!selectedNodeId) {
      return;
    }

    const selectedNode = cy.getElementById(selectedNodeId);
    if (selectedNode.empty()) return;

    if (selectedNode.data("extensionNode") === "true") {
      cy.elements("[extensionNode = 'true'], edge[extensionEdge = 'true']").addClass("faded");
    } else {
      cy.elements().addClass("faded");
    }

    const neighborhood = selectedNode.closedNeighborhood();
    neighborhood.removeClass("faded").addClass("neighbor");
    selectedNode.removeClass("neighbor").addClass("selected");
    selectedNode.connectedEdges().addClass("active-edge");

    cy.animate({
      center: { eles: selectedNode },
      duration: 260,
      easing: "ease-out-cubic",
    });
  }, [selectedEdgeId, selectedNodeId]);

  const fitGraph = () => {
    cyRef.current?.fit(undefined, 58);
  };

  const centerSelected = () => {
    const cy = cyRef.current;
    const selectedElementId = selectedNodeId ?? selectedEdgeId;
    if (!cy || !selectedElementId) {
      fitGraph();
      return;
    }

    cy.animate({
      center: { eles: cy.getElementById(selectedElementId) },
      zoom: 1.15,
      duration: 220,
    });
  };

  const clearSelection = () => {
    onSelectNode(null);
    onSelectEdge?.(null);
  };

  return (
    <section className="graph-panel" aria-label="Knowledge graph">
      <div className="graph-legends">
        {isEvidenceGraph ? (
          <div className="evidence-mode-switch" role="group" aria-label="Evidence graph display mode">
            <button
              type="button"
              className={evidenceMode === "papers" ? "active" : ""}
              onClick={() => setEvidenceMode("papers")}
            >
              论文图谱
            </button>
            <button
              type="button"
              className={evidenceMode === "evidence" ? "active" : ""}
              onClick={() => setEvidenceMode("evidence")}
            >
              多模证据子图
            </button>
          </div>
        ) : null}
        {isEvidenceGraph && evidenceMode === "evidence" ? (
          <div className="evidence-node-toggles" role="group" aria-label="Evidence graph node types">
            <span>显示节点：</span>
            <button
              type="button"
              className={nodeVisibility.authors ? "active" : ""}
              aria-pressed={nodeVisibility.authors}
              onClick={() => setNodeVisibility((current) => ({ ...current, authors: !current.authors }))}
            >
              作者
            </button>
            <button
              type="button"
              className={nodeVisibility.institutions ? "active" : ""}
              aria-pressed={nodeVisibility.institutions}
              onClick={() => setNodeVisibility((current) => ({ ...current, institutions: !current.institutions }))}
            >
              机构
            </button>
            <button
              type="button"
              className={nodeVisibility.tools ? "active" : ""}
              aria-pressed={nodeVisibility.tools}
              onClick={() => setNodeVisibility((current) => ({ ...current, tools: !current.tools }))}
            >
              查询工具
            </button>
          </div>
        ) : null}
        <div className="graph-legend">
          <span><MousePointer2 size={15} /> Drag nodes</span>
          <span className="legend-reference">References</span>
          <span className="legend-similar">Similar</span>
          <span className="legend-cited">Cited by</span>
          {evidenceMode === "evidence" && nodeVisibility.authors ? <span className="legend-authored">Authors</span> : null}
          {evidenceMode === "evidence" && nodeVisibility.institutions ? <span className="legend-affiliated">Institutions</span> : null}
          {evidenceMode === "evidence" && nodeVisibility.tools ? <span className="legend-mentioned">Evidence source</span> : null}
          {evidenceMode === "papers" ? <span className="legend-similar">Semantic similarity</span> : null}
          {nodes.some((node) => node.nodeType === "global_semantic_entity") ? (
            <>
              <span className="legend-local-global">Local → global</span>
              <span className="legend-mentioned">Mentions</span>
            </>
          ) : null}
        </div>
        {semanticLegend.length ? (
          <div className="semantic-entity-legend" aria-label="Semantic entity types in current graph">
            <strong>Entity types</strong>
            <div>
              {semanticLegend.map(({ entityType, visual }) => {
                const EntityIcon = visual.Icon;
                return (
                  <span key={entityType}>
                    <i style={{ backgroundColor: visual.color }}>
                      <EntityIcon aria-hidden="true" size={13} strokeWidth={2.2} />
                    </i>
                    {entityType}
                  </span>
                );
              })}
            </div>
          </div>
        ) : null}
      </div>

      <div ref={containerRef} className="graph-canvas" />


      <div className="graph-tools">
        <button className="tool-button" type="button" onClick={centerSelected} aria-label="Center selection">
          <LocateFixed size={18} />
        </button>
        <button className="tool-button" type="button" onClick={fitGraph} aria-label="Fit graph">
          <Focus size={18} />
        </button>
        <button className="tool-button" type="button" onClick={clearSelection} aria-label="Clear selection">
          <RotateCcw size={18} />
        </button>
      </div>
    </section>
  );
}
