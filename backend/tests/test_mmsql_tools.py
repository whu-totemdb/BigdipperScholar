from __future__ import annotations


import asyncio
import json

from app.mcp_servers import cn_queries as mmsql_server


class _CapturingDb:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def execute_sql_query(self, sql: str) -> dict:
        self.sql.append(sql)
        if "MATCH {" in sql:
            return {
                "status": "success",
                "row_count": 1,
                "data": [{"candidate_paper_count": 1}],
            }
        return {
            "status": "success",
            "row_count": 1,
            "data": [
                {
                    "paperid": "W1000004903",
                    "year": 2020,
                    "cited_by_count": 10,
                    "target_field_count": 1,
                    "target_field_ids": "C1",
                    "target_field_names": "Astronomy",
                }
            ],
        }


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_institution_field_author_rank_applies_bounded_parameters(monkeypatch) -> None:
    db = _CapturingDb()
    monkeypatch.setattr(mmsql_server, "db", db)

    payload = _run(
        mmsql_server.get_institution_field_author_rank(
            "I99065089",
            "Astronomy",
            year_from=2020,
            year_to=2024,
            candidate_limit=5000,
            top_k=500,
            min_papers=4,
        )
    )

    assert len(db.sql) == 2
    candidate_sql, graph_sql = db.sql
    sql = candidate_sql + "\n" + graph_sql
    assert payload["status"] == "success"
    assert payload["candidate_count"] == 1
    assert payload["candidate_limit"] == 1000
    assert payload["top_k"] == 100
    assert payload["min_papers"] == 4
    assert payload["_execution"]["queryLanguage"] == "mmsql"
    assert "p.year >= 2020" in sql
    assert "p.year <= 2024" in sql
    assert "pf.paperid ~ '^W[0-9]+$'" in sql
    assert "ORDER BY pf.paperid ASC" in sql
    assert "HAVING COUNT(DISTINCT giap.paperid) >= 4" in sql
    assert "LIMIT 1000" in sql
    assert graph_sql.rstrip().endswith("LIMIT 100;")


def test_paper_institution_overlap_exposes_candidate_and_field_boundaries(monkeypatch) -> None:
    db = _CapturingDb()
    monkeypatch.setattr(mmsql_server, "db", db)

    payload = _run(
        mmsql_server.get_paper_institution_overlap_graph(
            "W1000004903",
            year_from=2018,
            year_to=2022,
            field_score_min=0.4,
            candidate_limit=250,
            top_k=12,
        )
    )

    assert len(db.sql) == 2
    candidate_sql, graph_sql = db.sql
    sql = candidate_sql + "\n" + graph_sql
    assert payload["field_score_min"] == 0.4
    assert payload["candidate_limit"] == 250
    assert payload["top_k"] == 12
    assert "FieldSummary AS" in sql
    assert "target_field_ids" in sql
    assert "candidate_paper_count" in sql
    assert "pf.score_openalex >= 0.4" in sql
    assert "p.year >= 2018" in sql
    assert "p.year <= 2022" in sql
    assert graph_sql.rstrip().endswith("LIMIT 12;")
    assert "not direct collaboration" in payload["interpretation"]


def test_institution_expert_recommendation_reports_exclusion_boundary(monkeypatch) -> None:
    db = _CapturingDb()
    monkeypatch.setattr(mmsql_server, "db", db)

    payload = _run(
        mmsql_server.recommend_institution_experts_for_paper(
            "W1000004903",
            "I99065089",
            year_from=2015,
            year_to=2024,
            field_score_min=0.35,
            candidate_limit=300,
            top_k=15,
            min_matched_papers=3,
        )
    )

    assert len(db.sql) == 2
    candidate_sql, graph_sql = db.sql
    sql = candidate_sql + "\n" + graph_sql
    assert payload["candidate_limit"] == 300
    assert payload["top_k"] == 15
    assert payload["min_matched_papers"] == 3
    assert "TargetAuthorStats AS" in sql
    assert "excluded_target_author_count" in sql
    assert "NOT EXISTS" in sql
    assert "HAVING COUNT(DISTINCT iap.paperid) >= 3" in sql
    assert "ad.cited_by_count AS global_citations" in sql
    assert graph_sql.rstrip().endswith("LIMIT 15;")


def test_no_year_candidates_are_limited_before_metadata_join() -> None:
    rank_sql = mmsql_server.institution_field_candidate_sql("Astronomy", None, None, 1000)
    seed_sql = mmsql_server.paper_seed_field_candidate_sql("W1000004903", 0.3, None, None, 200)

    for sql, expected_limit in ((rank_sql, 1000), (seed_sql, 200)):
        assert "CandidatePaperIds AS" in sql
        assert sql.index(f"LIMIT {expected_limit}") < sql.index("JOIN cn_papers")
        assert "paperid ~ '^W[0-9]+$'" in sql


def test_paper_institution_overlap_defaults_to_200_candidates(monkeypatch) -> None:
    db = _CapturingDb()
    monkeypatch.setattr(mmsql_server, "db", db)

    payload = _run(mmsql_server.get_paper_institution_overlap_graph("W1000004903"))

    assert payload["candidate_limit"] == 200
    assert "LIMIT 200" in db.sql[0]


def test_tools_reject_reversed_year_range(monkeypatch) -> None:
    db = _CapturingDb()
    monkeypatch.setattr(mmsql_server, "db", db)

    payload = _run(
        mmsql_server.get_paper_institution_overlap_graph(
            "W1000004903",
            year_from=2024,
            year_to=2020,
        )
    )

    assert payload["status"] == "error"
    assert payload["message"] == "year_from must be less than or equal to year_to"
    assert db.sql == []
