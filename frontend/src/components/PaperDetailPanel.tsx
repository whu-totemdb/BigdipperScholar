import { BookmarkPlus, Building2, CircleHelp, ExternalLink, FileText, Loader2, Network, Sparkles, Tags, UserRound, X } from "lucide-react";
import type { CSSProperties } from "react";
import type { AuthorNode, ExploreMode, GlobalSemanticDataset, GlobalSemanticEntityNode, GraphEdge, GraphNode, InstitutionNode, SemanticEntityNode } from "../types";
import { nodeTypeLabel, paperCanonicalId, paperLabel } from "../utils/graph";
import { getSemanticEntityVisual, semanticEntityType } from "../utils/semanticEntityStyle";

type PaperDetailPanelProps = {
  node: GraphNode | null;
  edge?: GraphEdge | null;
  edgeSourceNode?: GraphNode | null;
  edgeTargetNode?: GraphNode | null;
  seedLabel: string;
  onClose: () => void;
  onExplore: (mode: ExploreMode) => void;
  onExpandGlobal?: (entity: SemanticEntityNode) => void;
  globalDataset?: GlobalSemanticDataset | null;
  isExploring?: boolean;
  exploringMode?: ExploreMode | null;
};

function DetailIcon({ node }: { node: GraphNode }) {
  if (node.nodeType === "author") return <UserRound size={18} />;
  if (node.nodeType === "institution") return <Building2 size={18} />;
  if (node.nodeType === "field") return <Tags size={18} />;
  if (node.nodeType === "semantic_entity" || node.nodeType === "global_semantic_entity") {
    const visual = getSemanticEntityVisual(semanticEntityType(node));
    const EntityIcon = visual.Icon;
    return <EntityIcon size={18} />;
  }
  if (node.nodeType === "unknown") return <CircleHelp size={18} />;
  return <FileText size={18} />;
}

function hasDisplayValue(value: unknown) {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") {
    const text = value.trim();
    return Boolean(text) && text !== "Unknown" && text !== "N/A";
  }
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function formatDisplayValue(value: unknown) {
  if (typeof value === "string") return cleanText(value);
  if (typeof value === "number" && Number.isFinite(value)) return value.toLocaleString();
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value) || isRecord(value)) return JSON.stringify(value);
  return undefined;
}

function dataPreview(data?: Record<string, unknown>) {
  if (!data) return [];
  return Object.entries(data)
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
    .slice(0, 4)
    .map(([key, value]) => [key, String(value)] as const);
}

type Fact = {
  label: string;
  value: string;
  href?: string;
};

function cleanText(value?: string) {
  const text = value?.trim();
  if (!text || text === "Unknown" || text === "N/A") return undefined;
  return text;
}

function cleanAuthorText(value?: string) {
  const text = cleanText(value);
  if (!text) return undefined;
  const normalized = text.toLowerCase();
  if (normalized === "unknown author" || normalized === "unknown authors") return undefined;
  return text;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function dataString(data: Record<string, unknown> | undefined, ...keys: string[]) {
  if (!data) return undefined;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string") {
      const text = cleanText(value);
      if (text) return text;
    }
  }
  return undefined;
}

function dataAuthorString(data: Record<string, unknown> | undefined, ...keys: string[]) {
  if (!data) return undefined;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string") {
      const text = cleanAuthorText(value);
      if (text) return text;
    }
  }
  return undefined;
}

