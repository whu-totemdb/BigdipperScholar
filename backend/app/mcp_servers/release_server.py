"""Public MCP entry point. The MMDB engine is deployed independently.

All database operations, including operator-defined semantic SQL, use the same
read-only connection factory. No Milvus/Neo4j pipeline is enabled here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path

import httpx
from fastmcp import FastMCP

from app.config import get_env
from app.mcp_servers import cn_queries as cn
from app.mcp_servers.mmdb_vectors import vector_settings, vector_search_sql, validate_vector

mcp = FastMCP("BigDipper Scholar MMDB")


def _parameter_query(sql: str, parameters: dict) -> dict:
    started = time.perf_counter()
    visible_parameters = dict(parameters)
    if "embedding" in visible_parameters:
        vector = visible_parameters["embedding"]
        values = json.loads(vector) if isinstance(vector, str) else vector
        visible_parameters["embedding"] = {
            "omitted": True, "dimensions": len(values),
            "sha256": hashlib.sha256(json.dumps(values).encode()).hexdigest(),
            "model": get_env("BIGDIPPER_EMBEDDING_MODEL") or None,
            "source": "stored_paper_vector" if parameters.get("paper_id") else "query_embedding",
        }
    execution = {"backend": "mmdb", "queryLanguage": "sql", "queryText": sql,
                 "parameters": visible_parameters}
    connection = None
    try:
        connection = cn.db.get_connection()
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        result = {"status": "success", "data": rows, "row_count": len(rows)}
    except Exception as exc:
        result = {"status": "error", "message": str(exc), "data": [], "row_count": 0}
    finally:
        if connection is not None:
            connection.close()
    execution.update({"databaseLatencyMs": round((time.perf_counter() - started) * 1000, 2),
                      "rowCount": result["row_count"]})
    result["_execution"] = execution
    return result


@mcp.tool
async def get_database_schema() -> str:
    """Inspect accessible relations, columns and indexes, plus operator-supplied graph/vector capabilities."""
    schema = get_env("BIGDIPPER_DB_SCHEMA", "public")
    prefix = get_env("BIGDIPPER_TABLE_PREFIX", "")
    parameters = {"schema": schema, "prefix": prefix}
    columns = await asyncio.to_thread(_parameter_query, """
        SELECT table_name, column_name, data_type, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %(schema)s
          AND left(table_name, length(%(prefix)s)) = %(prefix)s
        ORDER BY table_name, ordinal_position LIMIT 5001
    """, parameters)
    if columns["status"] != "success":
        return json.dumps(columns, ensure_ascii=False, default=str)
    indexes = await asyncio.to_thread(_parameter_query, """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = %(schema)s
          AND left(tablename, length(%(prefix)s)) = %(prefix)s
        ORDER BY tablename, indexname LIMIT 2001
    """, parameters)
    if indexes["status"] != "success":
        indexes["_execution"]["statements"] = [columns["_execution"], dict(indexes["_execution"])]
        return json.dumps(indexes, ensure_ascii=False, default=str)
    notes_path = get_env("BIGDIPPER_SCHEMA_NOTES_PATH")
    notes = json.loads(Path(notes_path).read_text()) if notes_path else {}
    payload = {
        "status": "success", "schema": schema, "tablePrefix": prefix,
        "columns": columns["data"][:5000], "indexes": indexes["data"][:2000],
        "truncated": len(columns["data"]) > 5000 or len(indexes["data"]) > 2000,
        "capabilities": notes,
        "semanticSearchConfigured": bool((get_env("BIGDIPPER_SEMANTIC_SQL_PATH") or vector_settings()["table"]) and get_env("BIGDIPPER_EMBEDDING_BASE_URL")),
        "vectorSearch": vector_settings(),
        "queryBackend": "mmdb",
        "dataProfile": get_env("BIGDIPPER_DATA_PROFILE", "generic"),
        "_execution": {"backend": "mmdb", "queryLanguage": "sql",
                       "statements": [columns["_execution"], indexes["_execution"]]},
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


@mcp.tool
async def execute_sciscinet_sql(sql: str, limit: int = 100) -> str:
    """Execute general read-only SQL/MMSQL against the configured database; tables are not restricted to CN."""
    return await cn.execute_sciscinet_sql(sql, limit)


@mcp.tool
async def search_papers_by_natural_language(query: str, limit: int = 10) -> str:
    """Embed text, then query vectors in the same multimodel database. CN uses its paper vector table; other schemas can configure a table or SQL template. Returns actual cosine scores, distance, rank and query provenance."""
    sql_path = get_env("BIGDIPPER_SEMANTIC_SQL_PATH")
    base = (get_env("BIGDIPPER_EMBEDDING_BASE_URL") or "").rstrip("/")
    if not base or not (sql_path or vector_settings()["table"]):
        return json.dumps({"status": "error", "message": "Configure the embedding endpoint and an MMDB vector table or semantic SQL template."})
    protocol = get_env("BIGDIPPER_EMBEDDING_PROTOCOL", "custom")
    model = get_env("BIGDIPPER_EMBEDDING_MODEL")
    key = get_env("BIGDIPPER_EMBEDDING_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        if protocol == "openai":
            response = await client.post(f"{base}/embeddings", json={"input": query, "model": model}, headers=headers)
        elif protocol == "custom":
            response = await client.post(f"{base}/embed", json={"query": query}, headers=headers)
        else:
            raise ValueError("Unknown embedding protocol")
        response.raise_for_status()
        body = response.json()
    vector = body["data"][0]["embedding"] if protocol == "openai" else body.get("embedding", body.get("vector"))
    validate_vector(vector)
    template = Path(sql_path).read_text().strip() if sql_path else vector_search_sql()
    normalized, error = cn.normalize_readonly_sql(template, cn.bounded_limit(limit, maximum=100))
    if error:
        raise ValueError(error)
    result = await asyncio.to_thread(_parameter_query, normalized, {
        "embedding": json.dumps(vector), "limit": cn.bounded_limit(limit, maximum=100), "query": query, "paper_id": None,
    })
    result["query"] = query
    result["embedding"] = {"model": model, "dimensions": len(vector), "protocol": protocol}
    result["vectorSource"] = {"sqlTemplate": bool(sql_path), **vector_settings()}
    for rank, row in enumerate(result["data"], 1):
        row.setdefault("rank", rank)
    result["_execution"]["queryLanguage"] = "mmsql"
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool
async def find_similar_papers_by_paperid(paper_id: str, limit: int = 10) -> str:
    """Find papers by a seed paper's stored vector in the same multimodel database. Excludes the seed; returns seed ID, actual distance, cosine score and rank."""
    from app.mcp_servers.mmdb_vectors import seed_vector_sql
    seed = await asyncio.to_thread(_parameter_query, seed_vector_sql(), {"paper_id": paper_id})
    if seed.get("status") == "error":
        return json.dumps(seed, ensure_ascii=False, default=str)
    if not seed["data"] or not seed["data"][0].get("embedding"):
        return json.dumps({"status": "error", "message": "Seed paper has no stored vector",
                           "paper_id": paper_id, "_execution": seed.get("_execution", {})})
    vector = json.loads(seed["data"][0]["embedding"])
    validate_vector(vector)
    result = await asyncio.to_thread(_parameter_query, vector_search_sql(), {
        "embedding": json.dumps(vector), "paper_id": paper_id, "limit": cn.bounded_limit(limit, maximum=100),
    })
    result.update({"paper_id": paper_id, "vectorSource": vector_settings()})
    result["_execution"]["seedQuery"] = seed["_execution"]
    result["_execution"]["queryLanguage"] = "mmsql"
    for rank, row in enumerate(result["data"], 1):
        row.update({"rank": rank, "seed_paper_id": paper_id})
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool
async def expand_paper_citations(paper_id: str, direction: str = "both", limit: int = 20) -> str:
    """CN profile: query real references/citations from the same MMDB. Direction references returns seed->reference; citations returns citing->seed. No graph-service request is made."""
    if get_env("BIGDIPPER_DATA_PROFILE", "generic") != "cn":
        return json.dumps({"status": "error", "message": "Use general MMSQL with your schema's citation objects."})
    if direction not in {"references", "citations", "both"}:
        raise ValueError("direction must be references, citations, or both")
    predicates = {"references": "citing_paperid = %(paper_id)s", "citations": "cited_paperid = %(paper_id)s",
                  "both": "(citing_paperid = %(paper_id)s OR cited_paperid = %(paper_id)s)"}
    result = await asyncio.to_thread(_parameter_query,
        "SELECT DISTINCT citing_paperid, cited_paperid FROM cn_paper_ref WHERE " + predicates[direction]
        + " ORDER BY citing_paperid, cited_paperid LIMIT %(limit)s",
        {"paper_id": paper_id, "limit": cn.bounded_limit(limit, maximum=100)})
    result.update({"paper_id": paper_id, "direction": direction})
    return json.dumps(result, ensure_ascii=False, default=str)


# Reusable, tested CN queries are optional. The generic profile supports other
# schemas through introspection and free SQL/MMSQL without a table-name rewrite.
CN_TOOLS = (
    "get_paper_metadata_batch", "search_papers_by_keywords", "get_all_tables",
    "get_all_fields", "get_all_cn_institutions", "get_institution_id",
    "papertitle_2_paperid", "authorname_2_authorid", "ref_paper",
    "get_paper_author_affiliations_graph", "get_institution_field_author_rank",
    "get_institution_field_stats", "get_field_top_authors", "recommend_coauthers",
    "generate_institution_report", "generate_author_report", "analyze_cross_discipline",
    "get_hottest_subfields", "recommend_institution_experts_for_paper",
    "get_paper_institution_overlap_graph", "get_field_institution_collaboration_graph",
    "explain_field_top_authors", "search_papers_by_venue", "aggregate_papers_by_venue",
    "get_paper_venue",
)
if get_env("BIGDIPPER_DATA_PROFILE", "generic") == "cn":
    for name in CN_TOOLS:
        mcp.tool(getattr(cn, name))

if __name__ == "__main__":
    mcp.run(transport="stdio")
