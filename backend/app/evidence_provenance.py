"""Ground evidence in selected tool data; no database access or invented facts."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Iterator

RELATIONS = {"cites", "reference", "cited_by", "similar", "authored_by", "affiliated_with", "coauthored_with", "institution_collaboration", "has_field"}


def unpack(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return unpack(json.loads(value))
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return [unpack(item.get("text")) if isinstance(item, dict) and item.get("type") == "text" else unpack(item) for item in value]
    return value


def rows(value: Any) -> Iterator[dict]:
    value = unpack(value)
    if isinstance(value, list):
        for item in value:
            yield from rows(item)
    elif isinstance(value, dict):
        if value.get("type") == "text" and "text" in value:
            yield from rows(value["text"])
            return
        containers = ("data", "rows", "papers", "results", "items", "nodes", "edges", "contexts", "metadata", "seed", "references", "citations", "similar_papers")
        found = False
        for key in containers:
            if isinstance(value.get(key), (list, dict)):
                found = True
                yield from rows(value[key])
        if not found or any(k in value for k in ("paperid", "paperId", "citing_paperid", "nodeType", "source")):
            yield value


def has_data(observation: dict) -> bool:
    if observation.get("status") != "success" or observation.get("toolName") in {"get_database_schema", "read_research_skill"}:
        return False
    value = unpack(observation.get("result"))
    if value in (None, "", [], {}):
        return False
    found = list(rows(value))
    return any(row and row.get("status") not in {"error", "failed"} and row.get("row_count") != 0
               and not ("data" in row and not row["data"]) for row in found)


def identity(value):
    return str(value).strip() if value is not None else ""


def labels(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def response_metadata(value):
    value = unpack(value)
    if isinstance(value, list):
        return {key: item for part in value for key, item in response_metadata(part).items()}
    if isinstance(value, dict):
        return {key: value[key] for key in ("embedding", "query", "limit", "metric_type") if key in value}
    return {}


def ground_bundle(selected: list[dict], proposed: dict | None = None) -> dict:
    nodes, edges, units = {}, {}, []
    proposed = proposed or {}

    def node(key, kind, label, oid, **attributes):
        if not key:
            return
        item = nodes.setdefault(key, {"id": key, "refId": key, "nodeType": kind, "label": label or key, "sourceObservationIds": []})
        if oid not in item["sourceObservationIds"]:
            item["sourceObservationIds"].append(oid)
        if label and label != key:
            item["label"] = label
        item.update({k: v for k, v in attributes.items() if v is not None and v != ""})

    def edge(source, target, kind, oid, **attributes):
        if not source or not target or source == target or kind not in RELATIONS:
            return
        key = (source, target, kind)
        item = edges.setdefault(key, {"id": "edge-" + hashlib.sha256(repr(key).encode()).hexdigest()[:20],
                                     "source": source, "target": target, "type": kind,
                                     "label": kind.replace("_", " "), "sourceObservationIds": []})
        if oid not in item["sourceObservationIds"]:
            item["sourceObservationIds"].append(oid)
        item.update(attributes)

    for observation in selected:
        if not has_data(observation):
            continue
        oid = int(observation["observationId"])
        args, execution = observation.get("arguments") or {}, observation.get("execution") or {}
        tool = str(observation.get("toolName") or "query")
        source = {"observationId": oid, "toolName": tool, "arguments": args,
                  "query": execution.get("queryText") or args.get("sql"), "execution": execution,
                  "toolMetadata": response_metadata(observation.get("result"))}
        origin = f"observation-{oid}"
        node(origin, "field", tool, oid, evidenceType="observation", toolName=tool, source=source)
        for index, row in enumerate(rows(observation.get("result"))):
            eid = f"obs-{oid}-row-{index}"
            pid = identity(row.get("paperid") or row.get("paperId") or row.get("paper_id"))
            if not pid and (row.get("nodeType") == "paper" or row.get("title")):
                pid = identity(row.get("id"))
            title = row.get("title") or row.get("display_name") or row.get("label") or pid
            kind = "entity" if pid else "statistic"
            if row.get("abstract") or row.get("abstract_inverted_index"):
                kind = "document"
            score = row.get("score", row.get("similarity", row.get("distance")))
            if score is not None:
                kind = "semantic"
            units.append({"evidenceId": eid, "evidenceType": kind, "label": str(title or tool),
                          "content": row, "sourceObservationIds": [oid], "sources": [source]})
            if pid:
                node(pid, "paper", str(title), oid, title=str(title), year=row.get("year", row.get("publication_year")),
                     date=row.get("date"), abstract=row.get("abstract"), doi=row.get("doi"),
                     citationCount=row.get("cited_by_count", row.get("citationCount")),
                     venue=row.get("venue", row.get("venue_key")), evidenceType="paper")
                retrieval = {"evidenceId": eid, "score": score, "rank": row.get("rank"),
                             "scoreKind": "distance" if "distance" in row and "score" not in row and "similarity" not in row else "score",
                             "distance": row.get("distance"), "query": args.get("query")}
                edge(pid, origin, "has_field", oid, **retrieval)
                seed = identity(args.get("paper_id") or args.get("seed_paper_id"))
                if seed and "similar" in tool and seed != pid:
                    node(seed, "paper", seed, oid)
                    edge(seed, pid, "similar", oid, **retrieval)
                authors = labels(row.get("authors"))
                if row.get("authorid") or row.get("author_id") or row.get("author_name"):
                    authors.append({"authorid": row.get("authorid", row.get("author_id")), "name": row.get("author_name")})
                for author in authors:
                    author = author if isinstance(author, dict) else {"name": author}
                    name = identity(author.get("name") or author.get("display_name") or author.get("author_name"))
                    aid = identity(author.get("authorid") or author.get("author_id") or author.get("id"))
                    if not aid and name:
                        aid = "author-label-" + hashlib.sha256(name.encode()).hexdigest()[:16]
                    node(aid, "author", name or aid, oid, identityBasis="id" if author.get("authorid") or author.get("id") else "observed_label")
                    edge(pid, aid, "authored_by", oid, evidenceId=eid)
                    # A paper's institution list does not prove each author's affiliation.
                    iid = identity(author.get("institutionid") or (row.get("institutionid") if row.get("authorid") == aid else None))
                    iname = identity(author.get("institution_name") or row.get("institution_name"))
                    if iid:
                        node(iid, "institution", iname or iid, oid)
                        edge(aid, iid, "affiliated_with", oid, evidenceId=eid, paperid=pid)
            citing, cited = identity(row.get("citing_paperid")), identity(row.get("cited_paperid"))
            if citing and cited:
                node(citing, "paper", citing, oid)
                node(cited, "paper", cited, oid)
                edge(citing, cited, "cites", oid, evidenceId=eid)
                units[-1]["evidenceType"] = "relation"
            if row.get("nodeType") in {"author", "institution", "field"} and row.get("id"):
                node(str(row["id"]), row["nodeType"], str(row.get("label") or row["id"]), oid)
            for entity_kind, id_keys, name_keys in (
                ("author", ("authorid", "author_id"), ("author_name", "display_name")),
                ("institution", ("institutionid", "institution_id"), ("institution_name", "affiliation_name")),
                ("field", ("fieldid", "field_id"), ("field_name", "display_name")),
            ):
                entity_id = next((identity(row[key]) for key in id_keys if row.get(key)), "")
                if entity_id:
                    entity_label = next((str(row[key]) for key in name_keys if row.get(key)), entity_id)
                    node(entity_id, entity_kind, entity_label, oid, observedAttributes=row)
                    edge(entity_id, origin, "has_field", oid, evidenceId=eid)
            relation = row.get("type", row.get("edge_type"))
            if relation in RELATIONS and row.get("source") and row.get("target"):
                edge(str(row["source"]), str(row["target"]), relation, oid, evidenceId=eid, score=score)
                units[-1]["evidenceType"] = "relation"

    annotations = {item.get("evidenceId"): item for item in proposed.get("evidence", []) if isinstance(item, dict)}
    for unit in units:
        if unit["evidenceId"] in annotations:
            unit["supportingClaims"] = annotations[unit["evidenceId"]].get("supportingClaims", [])
            unit["selectedByEvidenceAgent"] = True
    graph_edges = [item for item in edges.values() if item["source"] in nodes and item["target"] in nodes]
    citations = [{"paperId": item["id"], "title": item.get("title") or item["label"], "sourceObservationIds": item["sourceObservationIds"]}
                 for item in nodes.values() if item["nodeType"] == "paper"]
    return {"shouldRenderGraph": bool(nodes), "reason": "Grounded exclusively in selected successful observations.",
            "evidence": units, "citations": citations,
            "graphIntent": {"title": (proposed.get("graphIntent") or {}).get("title") or "Evidence Graph", "nodes": list(nodes.values()), "edges": graph_edges},
            "missingReportEntities": []}
