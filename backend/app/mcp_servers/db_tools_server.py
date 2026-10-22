"""Optional separate-store DB tools; never imported by the main MMDB MCP server."""
from fastmcp import FastMCP
import uvicorn
import time
import json
import os
import re
import decimal
import asyncio
import threading
import psycopg2
import httpx
from typing import Any
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

MCP_API_KEY = os.getenv("MCP_API_KEY", "")


class RawAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        client_key = headers.get(b"x-internal-key", b"").decode("utf-8")
        if scope["method"] != "OPTIONS" and client_key != MCP_API_KEY:
            await self.send_401(send)
            return
        await self.app(scope, receive, send)

    async def send_401(self, send):
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail": "Unauthorized"}'})


class HelmdbDatabase:
    def __init__(self):
        self.connection_params = {
            "host": os.getenv("DB_HOST", ""),
            "port": int(os.getenv("DB_PORT", "5432")),
            "user": os.getenv("DB_USER", ""),
            "password": os.getenv("DB_PASSWORD", ""),
            "dbname": os.getenv("DB_NAME", ""),
            "options": "-c client_encoding=UTF8 -c default_transaction_read_only=on "
                f"-c statement_timeout={max(1, int(os.getenv('BIGDIPPER_DB_STATEMENT_TIMEOUT_MS', '60000')))}",
            "connect_timeout": max(1, int(os.getenv("BIGDIPPER_DB_CONNECT_TIMEOUT_SECONDS", "5"))),
            "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        }

    def get_connection(self):
        if not all(self.connection_params[key] for key in ("host", "user", "dbname")):
            raise RuntimeError("Configure DB_HOST, DB_USER and DB_NAME for your own database.")
        return psycopg2.connect(**self.connection_params)

    def _execute_sql_sync(self, sql: str):
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(sql)
            if cursor.description is None:
                if conn:
                    conn.commit()
                return {"status": "success", "columns": [], "row_count": 0, "data": []}
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            data = []
            for row in rows:
                item = {}
                for i, col in enumerate(columns):
                    value = row[i]
                    if hasattr(value, "isoformat"):
                        value = value.isoformat()
                    elif isinstance(value, decimal.Decimal):
                        value = float(value)
                    elif isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    item[col] = value
                data.append(item)
            return {"status": "success", "columns": columns, "row_count": len(data), "data": data}
        except Exception as e:
            return {"status": "error", "message": str(e), "sql": sql}
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    async def execute_sql_query(self, sql: str):
        return await asyncio.to_thread(self._execute_sql_sync, sql)


db = HelmdbDatabase()
mcp = FastMCP("BigDipper Scholar DB Tools alternative")

# Short-lived process-local cache for repeated venue/detail/author queries.
# The MCP server is long-lived under the research backend, while the TTL keeps
# interactive results fresh and can be disabled with a value of 0.
_QUERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_CACHE_MAX_ENTRIES = 256


def _query_cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("BIGDIPPER_MCP_QUERY_CACHE_TTL_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30.0


def _query_cache_get(key: str) -> dict[str, Any] | None:
    ttl = _query_cache_ttl()
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _QUERY_CACHE_LOCK:
        entry = _QUERY_CACHE.get(key)
        if entry is None:
            return None
        created, payload = entry
        if now - created >= ttl:
            _QUERY_CACHE.pop(key, None)
            return None
        return payload


def _query_cache_put(key: str, payload: dict[str, Any]) -> None:
    if _query_cache_ttl() <= 0:
        return
    with _QUERY_CACHE_LOCK:
        if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX_ENTRIES:
            oldest = min(_QUERY_CACHE, key=lambda item: _QUERY_CACHE[item][0])
            _QUERY_CACHE.pop(oldest, None)
        _QUERY_CACHE[key] = (time.monotonic(), payload)


def sql_text(value: str) -> str:
    return (value or "").strip().replace("'", "''")


def paper_node_expr(pid_expr: str) -> str:
    return f"substring({pid_expr} from 2)::bigint"


async def run_sql_tool(tool_name: str, sql: str, **extra):
    start_time = time.time()
    cache_key = f"{tool_name}\n{sql.strip()}"
    raw = _query_cache_get(cache_key)
    cache_hit = raw is not None
    if raw is None:
        raw = await db.execute_sql_query(sql)
        if raw.get("status") == "success":
            _query_cache_put(cache_key, raw)
    actual_time = time.time() - start_time
    execution = {
        "backend": "helmdb",
        "queryLanguage": "mmsql" if re.search(r"\bMATCH\s*\{", sql, re.IGNORECASE) else "sql",
        "queryText": sql.strip(),
        "databaseLatencyMs": round(actual_time * 1000, 2),
        "rowCount": raw.get("row_count", 0),
        "cacheHit": cache_hit,
    }
    if raw.get("status") != "success":
        return json.dumps(
            {"status": "error", "tool": tool_name, "message": raw.get("message"), "_execution": execution},
            ensure_ascii=False,
            indent=2,
        )
    response = {
        "status": "success",
        "tool": tool_name,
        "execute_time": f"{actual_time:.3f} seconds",
        "row_count": raw.get("row_count", 0),
        "_execution": execution,
        "data": raw.get("data", []),
    }
    response.update(extra)
    return json.dumps(response, ensure_ascii=False, indent=2)


async def run_candidate_graph_tool(tool_name: str, candidate_sql: str, graph_sql_builder: Any, **extra):
    started = time.time()
    candidate_started = time.time()
    candidate_raw = await db.execute_sql_query(candidate_sql)
    candidate_latency_ms = round((time.time() - candidate_started) * 1000, 2)
    if candidate_raw.get("status") != "success":
        execution = {
            "backend": "helmdb",
            "queryLanguage": "sql",
            "queryText": candidate_sql.strip(),
            "databaseLatencyMs": candidate_latency_ms,
            "rowCount": 0,
        }
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "stage": "candidate_query",
                "message": candidate_raw.get("message"),
                "_execution": execution,
            },
            ensure_ascii=False,
            indent=2,
        )

    candidate_rows = candidate_raw.get("data", [])
    paper_ids = [
        str(row.get("paperid"))
        for row in candidate_rows
        if re.fullmatch(r"W[0-9]+", str(row.get("paperid") or ""))
    ]
    if not paper_ids:
        execution = {
            "backend": "helmdb",
            "queryLanguage": "sql",
            "queryText": candidate_sql.strip(),
            "databaseLatencyMs": candidate_latency_ms,
            "rowCount": 0,
        }
        response = {
            "status": "success",
            "tool": tool_name,
            "execute_time": f"{time.time() - started:.3f} seconds",
            "row_count": 0,
            "candidate_count": 0,
            "candidate_query_latency_ms": candidate_latency_ms,
            "graph_query_latency_ms": 0.0,
            "_execution": execution,
            "data": [],
        }
        response.update(extra)
        return json.dumps(response, ensure_ascii=False, indent=2)

    graph_sql = graph_sql_builder(paper_ids, candidate_rows)
    graph_started = time.time()
    graph_raw = await db.execute_sql_query(graph_sql)
    graph_latency_ms = round((time.time() - graph_started) * 1000, 2)
    total_database_latency_ms = round(candidate_latency_ms + graph_latency_ms, 2)
    execution = {
        "backend": "helmdb",
        "queryLanguage": "mmsql",
        "queryText": candidate_sql.strip() + ";\n\n" + graph_sql.strip(),
        "databaseLatencyMs": total_database_latency_ms,
        "rowCount": graph_raw.get("row_count", 0),
    }
    if graph_raw.get("status") != "success":
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "stage": "graph_query",
                "message": graph_raw.get("message"),
                "candidate_count": len(paper_ids),
                "candidate_query_latency_ms": candidate_latency_ms,
                "graph_query_latency_ms": graph_latency_ms,
                "_execution": execution,
            },
            ensure_ascii=False,
            indent=2,
        )

    response = {
        "status": "success",
        "tool": tool_name,
        "execute_time": f"{time.time() - started:.3f} seconds",
        "row_count": graph_raw.get("row_count", 0),
        "candidate_count": len(paper_ids),
        "candidate_query_latency_ms": candidate_latency_ms,
        "graph_query_latency_ms": graph_latency_ms,
        "_execution": execution,
        "data": graph_raw.get("data", []),
    }
    response.update(extra)
    return json.dumps(response, ensure_ascii=False, indent=2)

READONLY_SQL_STARTS = {"select", "with", "explain"}
FORBIDDEN_SQL_TOKENS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "call",
    "do",
    "merge",
    "vacuum",
    "analyze",
    "set",
    "reset",
}


def bounded_limit(limit: int, default: int = 10, maximum: int = 1000) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def bounded_score(value: float, default: float = 0.3) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    return max(0.0, min(score, 1.0))


def normalized_year_range(year_from: int | None, year_to: int | None) -> tuple[int | None, int | None]:
    start_year = int(year_from) if year_from is not None else None
    end_year = int(year_to) if year_to is not None else None
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("year_from must be less than or equal to year_to")
    return start_year, end_year


def paper_year_predicates(alias: str, year_from: int | None, year_to: int | None) -> str:
    predicates: list[str] = []
    if year_from is not None:
        predicates.append(f"{alias}.year >= {year_from}")
    if year_to is not None:
        predicates.append(f"{alias}.year <= {year_to}")
    return "\n          AND " + "\n          AND ".join(predicates) if predicates else ""

def institution_field_candidate_sql(
    field_name: str,
    year_from: int | None,
    year_to: int | None,
    candidate_limit: int,
) -> str:
    if year_from is None and year_to is None:
        return f"""
        WITH TargetField AS (
            SELECT fieldid FROM cn_fields WHERE display_name = '{field_name}' LIMIT 1
        ),
        CandidatePaperIds AS (
            SELECT pf.paperid
            FROM cn_paperfields pf
            JOIN TargetField tf ON pf.fieldid = tf.fieldid
            WHERE pf.score_openalex > 0.3
              AND pf.paperid ~ '^W[0-9]+$'
            GROUP BY pf.paperid
            ORDER BY pf.paperid ASC
            LIMIT {candidate_limit}
        )
        SELECT cp.paperid, p.year, COALESCE(p.cited_by_count, 0) AS cited_by_count
        FROM CandidatePaperIds cp
        JOIN cn_papers p ON cp.paperid = p.paperid
        ORDER BY cp.paperid ASC
        """
    year_predicates = paper_year_predicates("p", year_from, year_to)
    return f"""
    WITH TargetField AS (
        SELECT fieldid FROM cn_fields WHERE display_name = '{field_name}' LIMIT 1
    )
    SELECT pf.paperid, p.year, COALESCE(p.cited_by_count, 0) AS cited_by_count
    FROM cn_paperfields pf
    JOIN TargetField tf ON pf.fieldid = tf.fieldid
    JOIN cn_papers p ON pf.paperid = p.paperid
    WHERE pf.score_openalex > 0.3
      AND pf.paperid ~ '^W[0-9]+$'{year_predicates}
    GROUP BY pf.paperid, p.year, p.cited_by_count
    ORDER BY pf.paperid ASC
    LIMIT {candidate_limit}
    """


