"""Reusable queries executed exclusively in the configured multimodel database."""


import asyncio
import decimal
import json
import os
import re
import threading
import time
from typing import Any

import psycopg2
from dotenv import load_dotenv

load_dotenv()


class MultimodelDatabase:
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


db = MultimodelDatabase()


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
        "backend": "mmdb",
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
            "backend": "mmdb",
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
            "backend": "mmdb",
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
        "backend": "mmdb",
        "queryLanguage": "mmsql",
        "queryText": candidate_sql.strip() + ";\n\n" + graph_sql.strip(),
        "statements": [
            {"stage": "candidate_query", "queryLanguage": "sql", "queryText": candidate_sql.strip(),
             "status": candidate_raw.get("status"), "databaseLatencyMs": candidate_latency_ms},
            {"stage": "graph_query", "queryLanguage": "mmsql", "queryText": graph_sql.strip(),
             "status": graph_raw.get("status"), "databaseLatencyMs": graph_latency_ms},
        ],
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
        "backend": "mmdb",
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


async def get_paper_metadata_batch(paper_ids: list[str], limit: int = 20) -> str:
    """语义分析核心工具：批量补全 paperid 的题名、摘要、年份、引用数、领域和作者，保持输入顺序。"""
    payload = await fetch_paper_metadata_payload(paper_ids, limit, tool_name="get_paper_metadata_batch")
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def execute_sciscinet_sql(sql: str, limit: int = 100) -> str:
    """通用只读 SQL：在 sciscinet_cn 库执行 SELECT/WITH/EXPLAIN 查询，自动限制返回行数。"""
    normalized_sql, error = normalize_readonly_sql(sql, limit)
    if error:
        return json.dumps({"status": "error", "tool": "execute_sciscinet_sql", "message": error}, ensure_ascii=False, indent=2)
    return await run_sql_tool("execute_sciscinet_sql", normalized_sql or sql, original_sql=(sql or "").strip(), limit=bounded_limit(limit, default=100, maximum=1000))


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


async def get_all_tables() -> str:
    """元数据：查询 public schema 下所有表。"""
    sql = """
    SELECT table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name;
    """
    return await run_sql_tool("get_all_tables", sql)


async def get_all_fields() -> str:
    """元数据：查询所有学科领域。"""
    sql = """
    SELECT id, fieldid, display_name, level, works_count, cited_by_count
    FROM cn_fields
    ORDER BY level, display_name;
    """
    return await run_sql_tool("get_all_fields", sql)


async def get_all_cn_institutions() -> str:
    """元数据：查询所有中国机构名称。"""
    sql = """
    SELECT DISTINCT affiliation_name
    FROM cn_affiliations
    ORDER BY affiliation_name;
    """
    return await run_sql_tool("get_all_cn_institutions", sql)


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
