import {
  ArrowLeftRight,
  Atom,
  Blocks,
  Boxes,
  BrainCircuit,
  ChartNoAxesCombined,
  CircleDotDashed,
  CodeXml,
  Combine,
  Cpu,
  Database,
  FlaskConical,
  FolderCog,
  Gauge,
  HardDrive,
  Network,
  Orbit,
  RadioTower,
  ScanSearch,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  Star,
  Tag,
  type LucideIcon,
} from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
type SemanticEntityLike = {
  entityType?: string;
  data?: Record<string, unknown>;
};

export type SemanticEntityVisual = {
  color: string;
  borderColor: string;
  Icon: LucideIcon;
};

export const SEMANTIC_ENTITY_VISUALS: Record<string, SemanticEntityVisual> = {
  StorageTech: { color: "#0f766e", borderColor: "#115e59", Icon: HardDrive },
  System: { color: "#2563eb", borderColor: "#1d4ed8", Icon: Server },
  DataStructure: { color: "#7c3aed", borderColor: "#6d28d9", Icon: Network },
  Workload: { color: "#ea580c", borderColor: "#c2410c", Icon: Gauge },
  DataModel: { color: "#0891b2", borderColor: "#0e7490", Icon: Boxes },
  QueryLanguage: { color: "#4f46e5", borderColor: "#4338ca", Icon: CodeXml },
  ML4DB: { color: "#c026d3", borderColor: "#a21caf", Icon: BrainCircuit },
  DBMSEngines: { color: "#0369a1", borderColor: "#075985", Icon: Database },
  DataIntegration: { color: "#0d9488", borderColor: "#0f766e", Icon: Combine },
  Benchmark: { color: "#ca8a04", borderColor: "#a16207", Icon: ChartNoAxesCombined },
  Hardware: { color: "#475569", borderColor: "#334155", Icon: Cpu },
  Transaction: { color: "#059669", borderColor: "#047857", Icon: ArrowLeftRight },
  SecurityPrivacy: { color: "#dc2626", borderColor: "#b91c1c", Icon: ShieldCheck },
  Architecture: { color: "#9333ea", borderColor: "#7e22ce", Icon: Blocks },
  StorageManagement: { color: "#15803d", borderColor: "#166534", Icon: FolderCog },
  DBAdministration: { color: "#b45309", borderColor: "#92400e", Icon: Settings },
  DistProtocol: { color: "#0284c7", borderColor: "#0369a1", Icon: RadioTower },
  VectorRetrieval: { color: "#db2777", borderColor: "#be185d", Icon: ScanSearch },
  PhysicalProcess: { color: "#e11d48", borderColor: "#be123c", Icon: Atom },
  Exoplanet: { color: "#7c3aed", borderColor: "#6d28d9", Icon: Orbit },
  BinarySystem: { color: "#4338ca", borderColor: "#3730a3", Icon: CircleDotDashed },
  Star: { color: "#d97706", borderColor: "#b45309", Icon: Star },
  StarCluster: { color: "#c026d3", borderColor: "#a21caf", Icon: Sparkles },
  Method: { color: "#0f766e", borderColor: "#115e59", Icon: FlaskConical },
};

export const DEFAULT_SEMANTIC_ENTITY_VISUAL: SemanticEntityVisual = {
  color: "#64748b",
  borderColor: "#475569",
  Icon: Tag,
};

function normalizedEntityType(value?: string) {
  return value?.trim().replace(/[^a-zA-Z0-9]/g, "").toLowerCase() ?? "";
}

const visualsByNormalizedType = new Map(
  Object.entries(SEMANTIC_ENTITY_VISUALS).map(([entityType, visual]) => [
    normalizedEntityType(entityType),
    visual,
  ]),
);

export function semanticEntityType(entity: SemanticEntityLike) {
  const directType = entity.entityType?.trim();
  if (directType) return directType;

  for (const key of ["entityType", "entity_type", "type"]) {
    const value = entity.data?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }

  return undefined;
}

export function semanticEntityTypeLabel(entity: SemanticEntityLike) {
  return semanticEntityType(entity) ?? "Unknown";
}

export function getSemanticEntityVisual(entityType?: string) {
  return visualsByNormalizedType.get(normalizedEntityType(entityType))
    ?? DEFAULT_SEMANTIC_ENTITY_VISUAL;
}

const iconDataUriCache = new Map<LucideIcon, string>();

export function semanticEntityIconDataUri(Icon: LucideIcon) {
  const cached = iconDataUriCache.get(Icon);
  if (cached) return cached;

  const markup = renderToStaticMarkup(
    <Icon aria-hidden="true" color="#ffffff" size={28} strokeWidth={2.1} />,
  );
  const dataUri = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
  iconDataUriCache.set(Icon, dataUri);
  return dataUri;
}
