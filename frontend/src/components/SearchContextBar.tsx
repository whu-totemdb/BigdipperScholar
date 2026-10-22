import { ArrowLeft, Filter, LayoutList, Map, Network } from "lucide-react";
import type { GraphStateSummary, NodeTypeFilter, ViewMode } from "../types";

const nodeTypeOptions: Array<{ value: NodeTypeFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "paper", label: "Papers" },
  { value: "author", label: "Authors" },
  { value: "institution", label: "Institutions" },
  { value: "semantic_entity", label: "Entities" },
];

type SearchContextBarProps = {
  context: string;
  resultCount: number;
  totalCount: number;
  nodeTypeFilter: NodeTypeFilter;
  viewMode: ViewMode;
  listCollapsed: boolean;
  canGoBack: boolean;
  showNodeTypeFilter: boolean;
  graphStates?: GraphStateSummary[];
  activeGraphId?: string | null;
  onGraphStateSelect?: (graphId: string) => void;
  onBack: () => void;
  onNodeTypeFilterChange: (nodeType: NodeTypeFilter) => void;
  onToggleList: () => void;
  onViewModeChange: (mode: ViewMode) => void;
};

export function SearchContextBar({
  context,
  resultCount,
  totalCount,
  nodeTypeFilter,
  viewMode,
  listCollapsed,
  canGoBack,
  showNodeTypeFilter,
  graphStates = [],
  activeGraphId,
  onGraphStateSelect,
  onBack,
  onNodeTypeFilterChange,
  onToggleList,
  onViewModeChange,
}: SearchContextBarProps) {
  return (
    <div className="context-bar">
      <div className="context-token">
        {canGoBack ? (
          <button className="back-button" type="button" onClick={onBack} aria-label="Return to previous graph">
            <ArrowLeft size={17} />
          </button>
        ) : null}
        <span className="seed-pill" title={context}>
          {context}
        </span>
      </div>

      <div className="context-actions">
        {graphStates.length > 0 && onGraphStateSelect ? (
          <select
            className="graph-state-select"
            value={activeGraphId ?? ""}
            onChange={(event) => {
              if (event.target.value) onGraphStateSelect(event.target.value);
            }}
            aria-label="Saved graph states"
          >
            <option value="">Saved graphs</option>
            {graphStates.map((state) => (
              <option key={state.id} value={state.id}>{state.title || state.queryContext}</option>
            ))}
          </select>
        ) : null}
        {showNodeTypeFilter ? (
          <div className="node-type-control" aria-label="Node type filter">
            {nodeTypeOptions.map((option) => (
              <button
                className={nodeTypeFilter === option.value ? "active" : ""}
                key={option.value}
                type="button"
                onClick={() => onNodeTypeFilterChange(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        ) : null}
        <span className="result-count">
          {resultCount} of {totalCount}
        </span>
        <button
          className={`icon-button ${!listCollapsed ? "active-soft" : ""}`}
          type="button"
          onClick={onToggleList}
          aria-label="Toggle result list"
        >
          <Filter size={17} />
        </button>
        <button
          className={`icon-button ${viewMode === "default" ? "active" : ""}`}
          type="button"
          onClick={() => onViewModeChange("default")}
          aria-label="Default graph workspace"
          title="Default graph workspace"
        >
          <Network size={17} />
        </button>
        <button
          className={`icon-button ${viewMode === "list" ? "active" : ""}`}
          type="button"
          onClick={() => onViewModeChange("list")}
          aria-label="List mode"
          title="List mode"
        >
          <LayoutList size={17} />
        </button>
        <button
          className={`icon-button ${viewMode === "graph" ? "active" : ""}`}
          type="button"
          onClick={() => onViewModeChange("graph")}
          aria-label="Graph mode"
          title="Graph mode"
        >
          <Map size={17} />
        </button>
      </div>
    </div>
  );
}
