from __future__ import annotations

import decimal
import json
import time
from datetime import date, datetime
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_env
from app.logging_config import get_logger
from app.models import ToolSpec
from app.resource_usage import finish_resource_span, start_resource_span
from app.tools.base import ToolProvider


DEFAULT_LIMIT = 20
MAX_LIMIT = 50
logger = get_logger("bigdipper.db")


class HelmDBToolProvider(ToolProvider):
    def __init__(self) -> None:
        self.connection_params = {
            "host": get_env("DB_HOST"),
            "port": int(get_env("DB_PORT", "5432") or "5432"),
            "user": get_env("DB_USER"),
            "password": get_env("DB_PASSWORD"),
            "dbname": get_env("DB_NAME"),
            "options": "-c client_encoding=UTF8",
        }

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_all_fields",
                description="List HelmDB scholarly fields with level, works count, and cited-by count.",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
                },
            ),
            ToolSpec(
                name="search_institutions",
                description="Search HelmDB institutions by English institution name.",
                parameters={
                    "type": "object",
                    "properties": {
                        "institution_name": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "required": ["institution_name"],
                },
            ),
            ToolSpec(
                name="get_institution_field_stats",
                description="Rank institutions by output and citation stats in a HelmDB field.",
                parameters={
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "required": ["field_name"],
                },
            ),
            ToolSpec(
                name="get_field_top_authors",
                description="Rank authors by citations in a HelmDB field.",
                parameters={
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "required": ["field_name"],
                },
            ),
            ToolSpec(
                name="paper_title_to_id",
                description="Search paper IDs by title keyword in HelmDB.",
                parameters={
                    "type": "object",
                    "properties": {
                        "paper_title": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "required": ["paper_title"],
                },
            ),
            ToolSpec(
                name="get_paper_references",
                description="Find papers that cite a HelmDB paper ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "paper_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "required": ["paper_id"],
                },
            ),
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_all_fields":
            return self.get_all_fields(_limit(arguments.get("limit")))
        if name == "search_institutions":
            return self.search_institutions(
                institution_name=str(arguments.get("institution_name", "")),
                limit=_limit(arguments.get("limit")),
            )
        if name == "get_institution_field_stats":
            return self.get_institution_field_stats(
                field_name=str(arguments.get("field_name", "")),
                limit=_limit(arguments.get("limit")),
            )
        if name == "get_field_top_authors":
            return self.get_field_top_authors(
                field_name=str(arguments.get("field_name", "")),
                limit=_limit(arguments.get("limit")),
            )
        if name == "paper_title_to_id":
            return self.paper_title_to_id(
                paper_title=str(arguments.get("paper_title", "")),
                limit=_limit(arguments.get("limit"), default=10),
            )
        if name == "get_paper_references":
            return self.get_paper_references(
                paper_id=str(arguments.get("paper_id", "")),
                limit=_limit(arguments.get("limit")),
            )
        raise ValueError(f"Unknown HelmDB tool: {name}")

    def test_connection(self) -> dict[str, Any]:
        return self._query("SELECT 1 AS ok;", ())

    def get_all_fields(self, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        return self._query(
            """
            SELECT id, fieldid, display_name, level, works_count, cited_by_count
            FROM cn_fields
            ORDER BY level, display_name
            LIMIT %s;
            """,
            (limit,),
            label="get_all_fields",
        )

    def search_institutions(self, institution_name: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        term = institution_name.strip()
        if not term:
            return _validation_error("search_institutions", "institution_name is required")
        return self._query(
            """
            SELECT
                institution_id,
                affiliation_name,
                country_code,
                h_index,
                productivity,
                homepage_url,
                CASE WHEN ror IS NOT NULL THEN 'https://ror.org/' || ror ELSE NULL END AS ror_url,
                'https://openalex.org/institutions/' || institution_id AS openalex_url
            FROM cn_affiliations
            WHERE affiliation_name ILIKE %s
            ORDER BY h_index DESC NULLS LAST
            LIMIT %s;
            """,
            (f"%{term}%", limit),
            label="search_institutions",
        )

    def get_institution_field_stats(self, field_name: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        field = field_name.strip()
        if not field:
            return _validation_error("get_institution_field_stats", "field_name is required")
        return self._query(
            """
            WITH
            target_field AS (
                SELECT fieldid, display_name
                FROM cn_fields
                WHERE display_name = %s
                LIMIT 1
            ),
            field_papers AS (
                SELECT DISTINCT pf.paperid
                FROM cn_paperfields pf
                JOIN target_field tf ON pf.fieldid = tf.fieldid
                WHERE pf.score_openalex > 0.3
            ),
            institution_papers AS (
                SELECT paa.institutionid, fp.paperid, p.cited_by_count
                FROM field_papers fp
                JOIN cn_paper_author_affiliation paa ON fp.paperid = paa.paperid
                JOIN cn_papers p ON fp.paperid = p.paperid
                WHERE paa.institutionid IS NOT NULL
            ),
            institution_stats AS (
                SELECT
                    institutionid,
                    COUNT(DISTINCT paperid) AS paper_count,
                    SUM(cited_by_count) AS total_citations,
                    ROUND(AVG(cited_by_count)::numeric, 2) AS avg_citations,
                    MAX(cited_by_count) AS max_citations
                FROM institution_papers
                GROUP BY institutionid
                HAVING COUNT(DISTINCT paperid) >= 5
            )
            SELECT
                a.affiliation_name AS institution_name,
                a.country_code,
                s.paper_count,
                s.total_citations,
                s.avg_citations,
                s.max_citations,
                a.h_index AS institution_h_index,
                a.productivity,
                'https://openalex.org/institutions/' || a.institution_id AS openalex_url
            FROM institution_stats s
            JOIN cn_affiliations a ON s.institutionid = a.institution_id
            ORDER BY s.total_citations DESC NULLS LAST
            LIMIT %s;
            """,
            (field, limit),
            label="get_institution_field_stats",
            extra={"field_name": field},
        )

    def get_field_top_authors(self, field_name: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        field = field_name.strip()
        if not field:
            return _validation_error("get_field_top_authors", "field_name is required")
        return self._query(
            """
            SELECT
                ad.display_name AS author_name,
                ad.authorid,
                ad.last_known_institution,
                COUNT(DISTINCT paa.paperid) AS paper_count,
                SUM(p.cited_by_count) AS total_citations,
                a.h_index,
                a.productivity,
                ad.orcid,
                'https://openalex.org/authors/' || ad.authorid AS openalex_url
            FROM cn_paperfields pf
            JOIN cn_paper_author_affiliation paa ON pf.paperid = paa.paperid
            JOIN cn_papers p ON pf.paperid = p.paperid
            JOIN cn_author_details ad ON paa.authorid = ad.authorid
            LEFT JOIN cn_authors a ON paa.authorid = a.authorid
            WHERE pf.fieldid IN (SELECT fieldid FROM cn_fields WHERE display_name = %s)
              AND pf.score_openalex > 0.3
              AND paa.authorid IS NOT NULL
            GROUP BY ad.display_name, ad.authorid, ad.last_known_institution, a.h_index, a.productivity, ad.orcid
            HAVING COUNT(DISTINCT paa.paperid) >= 3
            ORDER BY total_citations DESC NULLS LAST
            LIMIT %s;
            """,
            (field, limit),
            label="get_field_top_authors",
            extra={"field_name": field},
        )

    def paper_title_to_id(self, paper_title: str, limit: int = 10) -> dict[str, Any]:
        title = paper_title.strip()
        if not title:
            return _validation_error("paper_title_to_id", "paper_title is required")
        return self._query(
            """
            SELECT paperid, title, year, cited_by_count
            FROM cn_paperdetails
            WHERE title ILIKE %s
            ORDER BY cited_by_count DESC NULLS LAST
            LIMIT %s;
            """,
            (f"%{title}%", limit),
            label="paper_title_to_id",
        )

    def get_paper_references(self, paper_id: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        pid = paper_id.strip()
        if not pid:
            return _validation_error("get_paper_references", "paper_id is required")
        return self._query(
            """
            SELECT
                cp.citing_paperid AS paperid,
                pd.title,
                pd.year,
                pd.cited_by_count,
                pd.doi,
                'https://openalex.org/works/' || cp.citing_paperid AS openalex_url
            FROM cn_paper_ref cp
            JOIN cn_paperdetails pd ON cp.citing_paperid = pd.paperid
            WHERE cp.cited_paperid = %s
            ORDER BY pd.cited_by_count DESC NULLS LAST
            LIMIT %s;
            """,
            (pid, limit),
            label="get_paper_references",
            extra={"paper_id": pid},
        )

    def _query(
        self,
        sql: str,
        params: tuple[Any, ...],
        label: str = "query",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        missing = self._missing_config()
        if missing:
            return {
                "status": "error",
                "tool": label,
                "error": f"Missing DB configuration: {', '.join(missing)}",
                "data": [],
                "summary": "database is not configured",
            }

        start = time.time()
        resource_span = start_resource_span(label, "database", backend="postgresql")
        logger.info("db_query=start tool=%s params=%s", label, json.dumps(params, ensure_ascii=False, default=str))
        try:
            with psycopg2.connect(**self.connection_params) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(sql, params)
                    rows = [_jsonable(dict(row)) for row in cursor.fetchall()]
        except Exception as exc:
            error_text = str(exc) or exc.__class__.__name__
            elapsed = round(time.time() - start, 3)
            finish_resource_span(resource_span, status="failed")
            logger.exception("db_query=error tool=%s elapsed_seconds=%.3f error=%s", label, elapsed, error_text)
            return {
                "status": "error",
                "tool": label,
                "error": error_text,
                "data": [],
                "summary": f"{label} failed",
            }

        elapsed = round(time.time() - start, 3)
        finish_resource_span(resource_span, status="success")
        result: dict[str, Any] = {
            "status": "success",
            "tool": label,
            "rowCount": len(rows),
            "data": rows,
            "executeTimeSeconds": elapsed,
            "summary": f"{label} returned {len(rows)} rows",
        }
        if extra:
            result.update(extra)
        logger.info(
            "db_query=end tool=%s elapsed_seconds=%.3f row_count=%d preview=%s",
            label,
            elapsed,
            len(rows),
            json.dumps(rows[:10], ensure_ascii=False, default=str),
        )
        return result

    def _missing_config(self) -> list[str]:
        required = ["host", "user", "password", "dbname"]
        return [name for name in required if not self.connection_params.get(name)]


def _limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 1), MAX_LIMIT)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _validation_error(tool: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "tool": tool,
        "error": message,
        "data": [],
        "summary": message,
    }