function dataNumber(data: Record<string, unknown> | undefined, ...keys: string[]) {
  if (!data) return undefined;
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function dataStringArrayLength(data: Record<string, unknown> | undefined, key: string) {
  const value = data?.[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").length : undefined;
}

function numberText(value?: number) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : undefined;
}

function externalUrl(value: string) {
  return /^https?:\/\//i.test(value) ? value : `https://${value}`;
}

function orcidUrl(orcid: string) {
  const value = orcid.replace(/^https?:\/\/orcid\.org\//i, "");
  return `https://orcid.org/${value}`;
}

function rorUrl(ror: string) {
  if (/^https?:\/\//i.test(ror)) return ror;
  return `https://ror.org/${ror.replace(/^ror:/i, "")}`;
}

function DetailFacts({ facts }: { facts: Fact[] }) {
  if (!facts.length) return null;

  return (
    <section className="article-facts">
      {facts.map((fact) => (
        <div key={fact.label}>
          <span>{fact.label}</span>
          {fact.href ? (
            <a href={fact.href} target="_blank" rel="noreferrer">
              {fact.value}
              <ExternalLink size={13} />
            </a>
          ) : (
            <strong>{fact.value}</strong>
          )}
        </div>
      ))}
    </section>
  );
}

function authorFacts(author: AuthorNode) {
  const facts: Fact[] = [];
  const data = author.data;
  const orcid = cleanText(author.orcid) ?? dataString(data, "orcid");
  const works = numberText(author.worksCount ?? dataNumber(data, "worksCount", "publicationCount"));
  const citations = numberText(author.citedByCount ?? dataNumber(data, "citedByCount"));
  const hIndex = numberText(author.hIndex ?? dataNumber(data, "hIndex"));
  const currentGraphPapers = numberText(author.currentGraphPaperCount ?? dataNumber(data, "currentGraphPaperCount") ?? dataStringArrayLength(data, "paperIds"));

  if (orcid) facts.push({ label: "ORCID", value: orcid, href: orcidUrl(orcid) });
  if (works) facts.push({ label: "Works", value: works });
  if (citations) facts.push({ label: "Citations", value: citations });
  if (hIndex) facts.push({ label: "H-index", value: hIndex });
  if (currentGraphPapers) facts.push({ label: "Current graph papers", value: currentGraphPapers });

  return facts;
}

function institutionFacts(institution: InstitutionNode) {
  const facts: Fact[] = [];
  const data = institution.data;
  const country = cleanText(institution.countryCode) ?? cleanText(institution.country) ?? dataString(data, "countryCode", "country");
  const type = cleanText(institution.type) ?? dataString(data, "type", "institutionType");
  const homepage = cleanText(institution.homepageUrl) ?? dataString(data, "homepageUrl");
  const ror = cleanText(institution.ror) ?? dataString(data, "ror");
  const hIndex = numberText(institution.hIndex ?? dataNumber(data, "hIndex"));
  const productivity = numberText(institution.productivity ?? dataNumber(data, "productivity"));
  const currentGraphAuthors = numberText(institution.currentGraphAuthorCount ?? dataNumber(data, "currentGraphAuthorCount") ?? dataStringArrayLength(data, "authorIds"));
  const currentGraphPapers = numberText(institution.currentGraphPaperCount ?? dataNumber(data, "currentGraphPaperCount", "paperCount") ?? dataStringArrayLength(data, "paperIds"));

  if (country) facts.push({ label: "Country", value: country });
  if (type) facts.push({ label: "Type", value: type });
  if (homepage) facts.push({ label: "Homepage", value: homepage.replace(/^https?:\/\//i, ""), href: externalUrl(homepage) });
  if (ror) facts.push({ label: "ROR", value: ror, href: rorUrl(ror) });
  if (hIndex) facts.push({ label: "H-index", value: hIndex });
  if (productivity) facts.push({ label: "Productivity", value: productivity });
  if (currentGraphAuthors) facts.push({ label: "Current graph authors", value: currentGraphAuthors });
  if (currentGraphPapers) facts.push({ label: "Current graph papers", value: currentGraphPapers });

  return facts;
}

function AuthorDetail({ author, onClose }: { author: AuthorNode; onClose: () => void }) {
  const data = author.data;
  const title = cleanAuthorText(author.displayName)
    ?? cleanAuthorText(author.authorName)
    ?? dataAuthorString(data, "displayName", "display_name", "authorName", "author_name", "label", "name")
    ?? cleanAuthorText(author.label)
    ?? "Author not listed";
  const dataInstitutions = Array.isArray(data?.institutions) ? data.institutions.filter(isRecord) : [];
  const affiliations = (author.affiliations?.length ? author.affiliations : dataInstitutions)
    ?.map((affiliation) => ({
      name: cleanText(affiliation.affiliationName as string | undefined)
        ?? cleanText(affiliation.rawAffiliationString as string | undefined)
        ?? dataString(affiliation, "label", "rawAffiliation"),
      position: cleanText(affiliation.authorPosition as string | undefined),
    }))
    .filter((affiliation) => Boolean(affiliation.name))
    .map((affiliation) => ({
      name: affiliation.name as string,
      position: affiliation.position,
    }));

  return (
    <aside className="detail-panel" aria-label="Author detail">
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Author</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <section className="article-summary">
        <div className="article-title-line">
          <p className="paper-kicker"><UserRound size={18} /> Author</p>
        </div>
        <h2>{title}</h2>
      </section>

      <DetailFacts facts={authorFacts(author)} />

      {affiliations?.length ? (
        <section className="abstract-section">
          <h3>Affiliations</h3>
          <dl className="metadata-list">
            {affiliations.map((affiliation, index) => (
              <div key={`${affiliation.name}-${index}`}>
                <dt>{affiliation.position ?? "Institution"}</dt>
                <dd>{affiliation.name}</dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}
    </aside>
  );
}

function InstitutionDetail({ institution, onClose }: { institution: InstitutionNode; onClose: () => void }) {
  const title = cleanText(institution.affiliationName) ?? dataString(institution.data, "affiliationName", "label") ?? institution.label;

  return (
    <aside className="detail-panel" aria-label="Institution detail">
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Institution</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <section className="article-summary">
        <div className="article-title-line">
          <p className="paper-kicker"><Building2 size={18} /> Institution</p>
        </div>
        <h2>{title}</h2>
      </section>

      <DetailFacts facts={institutionFacts(institution)} />
    </aside>
  );
}

function SemanticEntityDetail({ entity, onClose, onExpandGlobal, globalDataset, isExpanding }: { entity: SemanticEntityNode; onClose: () => void; onExpandGlobal?: (entity: SemanticEntityNode) => void; globalDataset?: GlobalSemanticDataset | null; isExpanding: boolean }) {
  const title = cleanText(entity.entityName) ?? dataString(entity.data, "entityName", "entity_name", "label") ?? entity.label;
  const entityType = semanticEntityType(entity);
  const comment = cleanText(entity.comment) ?? dataString(entity.data, "comment", "description", "summary");
  const properties = entity.properties ?? (isRecord(entity.data?.properties) ? entity.data.properties : undefined);
  const propertyEntries = properties
    ? Object.entries(properties)
      .filter(([, value]) => hasDisplayValue(value))
      .map(([key, value]) => [key, formatDisplayValue(value)] as const)
      .filter((entry): entry is readonly [string, string] => Boolean(entry[1]))
    : [];
  const visual = getSemanticEntityVisual(entityType);
  const EntityIcon = visual.Icon;
  const semanticStyle = {
    "--semantic-color": visual.color,
    "--semantic-border-color": visual.borderColor,
  } as CSSProperties;

  return (
    <aside className="detail-panel semantic-entity-detail" aria-label="Semantic entity detail" style={semanticStyle}>
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Semantic Entity</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <div className="semantic-detail-scroll">
        <section className="article-summary semantic-entity-summary">
          <div className="semantic-entity-heading">
            <span className="semantic-entity-hero-icon">
              <EntityIcon aria-hidden="true" size={25} strokeWidth={2.1} />
            </span>
            <div>
              <p className="paper-kicker">Semantic Entity</p>
              <h2>{title}</h2>
            </div>
          </div>
          <span className="semantic-type-badge">{entityType ?? "Unknown type"}</span>
        </section>

        {globalDataset && onExpandGlobal ? (
          <section className="global-expand-action">
            <button
              className="global-expand-button"
              type="button"
              onClick={() => onExpandGlobal(entity)}
              disabled={isExpanding}
            >
              {isExpanding ? <Loader2 className="spin" size={16} /> : <Network size={16} />}
              Expand in Global Graph
            </button>
            <span>{globalDataset.toUpperCase()} dataset · exact paper/entity mapping</span>
          </section>
        ) : null}

        <section className="abstract-section">
          <h3>Comment</h3>
          <p>{comment ?? "No comment available."}</p>
        </section>

        <section className="abstract-section semantic-properties">
          <h3>Properties</h3>
          {propertyEntries.length ? (
            <dl className="metadata-list">
              {propertyEntries.map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="semantic-empty-value">No additional properties.</p>
          )}
        </section>
      </div>
    </aside>
  );
}

function GlobalSemanticEntityDetail({ entity, onClose }: { entity: GlobalSemanticEntityNode; onClose: () => void }) {
  const entityType = semanticEntityType(entity);
  const visual = getSemanticEntityVisual(entityType);
  const EntityIcon = visual.Icon;
  const semanticStyle = {
    "--semantic-color": visual.color,
    "--semantic-border-color": visual.borderColor,
  } as CSSProperties;
  const facts: Fact[] = [
    { label: "Entity type", value: entityType ?? "Unknown type" },
    { label: "Paper count", value: entity.paperCount.toLocaleString() },
    { label: "Mention count", value: entity.mentionCount.toLocaleString() },
  ];
  if (entity.datasetScope) facts.push({ label: "Dataset scope", value: entity.datasetScope.toUpperCase() });
  if (entity.datasets.length) facts.push({ label: "Datasets", value: entity.datasets.join(", ") });
  const typeDistribution = Object.entries(entity.typeDistribution ?? {});

  return (
    <aside className="detail-panel semantic-entity-detail" aria-label="Global semantic entity detail" style={semanticStyle}>
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Global Semantic Entity</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <div className="semantic-detail-scroll">
        <section className="article-summary semantic-entity-summary">
          <div className="semantic-entity-heading">
            <span className="semantic-entity-hero-icon">
              <EntityIcon aria-hidden="true" size={25} strokeWidth={2.1} />
            </span>
            <div>
              <p className="paper-kicker">Global Semantic Entity</p>
              <h2>{entity.canonicalName}</h2>
            </div>
          </div>
          <span className="semantic-type-badge">{entityType ?? "Unknown type"}</span>
        </section>

        <DetailFacts facts={facts} />

        {typeDistribution.length ? (
          <section className="abstract-section semantic-properties">
            <h3>Type Distribution</h3>
            <dl className="metadata-list">
              {typeDistribution.map(([type, count]) => (
                <div key={type}>
                  <dt>{type}</dt>
                  <dd>{count.toLocaleString()}</dd>
                </div>
              ))}
            </dl>
          </section>
        ) : null}
      </div>
    </aside>
  );
}

export function PaperDetailPanel({
  node,
  edge = null,
  edgeSourceNode = null,
  edgeTargetNode = null,
  seedLabel,
  onClose,
  onExplore,
  onExpandGlobal,
  globalDataset = null,
  isExploring = false,
  exploringMode = null,
}: PaperDetailPanelProps) {
  const provenance = edge?.data ?? node?.data;
  if (provenance && Array.isArray(provenance.sourceObservationIds)) {
    return (
      <aside className="detail-panel" aria-label="Evidence detail">
        <div className="panel-header">
          <h2>{(node?.nodeType === "paper" ? node.title : node?.label) || edge?.label || "Evidence"}</h2>
          <button type="button" onClick={onClose} aria-label="Close detail"><X size={17} /></button>
        </div>
        <section className="abstract-section">
          <p>Observations: {provenance.sourceObservationIds.join(", ")}</p>
          {node?.nodeType === "paper" && node.abstract ? <p>{node.abstract}</p> : null}
          <details open>
            <summary>Recorded facts and provenance</summary>
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: "55vh", overflow: "auto" }}>{JSON.stringify(provenance, null, 2)}</pre>
          </details>
        </section>
      </aside>
    );
  }
  if (edge && ["local_to_global", "entity_mentioned_in_paper", "co_occurrence", "semantic_relation"].includes(edge.type)) {
    return (
      <GlobalSemanticEdgeDetail
        edge={edge}
        sourceNode={edgeSourceNode}
        targetNode={edgeTargetNode}
        onClose={onClose}
      />
    );
  }

  if (!node) {
    return (
      <aside className="detail-panel next-search-panel" aria-label="Next search">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Next Search</p>
            <h2>Grow the graph from {seedLabel}</h2>
          </div>
        </div>

        <div className="next-search-body">
          <div className="orbit-graphic" aria-hidden="true">
            <span />
            <span />
            <span />
            <span />
          </div>
          <p>
            Select a paper, author, or institution in the list or graph to inspect its local
            neighborhood and evidence.
          </p>
        </div>

        <div className="search-settings">
          <div>
            <strong>Search Settings</strong>
            <span>Mock heterogeneous graph, local interactions</span>
          </div>
          <button type="button">Search</button>
        </div>
      </aside>
    );
  }

  if (node.nodeType === "author") {
    return <AuthorDetail author={node} onClose={onClose} />;
  }

  if (node.nodeType === "institution") {
    return <InstitutionDetail institution={node} onClose={onClose} />;
  }

  if (node.nodeType === "semantic_entity") {
    return (
      <SemanticEntityDetail
        entity={node}
        onClose={onClose}
        onExpandGlobal={onExpandGlobal}
        globalDataset={globalDataset}
        isExpanding={isExploring}
      />
    );
  }

  if (node.nodeType === "global_semantic_entity") {
    return <GlobalSemanticEntityDetail entity={node} onClose={onClose} />;
  }


  if (node.nodeType !== "paper") {
    return (
      <aside className="detail-panel" aria-label={`${nodeTypeLabel(node.nodeType)} detail`}>
        <div className="panel-header article-header">
          <div className="article-tabs" aria-label="Detail tabs">
            <button className="active" type="button">{nodeTypeLabel(node.nodeType)}</button>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
            <X size={17} />
          </button>
        </div>

        <section className="article-summary">
          <div className="article-title-line">
            <p className="paper-kicker"><DetailIcon node={node} /> {nodeTypeLabel(node.nodeType)}</p>
          </div>
          <h2>{node.label}</h2>
          <p className="venue">{node.summary}</p>
        </section>

        <section className="article-facts">
          {node.nodeType === "field" ? (
            <>
              <div>
                <span>Level</span>
                <strong>{node.level ?? "Not listed"}</strong>
              </div>
              <div>
                <span>Works</span>
                <strong>{(node.worksCount ?? node.metric).toLocaleString()}</strong>
              </div>
              <div>
                <span>Citations</span>
                <strong>{(node.citedByCount ?? 0).toLocaleString()}</strong>
              </div>
            </>
          ) : null}
          {node.nodeType === "unknown" ? (
            <>
              <div>
                <span>Original Type</span>
                <strong>{node.originalNodeType ?? "Not listed"}</strong>
              </div>
              <div>
                <span>Metric</span>
                <strong>{node.metric.toLocaleString()}</strong>
              </div>
            </>
          ) : null}
        </section>

        <section className="abstract-section">
          <h3>Graph Role</h3>
          <p>
            This {nodeTypeLabel(node.nodeType).toLowerCase()} node is connected through the returned
            graph. Select connected nodes in the canvas to inspect the surrounding evidence.
          </p>
          {dataPreview(node.data).length ? (
            <dl className="metadata-list">
              {dataPreview(node.data).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </section>
      </aside>
    );
  }

  const paper = node;
  const paperId = paperCanonicalId(paper);
  const paperDataset = dataString(paper.data, "dataset", "datasetScope", "dataset_scope");

  return (
    <aside className="detail-panel" aria-label="Paper detail">
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Article</button>
          <button type="button">Notes 0</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <div className="paper-detail-scroll">
        <section className="article-summary">
          <div className="article-title-line">
            <p className="paper-kicker">{paperLabel(paper)}</p>
            <div className="article-title-actions">
              <button
                className="connection-detail-button"
                type="button"
                onClick={() => onExplore("connections")}
                disabled={isExploring}
              >
                {exploringMode === "connections" ? <Loader2 className="spin" size={15} /> : <Network size={15} />}
                Connections
              </button>
              <button className="icon-button" type="button" aria-label="Save paper">
                <BookmarkPlus size={18} />
              </button>
            </div>
          </div>
          <h2>{paper.title}</h2>
          <p className="venue">{paper.venue || "Venue not listed"}</p>
          {paper.authors.length ? <p className="authors">{paper.authors.join(", ")}</p> : null}
        </section>

        <section className="article-facts">
          <div>
            <span>DOI</span>
            {paper.doi ? (
              <a href={`https://doi.org/${paper.doi}`} target="_blank" rel="noreferrer">
                {paper.doi}
                <ExternalLink size={13} />
              </a>
            ) : (
              <strong>Not available</strong>
            )}
          </div>
          <div>
            <span>Citations</span>
            <strong>{paper.citationCount.toLocaleString()}</strong>
          </div>
          <div>
            <span>Topic</span>
            <strong>{paper.topic || "Not listed"}</strong>
          </div>
          <div>
            <span>Paper ID</span>
            <strong>{paperId || "Not listed"}</strong>
          </div>
          <div>
            <span>Year</span>
            <strong>{paper.year || "Not listed"}</strong>
          </div>
          <div>
            <span>Venue</span>
            <strong>{paper.venue || "Not listed"}</strong>
          </div>
          {paperDataset ? (
            <div>
              <span>Dataset</span>
              <strong>{paperDataset.toUpperCase()}</strong>
            </div>
          ) : null}
        </section>

        <section className="abstract-section">
          <h3>Abstract</h3>
          <p>{paper.abstract || "No abstract available."}</p>
        </section>
      </div>

      <div className="deep-dive">
        <div className="deep-dive-title">
          <Network size={18} />
          <strong>Dive deeper on this article</strong>
        </div>
        <div className="deep-dive-actions">
          <button type="button" onClick={() => onExplore("similar")} disabled={isExploring}>
            {exploringMode === "similar" ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />}
            Similar
          </button>
          <button type="button" onClick={() => onExplore("refs")} disabled={isExploring}>
            {exploringMode === "refs" ? <Loader2 className="spin" size={16} /> : <FileText size={16} />}
            Refs
          </button>
          <button type="button" onClick={() => onExplore("cited_by")} disabled={isExploring}>
            {exploringMode === "cited_by" ? <Loader2 className="spin" size={16} /> : <Network size={16} />}
            Cited By
          </button>
          <button type="button" onClick={() => onExplore("semantic_graph")} disabled={isExploring}>
            {exploringMode === "semantic_graph" ? <Loader2 className="spin" size={16} /> : <Tags size={16} />}
            Semantic Graph
          </button>
        </div>
      </div>
    </aside>
  );
}

function graphNodeName(node: GraphNode | null | undefined, fallback: string) {
  if (!node) return fallback;
  if (node.nodeType === "global_semantic_entity") return node.canonicalName;
  if (node.nodeType === "paper") return node.title || node.label || fallback;
  if (node.nodeType === "semantic_entity") return node.entityName || node.label || fallback;
  return node.label || fallback;
}

function graphNodePaperId(node: GraphNode | null | undefined) {
  return node?.nodeType === "paper" ? paperCanonicalId(node) : undefined;
}

function GlobalSemanticEdgeDetail({
  edge,
  sourceNode,
  targetNode,
  onClose,
}: {
  edge: GraphEdge;
  sourceNode?: GraphNode | null;
  targetNode?: GraphNode | null;
  onClose: () => void;
}) {
  const data = edge.data;
  const relationName = dataString(data, "relationName", "relation_name") ?? cleanText(edge.relationName);
  const paperCount = dataNumber(data, "paperCount", "weight") ?? edge.paperCount ?? edge.weight;
  const mentionCount = dataNumber(data, "mentionCount");
  const dataset = dataString(data, "dataset", "datasetScope");
  const edgeTypeLabel = edge.type === "local_to_global"
    ? "Local to global mapping"
    : edge.type === "entity_mentioned_in_paper"
      ? "Entity mentioned in paper"
      : edge.type === "co_occurrence"
        ? "Co-occurrence"
        : "Semantic relation";
  const facts: Fact[] = [
    { label: "Source", value: graphNodeName(sourceNode, edge.source) },
    { label: "Target", value: graphNodeName(targetNode, edge.target) },
    { label: "Edge type", value: edgeTypeLabel },
  ];
  let explanation = "This edge was returned by the global semantic expansion.";

  if (edge.type === "local_to_global") {
    explanation = "The selected local semantic entity maps to the global semantic entity.";
    const paperId = dataString(data, "paperId", "paper_id");
    if (paperId) facts.push({ label: "Paper ID", value: paperId });
    const localEntityId = dataString(data, "localEntityId", "local_entity_id");
    if (localEntityId) facts.push({ label: "Local entity ID", value: localEntityId });
    const globalEntityId = dataString(data, "globalEntityId", "global_entity_id");
    if (globalEntityId) facts.push({ label: "Global entity ID", value: globalEntityId });
  } else if (edge.type === "entity_mentioned_in_paper") {
    explanation = "This global semantic entity appears in the linked paper.";
    const paperId = dataString(data, "paperId", "paper_id") ?? graphNodePaperId(sourceNode) ?? graphNodePaperId(targetNode);
    if (paperId) facts.push({ label: "Paper ID", value: paperId });
    if (mentionCount !== undefined) facts.push({ label: "Mention count", value: mentionCount.toLocaleString() });
  } else if (edge.type === "co_occurrence") {
    explanation = "The two global semantic entities co-occur in the returned papers.";
    if (paperCount !== undefined) facts.push({ label: "Paper count / weight", value: paperCount.toLocaleString() });
  } else {
    explanation = "The two global semantic entities are connected by a semantic relation.";
    if (relationName) facts.push({ label: "Relation name", value: relationName });
    if (paperCount !== undefined) facts.push({ label: "Paper count", value: paperCount.toLocaleString() });
    if (mentionCount !== undefined) facts.push({ label: "Mention count", value: mentionCount.toLocaleString() });
  }

  if (dataset) facts.push({ label: "Dataset", value: dataset.toUpperCase() });

  return (
    <aside className="detail-panel global-edge-detail" aria-label="Global semantic edge detail">
      <div className="panel-header article-header">
        <div className="article-tabs" aria-label="Detail tabs">
          <button className="active" type="button">Semantic Graph Edge</button>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">
          <X size={17} />
        </button>
      </div>

      <div className="semantic-detail-scroll">
        <section className="article-summary">
          <p className="paper-kicker"><Network size={18} /> {edgeTypeLabel}</p>
          <h2>{graphNodeName(sourceNode, edge.source)} → {graphNodeName(targetNode, edge.target)}</h2>
        </section>

        <DetailFacts facts={facts} />

        <section className="abstract-section">
          <h3>Meaning</h3>
          <p>{explanation}</p>
        </section>
      </div>
    </aside>
  );
}