def paper_seed_field_candidate_sql(
    paper_id: str,
    field_score_min: float,
    year_from: int | None,
    year_to: int | None,
    candidate_limit: int,
) -> str:
    field_ctes = f"""
    PaperFields AS (
        SELECT DISTINCT pf.fieldid, f.display_name, f.level, pf.score_openalex
        FROM cn_paperfields pf
        LEFT JOIN cn_fields f ON pf.fieldid = f.fieldid
        WHERE pf.paperid = '{paper_id}' AND pf.score_openalex >= {field_score_min}
        ORDER BY f.level DESC, pf.score_openalex DESC, pf.fieldid ASC
        LIMIT 1
    ),
    FieldSummary AS (
        SELECT COUNT(*) AS target_field_count,
               string_agg(fieldid::text, ',') AS target_field_ids,
               string_agg(display_name, ' | ') AS target_field_names
        FROM PaperFields
    )
    """
    if year_from is None and year_to is None:
        return f"""
        WITH {field_ctes},
        CandidatePaperIds AS (
            SELECT pf.paperid
            FROM cn_paperfields pf
            WHERE pf.fieldid IN (SELECT fieldid FROM PaperFields)
              AND pf.score_openalex >= {field_score_min}
              AND pf.paperid ~ '^W[0-9]+$'
            GROUP BY pf.paperid
            ORDER BY pf.paperid ASC
            LIMIT {candidate_limit}
        )
        SELECT cp.paperid, p.year, COALESCE(p.cited_by_count, 0) AS cited_by_count,
               fs.target_field_count, fs.target_field_ids, fs.target_field_names
        FROM CandidatePaperIds cp
        JOIN cn_papers p ON cp.paperid = p.paperid
        CROSS JOIN FieldSummary fs
        ORDER BY cp.paperid ASC
        """
    year_predicates = paper_year_predicates("p", year_from, year_to)
    return f"""
    WITH {field_ctes}
    SELECT pf.paperid, p.year, COALESCE(p.cited_by_count, 0) AS cited_by_count,
           fs.target_field_count, fs.target_field_ids, fs.target_field_names
    FROM cn_paperfields pf
    JOIN cn_papers p ON pf.paperid = p.paperid
    CROSS JOIN FieldSummary fs
    WHERE pf.fieldid IN (SELECT fieldid FROM PaperFields)
      AND pf.score_openalex >= {field_score_min}
      AND pf.paperid ~ '^W[0-9]+$'{year_predicates}
    GROUP BY pf.paperid, p.year, p.cited_by_count,
             fs.target_field_count, fs.target_field_ids, fs.target_field_names
    ORDER BY pf.paperid ASC
    LIMIT {candidate_limit}
    """


def normalize_readonly_sql(sql: str, limit: int) -> tuple[str | None, str | None]:
    query = (sql or "").strip()
    if not query:
        return None, "sql is required"
    while query.endswith(";"):
        query = query[:-1].strip()
    if ";" in query:
        return None, "multiple SQL statements are not allowed"
    first = query.split(None, 1)[0].lower() if query.split(None, 1) else ""
    if first not in READONLY_SQL_STARTS:
        return None, "execute_sciscinet_sql only SELECT/WITH/EXPLAIN read-only SQL is allowed"
    lowered_tokens = {token.strip('"`[]()').lower() for token in query.replace("\n", " ").split()}
    blocked = sorted(lowered_tokens & FORBIDDEN_SQL_TOKENS)
    if blocked:
        return None, f"read-only SQL cannot contain tokens: {', '.join(blocked)}"
    if first == "explain":
        return query, None
    safe_limit = bounded_limit(limit, default=100, maximum=1000)
    return f"SELECT * FROM (\n{query}\n) AS agent_query LIMIT {safe_limit};", None


def vector_config() -> dict[str, Any]:
    milvus_uri = os.getenv("BIGDIPPER_MILVUS_URI", "http://127.0.0.1:19531")
    parsed = urlparse(milvus_uri if "://" in milvus_uri else f"tcp://{milvus_uri}")
    return {
        "embedding_base_url": os.getenv("BIGDIPPER_EMBEDDING_BASE_URL", "http://127.0.0.1:18081").rstrip("/"),
        "milvus_uri": milvus_uri,
        "milvus_host": os.getenv("BIGDIPPER_MILVUS_HOST") or parsed.hostname or "127.0.0.1",
        "milvus_port": os.getenv("BIGDIPPER_MILVUS_PORT") or str(parsed.port or 19531),
        "milvus_collection": os.getenv("BIGDIPPER_MILVUS_COLLECTION", "sciscinet_cn_paper_embeddings_hnsw"),
        "vector_field": os.getenv("BIGDIPPER_MILVUS_VECTOR_FIELD", "embedding"),
        "paperid_field": os.getenv("BIGDIPPER_MILVUS_PAPERID_FIELD", "paperid"),
        "timeout": float(os.getenv("BIGDIPPER_VECTOR_TOOL_TIMEOUT_SECONDS", "60")),
    }


async def embed_natural_language_query(query: str) -> list[float]:
    text = (query or "").strip()
    if not text:
        raise RuntimeError("query is required")
    config = vector_config()
    async with httpx.AsyncClient(timeout=config["timeout"], trust_env=False) as client:
        response = await client.post(f"{config['embedding_base_url']}/embed", json={"query": text})
        response.raise_for_status()
        payload = response.json()
    embedding = payload.get("embedding") or payload.get("vector")
    if not isinstance(embedding, list):
        raise RuntimeError("embedding service response did not contain embedding")
    return [float(value) for value in embedding]


def _milvus_collection():
    try:
        from pymilvus import Collection, connections
    except Exception as exc:
        raise RuntimeError("pymilvus is not installed") from exc
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
    config = vector_config()
    alias = "sciscinet_mcp_vector"
    connections.connect(alias=alias, host=config["milvus_host"], port=config["milvus_port"])
    return Collection(config["milvus_collection"], using=alias)


def _milvus_hit_to_dict(hit: Any) -> dict[str, Any] | None:
    paperid_field = vector_config()["paperid_field"]
    if isinstance(hit, dict):
        entity = hit.get("entity") if isinstance(hit.get("entity"), dict) else {}
        paper_id = entity.get(paperid_field) or entity.get("paperid") or hit.get("id")
        score = hit.get("score", hit.get("distance"))
    else:
        entity = getattr(hit, "entity", {}) or {}
        paper_id = entity.get(paperid_field) if isinstance(entity, dict) else None
        score = getattr(hit, "score", getattr(hit, "distance", None))
    if not paper_id:
        return None
    item: dict[str, Any] = {"paperid": str(paper_id)}
    if score is not None:
        try:
            item["score"] = float(score)
        except (TypeError, ValueError):
            item["score"] = score
    return item


async def milvus_search_by_vector(vector: list[float], limit: int) -> list[dict[str, Any]]:
    config = vector_config()
    safe_limit = bounded_limit(limit, default=10, maximum=50)

    def search_sync() -> list[dict[str, Any]]:
        collection = _milvus_collection()
        results = collection.search(
            data=[vector],
            anns_field=config["vector_field"],
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=safe_limit,
            output_fields=[config["paperid_field"]],
            timeout=config["timeout"],
            consistency_level="Eventually",
        )
        hits = results[0] if results else []
        data: list[dict[str, Any]] = []
        for hit in hits:
            item = _milvus_hit_to_dict(hit)
            if item:
                data.append(item)
        return data

    return await asyncio.to_thread(search_sync)


def _escape_milvus_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def get_paper_embedding_from_milvus(paper_id: str) -> list[float]:
    pid = (paper_id or "").strip()
    if not pid:
        raise RuntimeError("paper_id is required")
    config = vector_config()

    def query_sync() -> list[float]:
        collection = _milvus_collection()
        rows = collection.query(
            expr=f'{config["paperid_field"]} == "{_escape_milvus_string(pid)}"',
            output_fields=[config["paperid_field"], config["vector_field"]],
            limit=1,
            timeout=config["timeout"],
            consistency_level="Eventually",
        )
        if not rows:
            raise RuntimeError(f"paper embedding not found for {pid}")
        vector = rows[0].get(config["vector_field"])
        if not isinstance(vector, list):
            raise RuntimeError(f"Milvus row for {pid} did not contain vector field {config['vector_field']}")
        return [float(value) for value in vector]

    return await asyncio.to_thread(query_sync)


def normalize_paper_id_list(paper_ids: Any, limit: int) -> list[str]:
    safe_limit = bounded_limit(limit, default=20, maximum=100)
    if isinstance(paper_ids, str):
        raw_values = [part.strip() for part in paper_ids.replace("\n", ",").split(",")]
    elif isinstance(paper_ids, (list, tuple)):
        raw_values = list(paper_ids)
    else:
        raw_values = []
    seen: set[str] = set()
    ids: list[str] = []
    for raw_value in raw_values:
        paper_id = sql_text(str(raw_value)).strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        ids.append(paper_id)
        if len(ids) >= safe_limit:
            break
    return ids


def paper_id_values_sql(paper_ids: list[str]) -> str:
    return ", ".join(f"('{paper_id}', {index})" for index, paper_id in enumerate(paper_ids, start=1))


def paper_metadata_batch_sql(paper_ids: list[str], limit: int) -> str:
    values = paper_id_values_sql(paper_ids)
    safe_limit = bounded_limit(limit, default=20, maximum=100)
    return f"""
    WITH input_papers(paperid, input_rank) AS (
        VALUES {values}
    ),
    field_labels AS (
        SELECT pf.paperid,
               string_agg(f.display_name, ' | ' ORDER BY pf.score_openalex DESC) AS fields
        FROM cn_paperfields pf
        JOIN cn_fields f ON pf.fieldid = f.fieldid
        WHERE pf.paperid IN (SELECT paperid FROM input_papers)
          AND pf.score_openalex > 0.3
        GROUP BY pf.paperid
    ),
    author_labels AS (
        SELECT paa.paperid,
               string_agg(DISTINCT COALESCE(ad.display_name, paa.authorid), ' | ' ORDER BY COALESCE(ad.display_name, paa.authorid)) AS authors
        FROM cn_paper_author_affiliation paa
        LEFT JOIN cn_author_details ad ON paa.authorid = ad.authorid
        WHERE paa.paperid IN (SELECT paperid FROM input_papers)
        GROUP BY paa.paperid
    ),
    institution_labels AS (
        SELECT paa.paperid,
               string_agg(DISTINCT COALESCE(aff.affiliation_name, paa.institutionid), ' | ' ORDER BY COALESCE(aff.affiliation_name, paa.institutionid)) AS institutions
        FROM cn_paper_author_affiliation paa
        LEFT JOIN cn_affiliations aff ON paa.institutionid = aff.institution_id
        WHERE paa.paperid IN (SELECT paperid FROM input_papers)
          AND paa.institutionid IS NOT NULL
        GROUP BY paa.paperid
    )
    SELECT ip.input_rank, pd.paperid, pd.doi, pd.title, pd.display_name, pd.year, pd.date,
           pd.doctype, pd.cited_by_count, pd.is_retracted, pd.is_paratext, pd.language,
           'https://openalex.org/works/' || pd.paperid AS openalex_url,
           public.inverted_index_to_text(pd.abstract_inverted_index) AS abstract,
           length(public.inverted_index_to_text(pd.abstract_inverted_index)) AS abstract_char_count,
           fl.fields, al.authors, il.institutions
    FROM input_papers ip
    JOIN cn_paperdetails pd ON pd.paperid = ip.paperid
    LEFT JOIN field_labels fl ON fl.paperid = ip.paperid
    LEFT JOIN author_labels al ON al.paperid = ip.paperid
    LEFT JOIN institution_labels il ON il.paperid = ip.paperid
    ORDER BY ip.input_rank
    LIMIT {safe_limit};
    """


async def fetch_paper_metadata_payload(paper_ids: Any, limit: int, tool_name: str = "get_paper_metadata_batch") -> dict[str, Any]:
    start_time = time.time()
    ids = normalize_paper_id_list(paper_ids, limit)
    if not ids:
        return {"status": "error", "tool": tool_name, "message": "paper_ids is required", "paperIds": []}
    sql = paper_metadata_batch_sql(ids, limit)
    database_started = time.time()
    raw = await db.execute_sql_query(sql)
    execution = {
        "backend": "helmdb",
        "queryLanguage": "sql",
        "queryText": sql.strip(),
        "databaseLatencyMs": round((time.time() - database_started) * 1000, 2),
        "rowCount": raw.get("row_count", 0),
    }
    if raw.get("status") != "success":
        return {
            "status": "error",
            "tool": tool_name,
            "message": raw.get("message"),
            "_execution": execution,
            "paperIds": ids,
        }
    return {
        "status": "success",
        "tool": tool_name,
        "execute_time": f"{time.time() - start_time:.3f} seconds",
        "row_count": raw.get("row_count", 0),
        "data": raw.get("data", []),
        "paperIds": ids,
        "_execution": execution,
        "limit": bounded_limit(limit, default=20, maximum=100),
    }


