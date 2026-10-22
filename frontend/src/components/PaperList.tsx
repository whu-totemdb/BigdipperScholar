import { Building2, Circle, CircleHelp, Dot, FileText, PanelLeftClose, Tags, UserRound } from "lucide-react";
import type { GraphNode, NodeTypeFilter } from "../types";
import { nodeMetricLabel, nodeSubtitle, nodeTitle, nodeTypeLabel } from "../utils/graph";

type PaperListProps = {
  nodes: GraphNode[];
  selectedNodeId: string | null;
  nodeTypeFilter: NodeTypeFilter;
  onSelectNode: (nodeId: string) => void;
  onCollapse?: () => void;
};

function NodeIcon({ node }: { node: GraphNode }) {
  if (node.nodeType === "author") return <UserRound size={18} />;
  if (node.nodeType === "institution") return <Building2 size={18} />;
  if (node.nodeType === "field") return <Tags size={18} />;
  if (node.nodeType === "semantic_entity" || node.nodeType === "global_semantic_entity") return <Tags size={18} />;
  if (node.nodeType === "unknown") return <CircleHelp size={18} />;
  return <FileText size={18} />;
}

function listTitle(nodeTypeFilter: NodeTypeFilter) {
  if (nodeTypeFilter === "paper") return "Papers";
  if (nodeTypeFilter === "author") return "Authors";
  if (nodeTypeFilter === "institution") return "Institutions";
  if (nodeTypeFilter === "field") return "Fields";
  if (nodeTypeFilter === "semantic_entity") return "Semantic Entities";
  if (nodeTypeFilter === "unknown") return "Other Nodes";
  if (nodeTypeFilter === "global_semantic_entity") return "Global Semantic Entities";
  return "Knowledge Nodes";
}

export function PaperList({
  nodes,
  selectedNodeId,
  nodeTypeFilter,
  onSelectNode,
  onCollapse,
}: PaperListProps) {
  return (
    <aside className="paper-list" aria-label="Graph node results">
      <div className="paper-list-toolbar">
        <div>
          <strong>{listTitle(nodeTypeFilter)}</strong>
          <span>{nodes.length} results</span>
        </div>
        {onCollapse ? (
          <button type="button" onClick={onCollapse} aria-label="Collapse result list">
            <PanelLeftClose size={17} />
          </button>
        ) : null}
      </div>

      {nodes.map((node) => {
        const selected = node.id === selectedNodeId;

        return (
          <button
            className={`paper-row node-row node-${node.nodeType} ${selected ? "selected" : ""}`}
            key={node.id}
            type="button"
            onClick={() => onSelectNode(node.id)}
          >
            <div className="paper-row-header">
              <div>
                <div className="node-kind">
                  <NodeIcon node={node} />
                  <span>{nodeTypeLabel(node.nodeType)}</span>
                </div>
                <h3>{nodeTitle(node)}</h3>
                <p>{nodeSubtitle(node)}</p>
              </div>
              <Circle size={20} className="select-ring" />
            </div>

            <p className="paper-abstract">{node.summary}</p>

            <div className="paper-meta">
              <span>{node.label}</span>
              <Dot size={16} />
              <span>{nodeMetricLabel(node)}</span>
            </div>
          </button>
        );
      })}
    </aside>
  );
}
