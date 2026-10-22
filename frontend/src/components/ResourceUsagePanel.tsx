import {
  Activity,
  BrainCircuit,
  Cpu,
  Database,
  MemoryStick,
  Network,
  Timer,
  Wrench,
} from "lucide-react";
import type { ComponentType, SVGProps } from "react";
import type {
  ResourceUsage,
  ResourceUsageModule,
  ResourceUsageSummary,
} from "../types";

type ResourceUsagePanelProps = {
  usage: ResourceUsage;
};

type ResourceCategory = "llm" | "tool" | "database" | "graph" | "system";

type ResourceIcon = ComponentType<SVGProps<SVGSVGElement> & { size?: number | string }>;

type UsageCard = {
  label: string;
  value: string;
  note?: string;
  icon: ResourceIcon;
};

function isReportedNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function formatCount(value: number | null | undefined) {
  return isReportedNumber(value) ? value.toLocaleString() : "Not reported";
}

function formatDuration(value: number | null | undefined) {
  if (!isReportedNumber(value)) return "Not reported";
  if (value >= 60_000) return (value / 60_000).toFixed(1) + " min";
  if (value >= 1_000) return (value / 1_000).toFixed(value >= 10_000 ? 1 : 2) + " s";
  return Math.round(value).toLocaleString() + " ms";
}

function formatPercent(value: number | null | undefined) {
  if (!isReportedNumber(value)) return "Not reported";
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 }) + "%";
}

function formatMemory(value: number | null | undefined) {
  if (!isReportedNumber(value)) return "Not reported";
  if (value >= 1_024) {
    return (value / 1_024).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " GB";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 }) + " MB";
}


function formatTokenNote(summary?: ResourceUsageSummary | null) {
  if (!summary) return undefined;

  const parts = [
    isReportedNumber(summary.promptTokens)
      ? "Prompt " + summary.promptTokens.toLocaleString()
      : null,
    isReportedNumber(summary.completionTokens)
      ? "Completion " + summary.completionTokens.toLocaleString()
      : null,
  ].filter(Boolean);

  return parts.length ? parts.join(" · ") : undefined;
}

function getResourceCategory(type: string): ResourceCategory {
  const normalizedType = type.toLowerCase();

  if (normalizedType.includes("llm") || normalizedType.includes("model")) return "llm";
  if (normalizedType.includes("tool")) return "tool";
  if (
    normalizedType.includes("database") ||
    normalizedType.includes("postgres") ||
    normalizedType.includes("sqlite") ||
    normalizedType.includes("vector") ||
    normalizedType === "db"
  ) {
    return "database";
  }
  if (normalizedType.includes("graph")) return "graph";
  return "system";
}

function getResourceIcon(category: ResourceCategory): ResourceIcon {
  if (category === "llm") return BrainCircuit;
  if (category === "tool") return Wrench;
  if (category === "database") return Database;
  if (category === "graph") return Network;
  return Cpu;
}

function hasModuleUsage(module: ResourceUsageModule) {
  return [
    module.calls,
    module.latencyMs,
    module.promptTokens,
    module.completionTokens,
    module.totalTokens,
  ].some((value) => isReportedNumber(value) && value > 0);
}

function buildUsageCards(usage: ResourceUsage): UsageCard[] {
  const summary = usage.summary;
  const memoryNote = isReportedNumber(summary?.peakMemoryMb)
    ? "Peak " + formatMemory(summary.peakMemoryMb)
    : undefined;

  return [
    {
      label: "Total latency",
      value: formatDuration(usage.latencyMs),
      icon: Timer,
    },
    {
      label: "LLM calls",
      value: formatCount(summary?.llmCalls),
      icon: BrainCircuit,
    },
    {
      label: "Tool calls",
      value: formatCount(summary?.toolCalls),
      icon: Wrench,
    },
    {
      label: "DB queries",
      value: formatCount(summary?.dbQueries),
      icon: Database,
    },
    {
      label: "Tokens",
      value: formatCount(summary?.totalTokens),
      note: formatTokenNote(summary),
      icon: Activity,
    },
    {
      label: "CPU",
      value: formatPercent(summary?.cpuPercent),
      icon: Cpu,
    },
    {
      label: "Memory",
      value: formatMemory(summary?.memoryMb),
      note: memoryNote,
      icon: MemoryStick,
    },
  ];
}

export function ResourceUsagePanel({ usage }: ResourceUsagePanelProps) {
  const cards = buildUsageCards(usage);
  const modules = (usage.modules ?? []).filter(hasModuleUsage);
  const totalModuleLatency = modules.reduce(
    (total, module) =>
      total + (isReportedNumber(module.latencyMs) && module.latencyMs > 0 ? module.latencyMs : 0),
    0,
  );
  return (
    <section className="resource-usage-panel" aria-label="Resource Usage">
      <div className="resource-usage-heading">
        <div>
          <strong>资源占用</strong>
          <span>Resource Usage</span>
        </div>
        {usage.status ? (
          <span className={"resource-status resource-status-" + usage.status.toLowerCase()}>
            {usage.status}
          </span>
        ) : null}
      </div>

      <div className="resource-summary-grid">
        {cards.map((card) => {
          const Icon = card.icon;
          return (
            <div className="resource-summary-card" key={card.label}>
              <div className="resource-summary-label">
                <Icon size={13} aria-hidden="true" />
                <span>{card.label}</span>
              </div>
              <strong>{card.value}</strong>
              {card.note ? <small>{card.note}</small> : null}
            </div>
          );
        })}
      </div>

      {modules.length ? (
        <div className="resource-section">
          <h4>Modules</h4>
          <div className="resource-module-list">
            {modules.map((module, index) => {
              const category = getResourceCategory(module.type);
              const Icon = getResourceIcon(category);
              const latencyShare =
                totalModuleLatency > 0 && isReportedNumber(module.latencyMs)
                  ? (Math.max(module.latencyMs, 0) / totalModuleLatency) * 100
                  : 0;
              const barWidth = latencyShare > 0 ? Math.max(latencyShare, 2) : 0;

              return (
                <div className="resource-module" key={module.name + "-" + index}>
                  <div className="resource-module-heading">
                    <span className={"resource-type-icon resource-type-" + category}>
                      <Icon size={13} aria-hidden="true" />
                    </span>
                    <strong>{module.name}</strong>
                    <span className="resource-module-type">{module.type}</span>
                  </div>
                  <div className="resource-module-metrics">
                    <span>{formatDuration(module.latencyMs)}</span>
                    <span>{formatCount(module.calls)} calls</span>
                  </div>
                  <div
                    className="resource-module-track"
                    aria-label={
                      module.name + " latency share " + latencyShare.toFixed(1) + "%"
                    }
                  >
                    <span
                      className={"resource-module-bar resource-type-" + category}
                      style={{ width: Math.min(barWidth, 100) + "%" }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

    </section>
  );
}