@mcp.tool
async def get_paper_metadata_batch(paper_ids: list[str], limit: int = 20) -> str:
    """语义分析核心工具：批量补全 paperid 的题名、摘要、年份、引用数、领域和作者，保持输入顺序。"""
    payload = await fetch_paper_metadata_payload(paper_ids, limit, tool_name="get_paper_metadata_batch")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def normalize_citation_direction(direction: str) -> str:
    value = (direction or "both").strip().lower()
    aliases = {
        "ref": "references",
        "reference": "references",
        "references": "references",
        "out": "references",
        "citation": "citations",
        "citations": "citations",
        "cited_by": "citations",
        "in": "citations",
        "both": "both",
    }
    if value not in aliases:
        raise RuntimeError("direction must be one of: references, citations, both")
    return aliases[value]


_NEO4J_REFERENCES_QUERY = """
MATCH (seed:Paper {paperid: $paperid})-[:CITES]->(p:Paper)
RETURN p.paperid AS paperid, p.title AS title, p.year AS year, p.doi AS doi,
       p.cited_by_count AS cited_by_count, 'reference' AS relation,
       seed.paperid AS sourcePaperId, p.paperid AS targetPaperId
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


_NEO4J_CITATIONS_QUERY = """
MATCH (p:Paper)-[:CITES]->(seed:Paper {paperid: $paperid})
RETURN p.paperid AS paperid, p.title AS title, p.year AS year, p.doi AS doi,
       p.cited_by_count AS cited_by_count, 'citation' AS relation,
       p.paperid AS sourcePaperId, seed.paperid AS targetPaperId
ORDER BY p.cited_by_count DESC
LIMIT $limit
"""


async def query_neo4j_citations(paper_id: str, direction: str = "both", limit: int = 10) -> list[dict[str, Any]]:
    pid = (paper_id or "").strip()
    if not pid:
        raise RuntimeError("paper_id is required")
    normalized_direction = normalize_citation_direction(direction)
    safe_limit = bounded_limit(limit, default=10, maximum=50)
    return await asyncio.to_thread(_query_neo4j_citations_sync, pid, normalized_direction, safe_limit)


def _query_neo4j_citations_sync(paper_id: str, direction: str, limit: int) -> list[dict[str, Any]]:
    enabled = (os.getenv("BIGDIPPER_NEO4J_ENABLED", "false") or "false").lower() == "true"
    if not enabled:
        raise RuntimeError("Neo4j citation expansion is disabled; set BIGDIPPER_NEO4J_ENABLED=true")
    try:
        from neo4j import GraphDatabase
    except Exception as exc:
        raise RuntimeError("neo4j Python driver is not installed") from exc
    uri = os.getenv("BIGDIPPER_NEO4J_URI", "bolt://127.0.0.1:7778")
    user = os.getenv("BIGDIPPER_NEO4J_USER", "neo4j")
    password = os.getenv("BIGDIPPER_NEO4J_PASSWORD", "")
    timeout = float(os.getenv("BIGDIPPER_NEO4J_TOOL_TIMEOUT_SECONDS", "5") or "5")
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=timeout)
    try:
        rows: list[dict[str, Any]] = []
        with driver.session() as session:
            if direction in {"references", "both"}:
                rows.extend(dict(record) for record in session.run(_NEO4J_REFERENCES_QUERY, {"paperid": paper_id, "limit": limit}))
            if direction in {"citations", "both"}:
                rows.extend(dict(record) for record in session.run(_NEO4J_CITATIONS_QUERY, {"paperid": paper_id, "limit": limit}))
        return dedupe_rows_by_paperid(rows)
    finally:
        driver.close()


def dedupe_rows_by_paperid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        paper_id = str(row.get("paperid") or "")
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        unique.append(row)
    return unique


@mcp.tool
async def expand_paper_citations_neo4j(paper_id: str, direction: str = "both", limit: int = 10) -> str:
    """语义分析扩展：从 Neo4j 对种子 paperid 做一跳 references/citations 扩展，适合替代慢的关系表引文扩展。"""
    start_time = time.time()
    pid = (paper_id or "").strip()
    safe_limit = bounded_limit(limit, default=10, maximum=50)
    try:
        normalized_direction = normalize_citation_direction(direction)
        data = await query_neo4j_citations(pid, normalized_direction, safe_limit)
        return json.dumps(
            {
                "status": "success",
                "tool": "expand_paper_citations_neo4j",
                "execute_time": f"{time.time() - start_time:.3f} seconds",
                "row_count": len(data),
                "data": data,
                "paper_id": pid,
                "direction": normalized_direction,
                "limit": safe_limit,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "tool": "expand_paper_citations_neo4j",
                "message": str(exc),
                "paper_id": pid,
                "direction": direction,
                "limit": safe_limit,
            },
            ensure_ascii=False,
            indent=2,
        )


def paper_ids_from_items(items: list[dict[str, Any]], limit: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        paper_id = str(item.get("paperid") or item.get("id") or "").strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        ids.append(paper_id)
        if len(ids) >= limit:
            break
    return ids


GENERIC_SEMANTIC_QUERY_TERMS = {
    "recommendation",
    "recommendations",
    "recommender",
    "neural",
    "network",
    "networks",
    "paper",
    "papers",
    "with",
    "using",
    "for",
    "and",
    "the",
}

SEMANTIC_DOMAIN_ANCHOR_TERMS = {
    "academic",
    "bibliographic",
    "citation",
    "citations",
    "graph",
    "graphs",
    "publication",
    "publications",
    "reference",
    "references",
    "scholarly",
    "scientific",
    "topic",
    "topics",
}

ACADEMIC_DOMAIN_ROW_TERMS = {
    "academic",
    "bibliographic",
    "citation",
    "citations",
    "publication",
    "publications",
    "reference",
    "references",
    "researcher",
    "researchers",
    "scholarly",
    "scientific",
}

LOW_VALUE_SEMANTIC_SEED_TERMS = {
    "front matter",
    "proceedings",
    "report on",
    "tutorial",
    "workshop",
}

HIGH_PRIORITY_ACADEMIC_ANCHOR_TERMS = {
    "academic",
    "bibliographic",
    "reference",
    "references",
    "scholarly",
    "scientific",
}

SEMANTIC_TASK_PROFILES = {
    "topic_overview": {"retrieval": 20, "metadata": 12, "expansion": 8, "citation": 4},
    "method_comparison": {"retrieval": 24, "metadata": 16, "expansion": 10, "citation": 4},
    "citation_context_analysis": {"retrieval": 16, "metadata": 14, "expansion": 6, "citation": 8},
    "gap_or_trend_analysis": {"retrieval": 24, "metadata": 16, "expansion": 8, "citation": 4},
}


def resolve_semantic_task_profile(task_family: str | None = None) -> dict[str, Any]:
    normalized = (task_family or "topic_overview").strip().lower().replace("-", "_")
    if normalized not in SEMANTIC_TASK_PROFILES:
        normalized = "topic_overview"
    return {"name": normalized, "limits": dict(SEMANTIC_TASK_PROFILES[normalized])}



def choose_semantic_seed(metadata_rows: list[dict[str, Any]], retrieved_ids: list[str], query: str | None = None) -> str | None:
    metadata_ids = {str(row.get("paperid") or "").strip() for row in metadata_rows}
    focus_terms = semantic_focus_terms(query or "")
    anchor_terms = semantic_anchor_terms(query or "")
    if focus_terms or anchor_terms:
        rank = {paper_id: index for index, paper_id in enumerate(retrieved_ids)}
        scored_rows: list[tuple[int, int, str]] = []
        for row in metadata_rows:
            paper_id = str(row.get("paperid") or "").strip()
            if not paper_id:
                continue
            score = semantic_seed_score(row, focus_terms, anchor_terms)
            if score > 0:
                scored_rows.append((score, -rank.get(paper_id, 999999), paper_id))
        if scored_rows:
            scored_rows.sort(reverse=True)
            return scored_rows[0][2]
    for paper_id in retrieved_ids:
        if paper_id in metadata_ids:
            return paper_id
    return retrieved_ids[0] if retrieved_ids else None


def semantic_focus_terms(query: str) -> list[str]:
    normalized = query.lower().replace("-", " ")
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if len(token) < 4 or token in GENERIC_SEMANTIC_QUERY_TERMS or token in SEMANTIC_DOMAIN_ANCHOR_TERMS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def semantic_anchor_terms(query: str) -> list[str]:
    normalized = query.lower().replace("-", " ")
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if token not in SEMANTIC_DOMAIN_ANCHOR_TERMS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def semantic_seed_score(row: dict[str, Any], focus_terms: list[str], anchor_terms: list[str]) -> int:
    text = semantic_row_text(row)
    title_text = semantic_title_text(row)
    score = semantic_focus_score(row, focus_terms) * 3
    score += semantic_focus_score_in_text(title_text, focus_terms) * 8
    score += semantic_focus_score(row, anchor_terms) * 2
    if anchor_terms and any(semantic_term_in_text(term, text) for term in ACADEMIC_DOMAIN_ROW_TERMS):
        score += 6
    if set(anchor_terms) & HIGH_PRIORITY_ACADEMIC_ANCHOR_TERMS and any(semantic_term_in_text(term, text) for term in ACADEMIC_DOMAIN_ROW_TERMS):
        score += 20
    if any(term in title_text for term in LOW_VALUE_SEMANTIC_SEED_TERMS):
        score -= 8
    return score


def semantic_focus_score(row: dict[str, Any], focus_terms: list[str]) -> int:
    return semantic_focus_score_in_text(semantic_row_text(row), focus_terms)


def semantic_focus_score_in_text(text: str, focus_terms: list[str]) -> int:
    return sum(1 for term in focus_terms if semantic_term_in_text(term, text))


def semantic_row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ["title", "display_name", "abstract", "fields"]).lower().replace("-", " ")


def semantic_title_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ["title", "display_name"]).lower().replace("-", " ")


def semantic_term_in_text(term: str, text: str) -> bool:
    if term in text:
        return True
    if term.endswith("s") and len(term) > 4 and term[:-1] in text:
        return True
    return False


def split_label_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value).split("|")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        item = str(raw_item).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def score_by_paper_id(items: list[dict[str, Any]]) -> dict[str, Any]:
    scores: dict[str, Any] = {}
    for item in items:
        paper_id = str(item.get("paperid") or item.get("id") or "").strip()
        if paper_id and "score" in item:
            scores[paper_id] = item.get("score")
    return scores


def relation_by_paper_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    relations: dict[str, dict[str, Any]] = {}
    for item in items:
        paper_id = str(item.get("paperid") or "").strip()
        if paper_id:
            relations[paper_id] = item
    return relations


def context_from_metadata(
    row: dict[str, Any],
    *,
    source: str,
    score: Any = None,
    relation: str | None = None,
    seed_paper_id: str | None = None,
) -> dict[str, Any]:
    title = row.get("title") or row.get("display_name") or ""
    abstract = str(row.get("abstract") or "").strip()
    text_value = "Abstract:\n" + abstract if abstract else ("Title:\n" + title if title else "")
    return {
        "paper_id": str(row.get("paperid") or ""),
        "title": title,
        "text": text_value,
        "source": source,
        "score": score,
        "year": row.get("year"),
        "citation_count": row.get("cited_by_count"),
        "authors": split_label_list(row.get("authors")),
        "institutions": split_label_list(row.get("institutions")),
        "fields": split_label_list(row.get("fields")),
        "relation": relation,
        "seed_paper_id": seed_paper_id,
    }


def build_semantic_contexts(
    initial_metadata: dict[str, Any],
    expansion_metadata: dict[str, Any],
    initial_vector_results: list[dict[str, Any]],
    similar_results: list[dict[str, Any]],
    citation_rows: list[dict[str, Any]],
    seed_paper_id: str | None,
) -> list[dict[str, Any]]:
    initial_scores = score_by_paper_id(initial_vector_results)
    similar_scores = score_by_paper_id(similar_results)
    citation_relations = relation_by_paper_id(citation_rows)
    similar_ids = {str(item.get("paperid") or "") for item in similar_results}
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in initial_metadata.get("data", []) if isinstance(initial_metadata, dict) else []:
        paper_id = str(row.get("paperid") or "")
        if not paper_id or paper_id in seen:
            continue
        contexts.append(context_from_metadata(row, source="initial", score=initial_scores.get(paper_id)))
        seen.add(paper_id)

    for row in expansion_metadata.get("data", []) if isinstance(expansion_metadata, dict) else []:
        paper_id = str(row.get("paperid") or "")
        if not paper_id or paper_id in seen:
            continue
        relation_row = citation_relations.get(paper_id, {})
        if paper_id in similar_ids:
            source = "similar_expansion"
            score = similar_scores.get(paper_id)
        else:
            source = "citation_expansion"
            score = None
        relation = relation_row.get("relation") if relation_row else None
        context_seed = seed_paper_id if source == "citation_expansion" else None
        contexts.append(context_from_metadata(row, source=source, score=score, relation=relation, seed_paper_id=context_seed))
        seen.add(paper_id)
    if seed_paper_id:
        contexts.sort(key=lambda context: 0 if context_paper_id_from_context(context) == seed_paper_id else 1)
    return contexts


def context_paper_id_from_context(context: dict[str, Any]) -> str:
    return str(context.get("paper_id") or context.get("paperid") or "").strip()


@mcp.tool
async def semantic_synthesis_seed_pipeline(
    query: str,
    retrieval_limit: int | None = None,
    metadata_limit: int | None = None,
    expansion_limit: int | None = None,
    citation_limit: int | None = None,
) -> str:
    """语义综述推荐入口：执行向量召回、批量元数据、相似论文和 Neo4j 引文一跳扩展。"""
    start_time = time.time()
    profile = resolve_semantic_task_profile(None)
    profile_limits = profile["limits"]
    safe_retrieval_limit = bounded_limit(retrieval_limit, default=profile_limits["retrieval"], maximum=50)
    safe_metadata_limit = bounded_limit(metadata_limit, default=profile_limits["metadata"], maximum=30)
    safe_expansion_limit = bounded_limit(expansion_limit, default=profile_limits["expansion"], maximum=30)
    safe_citation_limit = bounded_limit(citation_limit, default=profile_limits["citation"], maximum=20)
    text = (query or "").strip()
    if not text:
        return json.dumps({"status": "error", "tool": "semantic_synthesis_seed_pipeline", "message": "query is required"}, ensure_ascii=False, indent=2)

    try:
        embedding = await embed_natural_language_query(text)
        retrieved = await milvus_search_by_vector(embedding, safe_retrieval_limit)
    except Exception as exc:
        return json.dumps({"status": "error", "tool": "semantic_synthesis_seed_pipeline", "stage": "initial_vector_search", "message": str(exc)}, ensure_ascii=False, indent=2)

    retrieved_ids = paper_ids_from_items(retrieved, safe_metadata_limit)
    initial_metadata = await fetch_paper_metadata_payload(retrieved_ids, safe_metadata_limit, tool_name="get_paper_metadata_batch") if retrieved_ids else {"status": "error", "message": "no retrieved paper ids", "data": []}
    seed_paper_id = choose_semantic_seed(initial_metadata.get("data", []), retrieved_ids, query=text)

    similar: list[dict[str, Any]] = []
    similar_error: str | None = None
    citation_rows: list[dict[str, Any]] = []
    citation_error: str | None = None
    if seed_paper_id:
        try:
            seed_embedding = await get_paper_embedding_from_milvus(seed_paper_id)
            similar_candidates = await milvus_search_by_vector(seed_embedding, safe_expansion_limit + 1)
            similar = [item for item in similar_candidates if item.get("paperid") != seed_paper_id][:safe_expansion_limit]
        except Exception as exc:
            similar_error = str(exc)
        try:
            citation_rows = await query_neo4j_citations(seed_paper_id, "both", safe_citation_limit)
        except Exception as exc:
            citation_error = str(exc)

    similar_ids = paper_ids_from_items(similar, safe_expansion_limit)
    citation_ids = paper_ids_from_items(citation_rows, safe_citation_limit * 2)
    expansion_ids = paper_ids_from_items([{"paperid": paper_id} for paper_id in [*similar_ids, *citation_ids] if paper_id not in set(retrieved_ids)], safe_metadata_limit)
    expansion_metadata = await fetch_paper_metadata_payload(expansion_ids, safe_metadata_limit, tool_name="get_paper_metadata_batch") if expansion_ids else {"status": "success", "row_count": 0, "data": [], "paperIds": []}

    return json.dumps(
        {
            "status": "success",
            "tool": "semantic_synthesis_seed_pipeline",
            "execute_time": f"{time.time() - start_time:.3f} seconds",
            "query": text,
            "semanticProfile": profile["name"],
            "effectiveLimits": {
                "retrieval": safe_retrieval_limit,
                "metadata": safe_metadata_limit,
                "expansion": safe_expansion_limit,
                "citation": safe_citation_limit,
            },
            "retrievedPaperIds": retrieved_ids,
            "initialVectorResults": retrieved,
            "initialMetadata": initial_metadata,
            "seedPaperId": seed_paper_id,
            "similarPaperIds": similar_ids,
            "similarVectorResults": {"status": "error", "message": similar_error, "data": []} if similar_error else {"status": "success", "row_count": len(similar), "data": similar},
            "citationPaperIds": citation_ids,
            "citationExpansion": {"status": "error", "message": citation_error, "data": []} if citation_error else {"status": "success", "row_count": len(citation_rows), "data": citation_rows},
            "expansionMetadata": expansion_metadata,
            "contexts": build_semantic_contexts(initial_metadata, expansion_metadata, retrieved, similar, citation_rows, seed_paper_id),
            "recommendedReportUse": "Select this observation when the user asks for semantic synthesis, literature overview, topic comparison, or related-paper analysis.",
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool
async def execute_sciscinet_sql(sql: str, limit: int = 100) -> str:
    """通用只读 SQL：在 sciscinet_cn 库执行 SELECT/WITH/EXPLAIN 查询，自动限制返回行数。"""
    normalized_sql, error = normalize_readonly_sql(sql, limit)
    if error:
        return json.dumps({"status": "error", "tool": "execute_sciscinet_sql", "message": error}, ensure_ascii=False, indent=2)
    return await run_sql_tool("execute_sciscinet_sql", normalized_sql or sql, original_sql=(sql or "").strip(), limit=bounded_limit(limit, default=100, maximum=1000))


@mcp.tool
async def search_papers_by_natural_language(query: str, limit: int = 10) -> str:
    """向量召回：输入英文自然语言 query，通过 SPECTER2 embedding + SciSciNetV2 Milvus 返回相关 paperid。"""
    start_time = time.time()
    safe_limit = bounded_limit(limit, default=10, maximum=50)
    try:
        embedding = await embed_natural_language_query(query)
        data = await milvus_search_by_vector(embedding, safe_limit)
        return json.dumps(
            {
                "status": "success",
                "tool": "search_papers_by_natural_language",
                "execute_time": f"{time.time() - start_time:.3f} seconds",
                "row_count": len(data),
                "data": data,
                "query": (query or "").strip(),
                "limit": safe_limit,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"status": "error", "tool": "search_papers_by_natural_language", "message": str(exc)}, ensure_ascii=False, indent=2)


@mcp.tool
async def find_similar_papers_by_paperid(paper_id: str, limit: int = 10) -> str:
    """向量召回：输入种子 paperid，从 Milvus 读取论文向量并返回相似论文 paperid。"""
    start_time = time.time()
    pid = (paper_id or "").strip()
    safe_limit = bounded_limit(limit, default=10, maximum=50)
    try:
        embedding = await get_paper_embedding_from_milvus(pid)
        candidates = await milvus_search_by_vector(embedding, safe_limit + 1)
        data = [item for item in candidates if item.get("paperid") != pid][:safe_limit]
        return json.dumps(
            {
                "status": "success",
                "tool": "find_similar_papers_by_paperid",
                "execute_time": f"{time.time() - start_time:.3f} seconds",
                "row_count": len(data),
                "data": data,
                "paper_id": pid,
                "limit": safe_limit,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"status": "error", "tool": "find_similar_papers_by_paperid", "message": str(exc), "paper_id": pid}, ensure_ascii=False, indent=2)


def optional_year_filter_sql(year_from: int | None, year_to: int | None) -> tuple[str, dict[str, Any]]:
    filters: list[str] = []
    metadata: dict[str, Any] = {}
    if year_from is not None:
        try:
            start_year = int(year_from)
            filters.append(f"pd.year >= {start_year}")
            metadata["yearFrom"] = start_year
        except (TypeError, ValueError):
            pass
    if year_to is not None:
        try:
            end_year = int(year_to)
            filters.append(f"pd.year <= {end_year}")
            metadata["yearTo"] = end_year
        except (TypeError, ValueError):
            pass
    return (" AND " + " AND ".join(filters)) if filters else "", metadata


def optional_field_filter_sql(field_name: str | None) -> tuple[str, str | None]:
    field = sql_text(field_name or "")
    if not field:
        return "", None
    return (
        f"""
    AND EXISTS (
        SELECT 1
        FROM cn_paperfields pf
        JOIN cn_fields f ON pf.fieldid = f.fieldid
        WHERE pf.paperid = pd.paperid
          AND f.display_name ILIKE '%{field}%'
          AND pf.score_openalex > 0.2
    )
""",
        field,
    )


def keyword_paper_search_sql(query: str, year_from: int | None, year_to: int | None, limit: int, field_name: str | None = None) -> tuple[str, dict[str, Any]]:
    text = sql_text(query)
    safe_limit = bounded_limit(limit, default=20, maximum=100)
    year_filter, year_metadata = optional_year_filter_sql(year_from, year_to)
    field_filter, safe_field = optional_field_filter_sql(field_name)
    sql = f"""
    WITH input AS (
        SELECT
            '{text}'::text AS raw_query,
            plainto_tsquery('english', '{text}') AS tsq
    ),
    matched AS (
        SELECT
            pd.paperid, pd.doi, pd.title, pd.display_name, pd.year, pd.date,
            pd.doctype, pd.cited_by_count, pd.language, pd.abstract_inverted_index,
            'https://openalex.org/works/' || pd.paperid AS openalex_url,
            ts_rank_cd(to_tsvector('english', pd.title), input.tsq) AS title_rank
        FROM input, cn_paperdetails pd
        WHERE to_tsvector('english', pd.title) @@ input.tsq
          {year_filter}
          {field_filter}
        ORDER BY pd.cited_by_count DESC NULLS LAST, title_rank DESC
        LIMIT {safe_limit}
    ),
    author_labels AS (
        SELECT paa.paperid,
               string_agg(DISTINCT COALESCE(ad.display_name, paa.authorid), ' | ' ORDER BY COALESCE(ad.display_name, paa.authorid)) AS authors
        FROM cn_paper_author_affiliation paa
        LEFT JOIN cn_author_details ad ON paa.authorid = ad.authorid
        WHERE paa.paperid IN (SELECT paperid FROM matched)
        GROUP BY paa.paperid
    ),
    institution_labels AS (
        SELECT paa.paperid,
               string_agg(DISTINCT COALESCE(aff.affiliation_name, paa.institutionid), ' | ' ORDER BY COALESCE(aff.affiliation_name, paa.institutionid)) AS institutions
        FROM cn_paper_author_affiliation paa
        LEFT JOIN cn_affiliations aff ON paa.institutionid = aff.institution_id
        WHERE paa.paperid IN (SELECT paperid FROM matched)
          AND paa.institutionid IS NOT NULL
        GROUP BY paa.paperid
    )
    SELECT m.paperid, m.doi, m.title, m.display_name, m.year, m.date, m.doctype,
           m.cited_by_count, m.language, m.openalex_url, m.title_rank,
           al.authors, il.institutions,
           public.inverted_index_to_text(m.abstract_inverted_index) AS abstract,
           length(public.inverted_index_to_text(m.abstract_inverted_index)) AS abstract_char_count
    FROM matched m
    LEFT JOIN author_labels al ON al.paperid = m.paperid
    LEFT JOIN institution_labels il ON il.paperid = m.paperid
    ORDER BY m.cited_by_count DESC NULLS LAST, m.title_rank DESC;
    """
    metadata = {"query": text, "limit": safe_limit, **year_metadata}
    if safe_field:
        metadata["fieldName"] = safe_field
    return sql, metadata


@mcp.tool
async def search_papers_by_keywords(
    query: str,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 20,
    field_name: str | None = None,
) -> str:
    """关键词召回：使用 cn_paperdetails.title 的全文 GIN 索引检索精确子领域论文，并返回作者、机构、摘要和引用数。适合 GNN/RAG/Graph NAS 等小领域；field_name 只能作为辅助过滤。"""
    text = (query or "").strip()
    if not text:
        return json.dumps({"status": "error", "tool": "search_papers_by_keywords", "message": "query is required"}, ensure_ascii=False, indent=2)
    sql, metadata = keyword_paper_search_sql(text, year_from, year_to, limit, field_name=field_name)
    return await run_sql_tool("search_papers_by_keywords", sql, match_strategy="indexed_title_full_text", **metadata)


@mcp.tool
async def get_all_tables() -> str:
    """元数据：查询 public schema 下所有表。"""
    sql = """
    SELECT table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name;
    """
    return await run_sql_tool("get_all_tables", sql)


@mcp.tool
async def get_all_fields() -> str:
    """元数据：查询所有学科领域。"""
    sql = """
    SELECT id, fieldid, display_name, level, works_count, cited_by_count
    FROM cn_fields
    ORDER BY level, display_name;
    """
    return await run_sql_tool("get_all_fields", sql)


@mcp.tool
async def get_all_cn_institutions() -> str:
    """元数据：查询所有中国机构名称。"""
    sql = """
    SELECT DISTINCT affiliation_name
    FROM cn_affiliations
    ORDER BY affiliation_name;
    """
    return await run_sql_tool("get_all_cn_institutions", sql)


@mcp.tool
async def get_institution_id(institution_name: str) -> str:
    """元数据：通过机构英文名模糊查询机构 ID。"""
    inst = sql_text(institution_name)
    sql = f"""
    SELECT institution_id, affiliation_name, country_code, h_index, productivity, homepage_url,
           CASE WHEN ror IS NOT NULL THEN 'https://ror.org/' || ror ELSE NULL END AS ror_url,
           'https://openalex.org/institutions/' || institution_id AS openalex_url
    FROM cn_affiliations
    WHERE affiliation_name ILIKE '%{inst}%'
    ORDER BY h_index DESC
    LIMIT 10;
    """
    return await run_sql_tool("get_institution_id", sql, institution_name=inst)


@mcp.tool
async def papertitle_2_paperid(paper_title: str) -> str:
    """关键元数据：论文标题 -> paperid。使用 CN 库标题全文 GIN 索引，并返回摘要文本。"""
    title = sql_text(paper_title)
    sql = f"""
    WITH input AS (
        SELECT
            '{title}'::text AS raw_title,
            regexp_replace(lower('{title}'), '[^a-z0-9]+', ' ', 'g') AS norm_title,
            plainto_tsquery('english', '{title}') AS tsq
    ),
    matches AS (
        SELECT
            pd.paperid,
            pd.doi,
            pd.title,
            pd.display_name,
            pd.year,
            pd.date,
            pd.doctype,
            pd.cited_by_count,
            pd.is_retracted,
            pd.is_paratext,
            pd.cited_by_api_url,
            pd.language,
            pd.abstract_inverted_index,
            'https://openalex.org/works/' || pd.paperid AS openalex_url,
            (regexp_replace(lower(pd.title), '[^a-z0-9]+', ' ', 'g') = input.norm_title) AS exact_normalized_match,
            ts_rank_cd(to_tsvector('english', pd.title), input.tsq) AS rank
        FROM input, cn_paperdetails pd
        WHERE to_tsvector('english', pd.title) @@ input.tsq
        ORDER BY exact_normalized_match DESC, pd.cited_by_count DESC NULLS LAST, rank DESC
        LIMIT 10
    )
    SELECT
        paperid, doi, title, display_name, year, date, doctype, cited_by_count,
        is_retracted, is_paratext, cited_by_api_url, language, openalex_url,
        exact_normalized_match, rank,
        public.inverted_index_to_text(abstract_inverted_index) AS abstract,
        length(public.inverted_index_to_text(abstract_inverted_index)) AS abstract_char_count
    FROM matches;
    """
    return await run_sql_tool("papertitle_2_paperid", sql, paper_title=title, match_strategy="full_text_gin_with_abstract")


@mcp.tool
async def authorname_2_authorid(author_name: str) -> str:
    """元数据：根据作者姓名前缀查询 authorid，并返回主要机构。"""
    name = sql_text(author_name)
    sql = f"""
    WITH MatchedAuthors AS (
        SELECT ad.authorid, ad.display_name, ad.cited_by_count, ad.orcid, a.h_index
        FROM cn_author_details ad
        LEFT JOIN cn_authors a ON ad.authorid = a.authorid
        WHERE ad.display_name ILIKE '{name}%'
        ORDER BY ad.cited_by_count DESC
        LIMIT 20
    ),
    AuthorInstitutions AS (
        SELECT paa.authorid, aff.affiliation_name, COUNT(DISTINCT paa.paperid) AS paper_count,
               ROW_NUMBER() OVER (PARTITION BY paa.authorid ORDER BY COUNT(DISTINCT paa.paperid) DESC) AS rn
        FROM cn_paper_author_affiliation paa
        JOIN cn_affiliations aff ON paa.institutionid = aff.institution_id
        WHERE paa.authorid IN (SELECT authorid FROM MatchedAuthors)
        GROUP BY paa.authorid, aff.affiliation_name
    )
    SELECT ma.authorid, ma.display_name, ma.cited_by_count, ma.h_index, ma.orcid,
           ai.affiliation_name AS main_institution,
           'https://openalex.org/authors/' || ma.authorid AS openalex_url
    FROM MatchedAuthors ma
    LEFT JOIN AuthorInstitutions ai ON ma.authorid = ai.authorid AND ai.rn = 1
    ORDER BY ma.cited_by_count DESC
    LIMIT 10;
    """
    return await run_sql_tool("authorname_2_authorid", sql, author_name=name)


@mcp.tool
async def ref_paper(paper_id: str) -> str:
    """元数据/引文：查询某篇论文的被引文献。当前使用关系表，适合索引查询。"""
    pid = sql_text(paper_id)
    sql = f"""
    SELECT cp.citing_paperid AS paperid, pd.title, pd.year, pd.cited_by_count, pd.doi,
           'https://openalex.org/works/' || cp.citing_paperid AS openalex_url
    FROM cn_paper_ref cp
    JOIN cn_paperdetails pd ON cp.citing_paperid = pd.paperid
    WHERE cp.cited_paperid = '{pid}'
    ORDER BY pd.cited_by_count DESC
    LIMIT 50;
    """
    return await run_sql_tool("ref_paper", sql, paper_id=pid)


@mcp.tool
async def get_paper_author_affiliations_graph(paper_id: str) -> str:
    """MMSQL 单跳：查询论文对应的作者和机构事实节点。"""
    pid = sql_text(paper_id)
    sql = f"""
    WITH TargetPaper AS (
        SELECT '{pid}'::text AS paperid, substring('{pid}' from 2)::bigint AS paper_node_id
    ),
    PaperAuthorAffiliations AS (
        SELECT DISTINCT
            'W' || pnode.id::text AS paperid,
            paa.properties->>'authorid' AS authorid,
            paa.properties->>'institutionid' AS institutionid,
            paa.properties->>'author_position' AS author_position
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM TargetPaper)
    )
    SELECT paa.paperid, paa.authorid, ad.display_name AS author_name,
           paa.institutionid, aff.affiliation_name, paa.author_position
    FROM PaperAuthorAffiliations paa
    LEFT JOIN cn_author_details ad ON paa.authorid = ad.authorid
    LEFT JOIN cn_affiliations aff ON paa.institutionid = aff.institution_id
    ORDER BY CASE WHEN paa.author_position !~ '[^0-9]' THEN paa.author_position::int ELSE 999999 END, ad.cited_by_count DESC
    LIMIT 100;
    """
    return await run_sql_tool("get_paper_author_affiliations_graph", sql, paper_id=pid)


@mcp.tool
async def get_institution_field_author_rank(
    institution_id: str,
    field_name: str,
    year_from: int | None = None,
    year_to: int | None = None,
    candidate_limit: int = 1000,
    top_k: int = 20,
    min_papers: int = 2,
) -> str:
    """MMSQL：查询某机构在指定领域和年份窗口内的作者排名。"""
    inst = sql_text(institution_id)
    field = sql_text(field_name)
    try:
        start_year, end_year = normalized_year_range(year_from, year_to)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {"status": "error", "tool": "get_institution_field_author_rank", "message": str(exc)},
            ensure_ascii=False,
            indent=2,
        )
    safe_candidate_limit = bounded_limit(candidate_limit, default=1000, maximum=1000)
    safe_top_k = bounded_limit(top_k, default=20, maximum=100)
    safe_min_papers = bounded_limit(min_papers, default=2, maximum=100)
    candidate_sql = institution_field_candidate_sql(
        field,
        start_year,
        end_year,
        safe_candidate_limit,
    )

    def graph_sql_builder(paper_ids: list[str], _candidate_rows: list[dict[str, Any]]) -> str:
        node_ids = ", ".join(paper_id[1:] for paper_id in paper_ids)
        return f"""
        WITH TargetInst AS (
            SELECT institution_id FROM cn_affiliations WHERE institution_id = '{inst}' LIMIT 1
        ),
        GraphInstAuthorPapers AS (
            SELECT DISTINCT paa.properties->>'authorid' AS authorid, 'W' || pnode.id::text AS paperid
            FROM cn_writen_by_network
            MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
            WHERE pnode.id IN ({node_ids})
              AND paa.properties->>'institutionid' IN (SELECT institution_id FROM TargetInst)
        ),
        AuthorStats AS (
            SELECT giap.authorid, COUNT(DISTINCT giap.paperid) AS paper_count,
                   SUM(COALESCE(p.cited_by_count, 0)) AS total_citations,
                   ROUND(AVG(COALESCE(p.cited_by_count, 0))::numeric, 2) AS avg_citations,
                   MAX(COALESCE(p.cited_by_count, 0)) AS max_citations
            FROM GraphInstAuthorPapers giap
            JOIN cn_papers p ON giap.paperid = p.paperid
            GROUP BY giap.authorid
            HAVING COUNT(DISTINCT giap.paperid) >= {safe_min_papers}
        )
        SELECT ROW_NUMBER() OVER (
                   ORDER BY s.total_citations DESC, s.paper_count DESC, s.authorid ASC
               ) AS rank,
               ad.display_name AS author_name, ad.authorid, s.paper_count, s.total_citations,
               s.avg_citations, s.max_citations, a.h_index AS author_h_index, a.productivity,
               ad.orcid, {len(paper_ids)} AS candidate_paper_count,
               'https://openalex.org/authors/' || ad.authorid AS openalex_url
        FROM AuthorStats s
        JOIN cn_author_details ad ON s.authorid = ad.authorid
        LEFT JOIN cn_authors a ON s.authorid = a.authorid
        ORDER BY s.total_citations DESC, s.paper_count DESC, s.authorid ASC
        LIMIT {safe_top_k};
        """

    return await run_candidate_graph_tool(
        "get_institution_field_author_rank",
        candidate_sql,
        graph_sql_builder,
        institution_id=inst,
        field_name=field,
        year_from=start_year,
        year_to=end_year,
        candidate_limit=safe_candidate_limit,
        top_k=safe_top_k,
        min_papers=safe_min_papers,
        candidate_order="paperid ASC",
    )


@mcp.tool
async def get_institution_field_stats(field_name: str) -> str:
    """MMSQL：按领域统计中国机构发文和被引。bench 默认取领域前 1000 篇候选论文。"""
    field = sql_text(field_name)
    sql = f"""
    WITH TargetField AS (
        SELECT fieldid FROM cn_fields WHERE display_name = '{field}' LIMIT 1
    ),
    FieldPapers AS (
        SELECT DISTINCT pf.paperid
        FROM cn_paperfields pf JOIN TargetField tf ON pf.fieldid = tf.fieldid
        WHERE pf.score_openalex > 0.3
        LIMIT 1000
    ),
    FieldPaperIds AS (
        SELECT substring(paperid from 2)::bigint AS paper_node_id FROM FieldPapers
    ),
    GraphInstitutionPapers AS (
        SELECT DISTINCT paa.properties->>'institutionid' AS institutionid, 'W' || pnode.id::text AS paperid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM FieldPaperIds)
    ),
    InstitutionStats AS (
        SELECT gip.institutionid, COUNT(DISTINCT gip.paperid) AS paper_count,
               SUM(p.cited_by_count) AS total_citations,
               ROUND(AVG(p.cited_by_count)::numeric, 2) AS avg_citations,
               MAX(p.cited_by_count) AS max_citations
        FROM GraphInstitutionPapers gip
        JOIN cn_papers p ON gip.paperid = p.paperid
        GROUP BY gip.institutionid
        HAVING COUNT(DISTINCT gip.paperid) >= 5
    )
    SELECT a.affiliation_name AS institution_name, a.country_code, s.paper_count,
           s.total_citations, s.avg_citations, s.max_citations, a.h_index AS institution_h_index,
           a.productivity, 'https://openalex.org/institutions/' || a.institution_id AS openalex_url
    FROM InstitutionStats s
    JOIN cn_affiliations a ON s.institutionid = a.institution_id
    ORDER BY s.total_citations DESC
    LIMIT 30;
    """
    return await run_sql_tool("get_institution_field_stats", sql, field_name=field, paper_limit=1000)


@mcp.tool
async def get_field_top_authors(field_name: str) -> str:
    """MMSQL：查询某领域高被引作者。bench 默认取领域前 1000 篇候选论文。"""
    field = sql_text(field_name)
    sql = f"""
    WITH TargetField AS (
        SELECT fieldid FROM cn_fields WHERE display_name = '{field}' LIMIT 1
    ),
    FieldPapers AS (
        SELECT DISTINCT pf.paperid
        FROM cn_paperfields pf JOIN TargetField tf ON pf.fieldid = tf.fieldid
        WHERE pf.score_openalex > 0.3
        LIMIT 1000
    ),
    FieldPaperIds AS (
        SELECT substring(paperid from 2)::bigint AS paper_node_id FROM FieldPapers
    ),
    PaperAuthors AS (
        SELECT DISTINCT 'W' || pnode.id::text AS paperid, paa.properties->>'authorid' AS authorid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM FieldPaperIds)
    )
    SELECT ad.display_name AS author_name, ad.authorid, ad.last_known_institution,
           COUNT(DISTINCT pa.paperid) AS paper_count, SUM(p.cited_by_count) AS total_citations,
           a.h_index, a.productivity, ad.orcid,
           'https://openalex.org/authors/' || ad.authorid AS openalex_url
    FROM PaperAuthors pa
    JOIN cn_papers p ON pa.paperid = p.paperid
    JOIN cn_author_details ad ON pa.authorid = ad.authorid
    LEFT JOIN cn_authors a ON pa.authorid = a.authorid
    GROUP BY ad.display_name, ad.authorid, ad.last_known_institution, a.h_index, a.productivity, ad.orcid
    HAVING COUNT(DISTINCT pa.paperid) >= 3
    ORDER BY total_citations DESC
    LIMIT 30;
    """
    return await run_sql_tool("get_field_top_authors", sql, field_name=field, paper_limit=1000)


@mcp.tool
async def recommend_coauthers(paper_id: str) -> str:
    """MMSQL：基于目标论文领域推荐潜在合作者。bench 默认取 1000 篇同领域候选论文。"""
    pid = sql_text(paper_id)
    sql = f"""
    WITH TargetPaper AS (
        SELECT '{pid}'::text AS paperid, substring('{pid}' from 2)::bigint AS paper_node_id
    ),
    PaperFields AS (
        SELECT DISTINCT fieldid
        FROM cn_paperfields
        WHERE paperid IN (SELECT paperid FROM TargetPaper) AND score_openalex > 0.3
    ),
    PaperAuthors AS (
        SELECT DISTINCT paa.properties->>'authorid' AS authorid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM TargetPaper)
    ),
    FieldPapers AS (
        SELECT DISTINCT pf.paperid
        FROM cn_paperfields pf
        WHERE pf.fieldid IN (SELECT fieldid FROM PaperFields) AND pf.score_openalex > 0.3
        LIMIT 1000
    ),
    FieldPaperIds AS (
        SELECT substring(paperid from 2)::bigint AS paper_node_id FROM FieldPapers
    ),
    CandidateAuthorPapers AS (
        SELECT DISTINCT paa.properties->>'authorid' AS authorid, 'W' || pnode.id::text AS paperid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM FieldPaperIds)
    ),
    Candidates AS (
        SELECT cap.authorid, COUNT(DISTINCT cap.paperid) AS matched
        FROM CandidateAuthorPapers cap
        WHERE cap.authorid NOT IN (SELECT authorid FROM PaperAuthors)
        GROUP BY cap.authorid
        HAVING COUNT(DISTINCT cap.paperid) >= 3
        LIMIT 100
    )
    SELECT ad.authorid, ad.display_name, ad.last_known_institution, ad.cited_by_count,
           c.matched, ad.orcid, 'https://openalex.org/authors/' || ad.authorid AS openalex_url
    FROM Candidates c
    JOIN cn_author_details ad ON c.authorid = ad.authorid
    ORDER BY ad.cited_by_count DESC
    LIMIT 20;
    """
    return await run_sql_tool("recommend_coauthers", sql, paper_id=pid, field_paper_limit=1000)


@mcp.tool
async def generate_institution_report(institution_id: str) -> str:
    """MMSQL：生成机构报告。bench 默认抽取该机构 100 篇论文进入图查询。"""
    iid = sql_text(institution_id)
    sql = f"""
    WITH TargetInst AS (SELECT '{iid}'::text AS institution_id),
    InstitutionPaperIds AS (
        SELECT DISTINCT paperid
        FROM cn_paper_author_affiliation
        WHERE institutionid IN (SELECT institution_id FROM TargetInst) AND paperid IS NOT NULL
        LIMIT 100
    ),
    InstitutionPaperNodeIds AS (
        SELECT substring(paperid from 2)::bigint AS paper_node_id FROM InstitutionPaperIds
    ),
    GraphInstitutionPapers AS (
        SELECT DISTINCT 'W' || pnode.id::text AS paperid,
               paa.properties->>'authorid' AS authorid,
               paa.properties->>'institutionid' AS institutionid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM InstitutionPaperNodeIds)
          AND paa.properties->>'institutionid' IN (SELECT institution_id FROM TargetInst)
    ),
    InstitutionPapers AS (
        SELECT gip.paperid, gip.authorid, p.cited_by_count, p.year
        FROM GraphInstitutionPapers gip JOIN cn_papers p ON gip.paperid = p.paperid
    ),
    TopFields AS (
        SELECT f.display_name, COUNT(DISTINCT ip.paperid) AS papers
        FROM InstitutionPapers ip
        JOIN cn_paperfields pf ON ip.paperid = pf.paperid
        JOIN cn_fields f ON pf.fieldid = f.fieldid
        WHERE pf.score_openalex > 0.3
        GROUP BY f.display_name
        ORDER BY papers DESC
        LIMIT 10
    ),
    TopAuthors AS (
        SELECT ad.display_name, COUNT(DISTINCT ip.paperid) AS papers, SUM(ip.cited_by_count) AS cite
        FROM InstitutionPapers ip
        JOIN cn_author_details ad ON ip.authorid = ad.authorid
        GROUP BY ad.display_name
        ORDER BY cite DESC
        LIMIT 10
    )
    SELECT json_build_object(
        'institution_info', (SELECT row_to_json(t) FROM (
            SELECT institution_id, affiliation_name, country_code, h_index
            FROM cn_affiliations WHERE institution_id IN (SELECT institution_id FROM TargetInst)
        ) t),
        'stats', (SELECT row_to_json(t) FROM (
            SELECT COUNT(DISTINCT paperid) AS papers, COALESCE(SUM(cited_by_count), 0) AS total_cite,
                   ROUND(AVG(cited_by_count)::numeric, 2) AS avg_cite, MIN(year) AS first_year, MAX(year) AS last_year
            FROM InstitutionPapers
        ) t),
        'top_fields', COALESCE((SELECT json_agg(t) FROM (SELECT display_name, papers FROM TopFields) t), '[]'::json),
        'top_authors', COALESCE((SELECT json_agg(t) FROM (SELECT display_name, papers, cite FROM TopAuthors) t), '[]'::json)
    ) AS report;
    """
    return await run_sql_tool("generate_institution_report", sql, institution_id=iid, paper_limit=100)


@mcp.tool
async def generate_author_report(author_id: str) -> str:
    """报告分析：生成作者报告。该工具保留关系 SQL，避免当前多跳 MATCH 不稳定。"""
    aid = sql_text(author_id)
    sql = f"""
    WITH AuthorPapers AS (
        SELECT paa.paperid, p.cited_by_count, p.year, pd.title, pd.doi
        FROM cn_paper_author_affiliation paa
        JOIN cn_papers p ON paa.paperid = p.paperid
        LEFT JOIN cn_paperdetails pd ON paa.paperid = pd.paperid
        WHERE paa.authorid = '{aid}'
    ),
    TopFields AS (
        SELECT f.display_name AS field_name, f.fieldid, COUNT(DISTINCT pf.paperid) AS papers
        FROM cn_paperfields pf
        JOIN cn_fields f ON pf.fieldid = f.fieldid
        WHERE pf.paperid IN (SELECT paperid FROM AuthorPapers) AND pf.score_openalex > 0.3
        GROUP BY f.display_name, f.fieldid
        ORDER BY papers DESC
        LIMIT 5
    ),
    Collaborators AS (
        SELECT paa2.authorid, ad.display_name, COUNT(DISTINCT paa1.paperid) AS co_paper_count,
               SUM(p.cited_by_count) AS co_citations, ad.orcid
        FROM cn_paper_author_affiliation paa1
        JOIN cn_paper_author_affiliation paa2 ON paa1.paperid = paa2.paperid
        JOIN cn_papers p ON paa1.paperid = p.paperid
        JOIN cn_author_details ad ON paa2.authorid = ad.authorid
        WHERE paa1.authorid = '{aid}' AND paa2.authorid != '{aid}'
        GROUP BY paa2.authorid, ad.display_name, ad.orcid
        ORDER BY co_paper_count DESC
        LIMIT 10
    )
    SELECT json_build_object(
        'author_info', (SELECT row_to_json(t) FROM (
            SELECT authorid, display_name, last_known_institution, cited_by_count, orcid, works_api_url
            FROM cn_author_details WHERE authorid = '{aid}'
        ) t),
        'career_stats', (SELECT row_to_json(t) FROM (
            SELECT COUNT(DISTINCT paperid) AS papers, COALESCE(SUM(cited_by_count), 0) AS total_cite,
                   ROUND(AVG(cited_by_count)::numeric, 2) AS avg_cite, MIN(year) AS first_year, MAX(year) AS last_year
            FROM AuthorPapers
        ) t),
        'top_fields', COALESCE((SELECT json_agg(t) FROM (SELECT field_name, papers FROM TopFields) t), '[]'::json),
        'collaborators', COALESCE((SELECT json_agg(json_build_object(
            'authorid', authorid, 'display_name', display_name, 'co_paper_count', co_paper_count,
            'co_citations', co_citations, 'orcid', orcid,
            'openalex_url', 'https://openalex.org/authors/' || authorid
        ) ORDER BY co_paper_count DESC) FROM Collaborators), '[]'::json)
    ) AS report;
    """
    return await run_sql_tool("generate_author_report", sql, author_id=aid)


@mcp.tool
async def analyze_cross_discipline(field_name: str) -> str:
    """领域分析：查询某领域与其他领域的交叉融合情况。当前保留关系 SQL，避免多 MATCH。"""
    field = sql_text(field_name)
    sql = f"""
    SELECT f.display_name AS cross_field, COUNT(DISTINCT pf1.paperid) AS co_papers
    FROM cn_paperfields pf1
    JOIN cn_paperfields pf2 ON pf1.paperid = pf2.paperid
    JOIN cn_fields f ON pf2.fieldid = f.fieldid
    WHERE pf1.fieldid IN (SELECT fieldid FROM cn_fields WHERE display_name = '{field}')
      AND pf2.fieldid NOT IN (SELECT fieldid FROM cn_fields WHERE display_name = '{field}')
      AND pf1.score_openalex > 0.3
      AND pf2.score_openalex > 0.3
    GROUP BY f.display_name
    HAVING COUNT(DISTINCT pf1.paperid) >= 10
    ORDER BY co_papers DESC
    LIMIT 20;
    """
    return await run_sql_tool("analyze_cross_discipline", sql, field_name=field)


@mcp.tool
async def get_hottest_subfields() -> str:
    """领域分析：查询近三年热门子领域。"""
    sql = """
    SELECT f.display_name AS subfield, COUNT(DISTINCT p.paperid) AS recent_papers,
           SUM(p.cited_by_count) AS citations
    FROM cn_paperfields pf
    JOIN cn_fields f ON pf.fieldid = f.fieldid
    JOIN cn_papers p ON pf.paperid = p.paperid
    WHERE f.level = 1
      AND p.year >= EXTRACT(YEAR FROM CURRENT_DATE) - 3
      AND pf.score_openalex > 0.4
    GROUP BY f.display_name
    HAVING COUNT(DISTINCT p.paperid) >= 500
    ORDER BY recent_papers DESC
    LIMIT 20;
    """
    return await run_sql_tool("get_hottest_subfields", sql)


@mcp.tool
async def recommend_institution_experts_for_paper(
    paper_id: str,
    institution_id: str,
    year_from: int | None = None,
    year_to: int | None = None,
    field_score_min: float = 0.3,
    candidate_limit: int = 1000,
    top_k: int = 20,
    min_matched_papers: int = 2,
) -> str:
    """MMSQL：给一篇论文推荐指定机构内、同领域且满足年份条件的专家。"""
    pid = sql_text(paper_id)
    iid = sql_text(institution_id)
    try:
        start_year, end_year = normalized_year_range(year_from, year_to)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {"status": "error", "tool": "recommend_institution_experts_for_paper", "message": str(exc)},
            ensure_ascii=False,
            indent=2,
        )
    safe_score = bounded_score(field_score_min)
    safe_candidate_limit = bounded_limit(candidate_limit, default=1000, maximum=1000)
    safe_top_k = bounded_limit(top_k, default=20, maximum=100)
    safe_min_matched = bounded_limit(min_matched_papers, default=2, maximum=100)
    candidate_sql = paper_seed_field_candidate_sql(
        pid,
        safe_score,
        start_year,
        end_year,
        safe_candidate_limit,
    )

    def graph_sql_builder(paper_ids: list[str], candidate_rows: list[dict[str, Any]]) -> str:
        node_ids = ", ".join(paper_id[1:] for paper_id in paper_ids)
        summary = candidate_rows[0]
        field_count = int(summary.get("target_field_count") or 0)
        field_ids = sql_text(str(summary.get("target_field_ids") or ""))
        field_names = sql_text(str(summary.get("target_field_names") or ""))
        return f"""
        WITH TargetAuthors AS (
            SELECT DISTINCT authorid
            FROM cn_paper_author_affiliation
            WHERE paperid = '{pid}'
        ),
        TargetAuthorStats AS (
            SELECT COUNT(*) AS excluded_target_author_count FROM TargetAuthors
        ),
        InstitutionAuthorPapers AS (
            SELECT DISTINCT paa.properties->>'authorid' AS authorid, 'W' || pnode.id::text AS paperid
            FROM cn_writen_by_network
            MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
            WHERE pnode.id IN ({node_ids})
              AND paa.properties->>'institutionid' = '{iid}'
        ),
        ExpertStats AS (
            SELECT iap.authorid, COUNT(DISTINCT iap.paperid) AS matched_papers,
                   SUM(COALESCE(p.cited_by_count, 0)) AS matched_citations
            FROM InstitutionAuthorPapers iap
            JOIN cn_papers p ON iap.paperid = p.paperid
            WHERE NOT EXISTS (
                SELECT 1 FROM TargetAuthors ta WHERE ta.authorid = iap.authorid
            )
            GROUP BY iap.authorid
            HAVING COUNT(DISTINCT iap.paperid) >= {safe_min_matched}
        )
        SELECT ad.authorid, ad.display_name, ad.cited_by_count AS global_citations, ad.orcid,
               es.matched_papers, es.matched_citations,
               {len(paper_ids)} AS candidate_paper_count,
               tas.excluded_target_author_count,
               {field_count} AS target_field_count,
               '{field_ids}'::text AS target_field_ids,
               '{field_names}'::text AS target_field_names,
               'https://openalex.org/authors/' || ad.authorid AS openalex_url
        FROM ExpertStats es
        JOIN cn_author_details ad ON es.authorid = ad.authorid
        CROSS JOIN TargetAuthorStats tas
        ORDER BY es.matched_citations DESC, ad.cited_by_count DESC, ad.authorid ASC
        LIMIT {safe_top_k};
        """

    return await run_candidate_graph_tool(
        "recommend_institution_experts_for_paper",
        candidate_sql,
        graph_sql_builder,
        paper_id=pid,
        institution_id=iid,
        year_from=start_year,
        year_to=end_year,
        field_score_min=safe_score,
        candidate_limit=safe_candidate_limit,
        top_k=safe_top_k,
        min_matched_papers=safe_min_matched,
        candidate_order="paperid ASC",
    )


@mcp.tool
async def get_paper_institution_overlap_graph(
    paper_id: str,
    year_from: int | None = None,
    year_to: int | None = None,
    field_score_min: float = 0.3,
    candidate_limit: int = 200,
    top_k: int = 20,
) -> str:
    """MMSQL：查看目标论文同领域、指定年份候选论文中的机构分布。"""
    pid = sql_text(paper_id)
    try:
        start_year, end_year = normalized_year_range(year_from, year_to)
    except (TypeError, ValueError) as exc:
        return json.dumps(
            {"status": "error", "tool": "get_paper_institution_overlap_graph", "message": str(exc)},
            ensure_ascii=False,
            indent=2,
        )
    safe_score = bounded_score(field_score_min)
    safe_candidate_limit = bounded_limit(candidate_limit, default=200, maximum=1000)
    safe_top_k = bounded_limit(top_k, default=20, maximum=100)
    candidate_sql = paper_seed_field_candidate_sql(
        pid,
        safe_score,
        start_year,
        end_year,
        safe_candidate_limit,
    )

    def graph_sql_builder(paper_ids: list[str], candidate_rows: list[dict[str, Any]]) -> str:
        node_ids = ", ".join(paper_id[1:] for paper_id in paper_ids)
        summary = candidate_rows[0]
        field_count = int(summary.get("target_field_count") or 0)
        field_ids = sql_text(str(summary.get("target_field_ids") or ""))
        field_names = sql_text(str(summary.get("target_field_names") or ""))
        return f"""
        WITH InstitutionPapers AS (
            SELECT DISTINCT paa.properties->>'institutionid' AS institutionid,
                            'W' || pnode.id::text AS paperid
            FROM cn_writen_by_network
            MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
            WHERE pnode.id IN ({node_ids})
        )
        SELECT aff.institution_id, aff.affiliation_name,
               COUNT(DISTINCT ip.paperid) AS matched_papers,
               SUM(COALESCE(p.cited_by_count, 0)) AS matched_citations,
               aff.h_index, {len(paper_ids)} AS candidate_paper_count,
               {field_count} AS target_field_count,
               '{field_ids}'::text AS target_field_ids,
               '{field_names}'::text AS target_field_names,
               'https://openalex.org/institutions/' || aff.institution_id AS openalex_url
        FROM InstitutionPapers ip
        JOIN cn_affiliations aff ON ip.institutionid = aff.institution_id
        JOIN cn_papers p ON ip.paperid = p.paperid
        GROUP BY aff.institution_id, aff.affiliation_name, aff.h_index
        ORDER BY matched_papers DESC, matched_citations DESC, aff.institution_id ASC
        LIMIT {safe_top_k};
        """

    return await run_candidate_graph_tool(
        "get_paper_institution_overlap_graph",
        candidate_sql,
        graph_sql_builder,
        paper_id=pid,
        year_from=start_year,
        year_to=end_year,
        field_score_min=safe_score,
        candidate_limit=safe_candidate_limit,
        top_k=safe_top_k,
        candidate_order="paperid ASC",
        interpretation="Institution overlap within the bounded candidate set; not direct collaboration.",
    )


@mcp.tool
async def get_field_institution_collaboration_graph(field_name: str) -> str:
    """新增 MMSQL 场景：按领域统计机构共同参与的论文网络摘要。"""
    field = sql_text(field_name)
    sql = f"""
    WITH TargetField AS (
        SELECT fieldid FROM cn_fields WHERE display_name = '{field}' LIMIT 1
    ),
    FieldPapers AS (
        SELECT DISTINCT pf.paperid
        FROM cn_paperfields pf JOIN TargetField tf ON pf.fieldid = tf.fieldid
        WHERE pf.score_openalex > 0.3
        LIMIT 1000
    ),
    FieldPaperIds AS (
        SELECT substring(paperid from 2)::bigint AS paper_node_id FROM FieldPapers
    ),
    InstitutionPapers AS (
        SELECT DISTINCT paa.properties->>'institutionid' AS institutionid, 'W' || pnode.id::text AS paperid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM FieldPaperIds)
    ),
    InstitutionPairs AS (
        SELECT ip1.institutionid AS institution_a, ip2.institutionid AS institution_b,
               COUNT(DISTINCT ip1.paperid) AS co_papers
        FROM InstitutionPapers ip1
        JOIN InstitutionPapers ip2 ON ip1.paperid = ip2.paperid AND ip1.institutionid < ip2.institutionid
        GROUP BY ip1.institutionid, ip2.institutionid
        HAVING COUNT(DISTINCT ip1.paperid) >= 2
    )
    SELECT a.affiliation_name AS institution_a, b.affiliation_name AS institution_b,
           p.co_papers,
           'https://openalex.org/institutions/' || p.institution_a AS institution_a_url,
           'https://openalex.org/institutions/' || p.institution_b AS institution_b_url
    FROM InstitutionPairs p
    JOIN cn_affiliations a ON p.institution_a = a.institution_id
    JOIN cn_affiliations b ON p.institution_b = b.institution_id
    ORDER BY p.co_papers DESC
    LIMIT 30;
    """
    return await run_sql_tool("get_field_institution_collaboration_graph", sql, field_name=field, paper_limit=1000)


@mcp.tool
async def explain_field_top_authors(field_name: str) -> str:
    """返回优化后 MMSQL 领域作者查询的 EXPLAIN 计划。"""
    field = sql_text(field_name)
    sql = f"""
    EXPLAIN
    WITH TargetField AS (SELECT fieldid FROM cn_fields WHERE display_name = '{field}' LIMIT 1),
    FieldPapers AS (
        SELECT DISTINCT pf.paperid FROM cn_paperfields pf JOIN TargetField tf ON pf.fieldid = tf.fieldid
        WHERE pf.score_openalex > 0.3 LIMIT 1000
    ),
    FieldPaperIds AS (SELECT substring(paperid from 2)::bigint AS paper_node_id FROM FieldPapers),
    PaperAuthors AS (
        SELECT DISTINCT 'W' || pnode.id::text AS paperid, paa.properties->>'authorid' AS authorid
        FROM cn_writen_by_network
        MATCH {{(pnode: cn_paper_nodes)-[w: cn_writen_by]->(paa: cn_paper_author_affiliation_nodes)}}
        WHERE pnode.id IN (SELECT paper_node_id FROM FieldPaperIds)
    )
    SELECT ad.authorid, COUNT(DISTINCT pa.paperid) AS paper_count
    FROM PaperAuthors pa JOIN cn_author_details ad ON pa.authorid = ad.authorid
    GROUP BY ad.authorid
    LIMIT 30;
    """
    return await run_sql_tool("explain_field_top_authors", sql, field_name=field)


def _validated_venue_keys(venue_keys: list[str] | None) -> list[str]:
    """Validate normalized venue keys before interpolating a read-only SQL list."""
    if not venue_keys:
        raise ValueError("venue_keys must contain at least one normalized venue key")
    values = []
    for value in venue_keys:
        key = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_\-]{1,63}", key):
            raise ValueError(f"invalid venue_key: {value}")
        values.append(key)
    return sorted(set(values))


def _sql_text_array(values: list[str]) -> str:
    return ", ".join("'" + sql_text(value) + "'" for value in values)


@mcp.tool
async def search_papers_by_venue(
    venue_keys: list[str],
    institution_id: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 100,
) -> str:
    """按规范 venue_key 查询论文及作者；年份按 cn_papers.date 的年份解释。"""
    try:
        keys = _validated_venue_keys(venue_keys)
        start_year, end_year = normalized_year_range(year_from, year_to)
        safe_limit = bounded_limit(limit, default=100, maximum=500)
    except (TypeError, ValueError) as exc:
        return json.dumps({"status": "error", "tool": "search_papers_by_venue", "message": str(exc)}, ensure_ascii=False)

    venue_list = _sql_text_array(keys)
    institution_clause = ""
    if institution_id:
        institution_clause = f"AND paa_filter.institutionid = '{sql_text(institution_id)}'"
    sql = f"""
    WITH PaperVenue AS (
        SELECT DISTINCT paperid, venue_key, source_name, alias_name, venue_family, venue_tier
        FROM cn_paper_venue_lookup
        WHERE venue_key IN ({venue_list})
    ), TargetPapers AS (
        SELECT DISTINCT pv.paperid
        FROM PaperVenue pv
        JOIN cn_papers cp ON cp.paperid = pv.paperid
        JOIN cn_paper_author_affiliation paa_target ON paa_target.paperid = pv.paperid
        WHERE cp.year >= {start_year}
          AND cp.year <= {end_year}
          {"AND paa_target.institutionid = '" + sql_text(institution_id) + "'" if institution_id else ""}
    ), PaperAuthors AS (
        SELECT paa.paperid,
               string_agg(DISTINCT ca.authorid || '::' || ca.author_name, ' | ') AS authors
        FROM cn_paper_author_affiliation paa
        JOIN cn_authors ca ON ca.authorid = paa.authorid
        JOIN TargetPapers tp ON tp.paperid = paa.paperid
        GROUP BY paa.paperid
    )
    SELECT cp.paperid, cp.date, cp.year::int AS publication_year,
           cpd.title, pv.venue_key, pv.source_name, pv.alias_name,
           pv.venue_family, pv.venue_tier, pa.authors
    FROM cn_papers cp
    JOIN cn_paperdetails cpd ON cpd.paperid = cp.paperid
    JOIN PaperVenue pv ON pv.paperid = cp.paperid
    JOIN PaperAuthors pa ON pa.paperid = cp.paperid
    JOIN cn_paper_author_affiliation paa_filter ON paa_filter.paperid = cp.paperid
    WHERE cp.year >= {start_year}
      AND cp.year <= {end_year}
      {institution_clause}
    GROUP BY cp.paperid, cp.date, cpd.title, pv.venue_key, pv.source_name,
             pv.alias_name, pv.venue_family, pv.venue_tier, pa.authors
    ORDER BY cp.date DESC, cp.paperid
    LIMIT {safe_limit};
    """
    return await run_sql_tool(
        "search_papers_by_venue", sql,
        venue_keys=keys, institution_id=institution_id,
        year_from=start_year, year_to=end_year,
        year_definition="publication_year from cn_papers.date",
    )


@mcp.tool
async def aggregate_papers_by_venue(
    venue_keys: list[str],
    institution_id: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> str:
    """按出版日期年份统计去重论文数，避免作者 join 造成重复计数。"""
    try:
        keys = _validated_venue_keys(venue_keys)
        start_year, end_year = normalized_year_range(year_from, year_to)
    except (TypeError, ValueError) as exc:
        return json.dumps({"status": "error", "tool": "aggregate_papers_by_venue", "message": str(exc)}, ensure_ascii=False)

    venue_list = _sql_text_array(keys)
    institution_clause = ""
    if institution_id:
        institution_clause = f"AND paa.institutionid = '{sql_text(institution_id)}'"
    sql = f"""
    SELECT cp.year::int AS publication_year,
           pv.venue_key,
           COUNT(DISTINCT cp.paperid) AS paper_count
    FROM cn_papers cp
    JOIN cn_paper_venue_lookup pv ON pv.paperid = cp.paperid
    {"JOIN cn_paper_author_affiliation paa ON paa.paperid = cp.paperid" if institution_id else ""}
    WHERE pv.venue_key IN ({venue_list})
      AND cp.year >= {start_year}
      AND cp.year <= {end_year}
      {institution_clause}
    GROUP BY cp.year::int, pv.venue_key
    ORDER BY publication_year, pv.venue_key;
    """
    return await run_sql_tool(
        "aggregate_papers_by_venue", sql,
        venue_keys=keys, institution_id=institution_id,
        year_from=start_year, year_to=end_year,
        year_definition="publication_year from cn_papers.date",
        count_definition="COUNT(DISTINCT paperid)",
    )


@mcp.tool
async def get_paper_venue(paper_id: str) -> str:
    """查询单篇论文的原始 source 与规范 venue 信息。"""
    pid = sql_text(paper_id)
    if not re.fullmatch(r"W[0-9]+", pid):
        return json.dumps({"status": "error", "tool": "get_paper_venue", "message": "paper_id must look like W123"}, ensure_ascii=False)
    sql = f"""
    SELECT DISTINCT paperid, sourceid, source_name, source_normalized_name,
           alias_name, venue_key, venue_family, venue_tier,
           issn, publisher, homepage_url, landing_page_url, pdf_url,
           version, license
    FROM cn_paper_venue_lookup
    WHERE paperid = '{pid}'
    ORDER BY venue_key NULLS LAST, source_name, sourceid;
    """
    return await run_sql_tool("get_paper_venue", sql, paper_id=pid)


@mcp.tool
async def rank_venue_papers_by_topic(
    query: str,
    venue_keys: list[str],
    institution_id: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 50,
    min_topic_score: float = 0.0,
) -> str:
    """在已限定的 venue 候选集内按标题+摘要主题相关性重排。"""
    text = (query or "").strip()
    if not text:
        return json.dumps({"status": "error", "tool": "rank_venue_papers_by_topic", "message": "query is required"}, ensure_ascii=False)
    try:
        keys = _validated_venue_keys(venue_keys)
        start_year, end_year = normalized_year_range(year_from, year_to)
        safe_limit = bounded_limit(limit, default=50, maximum=200)
        score_floor = float(min_topic_score)
        if score_floor < 0 or score_floor > 100:
            raise ValueError("min_topic_score must be between 0 and 100")
    except (TypeError, ValueError) as exc:
        return json.dumps({"status": "error", "tool": "rank_venue_papers_by_topic", "message": str(exc)}, ensure_ascii=False)

    key_sql = _sql_text_array(keys)
    topic_terms = [term for term in re.findall(r"[a-z0-9]+", text.lower()) if len(term) >= 3]
    if not topic_terms:
        return json.dumps({"status": "error", "tool": "rank_venue_papers_by_topic", "message": "query must contain searchable terms"}, ensure_ascii=False)
    # OR semantics is intentional: a paper need not contain every topic word.
    topic_tsquery = " | ".join(topic_terms[:24])
    institution_filter = ""
    if institution_id:
        institution_filter = f"AND EXISTS (SELECT 1 FROM cn_paper_author_affiliation pinst WHERE pinst.paperid = cp.paperid AND pinst.institutionid = '{sql_text(institution_id)}')"
    sql = f"""
    WITH Candidates AS (
        SELECT DISTINCT cp.paperid, cp.date, cp.year::int AS publication_year,
               cpd.title, cpd.abstract_inverted_index,
               pv.venue_key, pv.source_name, pv.alias_name,
               public.inverted_index_to_text(cpd.abstract_inverted_index) AS abstract
        FROM cn_papers cp
        JOIN cn_paperdetails cpd ON cpd.paperid = cp.paperid
        JOIN cn_paper_venue_lookup pv ON pv.paperid = cp.paperid
        WHERE pv.venue_key IN ({key_sql})
          AND cp.year BETWEEN {start_year} AND {end_year}
          {institution_filter}
    ), Scored AS (
        SELECT c.*,
               ts_rank_cd(
                   to_tsvector('english', coalesce(c.title, '') || ' ' || coalesce(c.abstract, '')),
                   to_tsquery('english', '{sql_text(topic_tsquery)}')
               ) AS topic_score
        FROM Candidates c
    ), Matched AS (
        SELECT * FROM Scored WHERE topic_score >= {score_floor}
        ORDER BY topic_score DESC, publication_year DESC, paperid
        LIMIT {safe_limit}
    ), AuthorLabels AS (
        SELECT paa.paperid,
               string_agg(DISTINCT paa.authorid || '::' || COALESCE(ca.author_name, paa.authorid), ' | ') AS authors
        FROM cn_paper_author_affiliation paa
        LEFT JOIN cn_authors ca ON ca.authorid = paa.authorid
        WHERE paa.paperid IN (SELECT paperid FROM Matched)
        GROUP BY paa.paperid
    )
    SELECT m.paperid, m.date, m.publication_year, m.title, m.venue_key,
           m.source_name, m.alias_name, m.topic_score, al.authors, m.abstract
    FROM Matched m
    LEFT JOIN AuthorLabels al ON al.paperid = m.paperid
    ORDER BY m.topic_score DESC, m.publication_year DESC, m.paperid;
    """
    return await run_sql_tool(
        "rank_venue_papers_by_topic", sql,
        query=text, venue_keys=keys, institution_id=institution_id,
        year_from=start_year, year_to=end_year,
        min_topic_score=score_floor,
        ranking_definition="title + abstract PostgreSQL text search within venue/institution candidates",
    )


@mcp.tool
async def get_database_schema() -> str:
    """Inspect the configured relational schema for the separate DB_Tools alternative."""
    result = await db.execute_sql_query("""SELECT table_name, column_name, data_type
        FROM information_schema.columns WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position LIMIT 5000""")
    result["queryBackend"] = "db_tools"
    result["externalTools"] = {
        "semantic": "Milvus via search_papers_by_natural_language/find_similar_papers_by_paperid",
        "citations": "Neo4j via expand_paper_citations_neo4j when enabled",
    }
    return json.dumps(result, ensure_ascii=False, default=str)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        try:
            app = mcp.http_app(transport=transport)
        except AttributeError:
            app = mcp._app
        protected_app = RawAuthMiddleware(app)
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8009"))
        print(f"Server starting on {host}:{port}...")
        uvicorn.run(protected_app, host=host, port=port)
