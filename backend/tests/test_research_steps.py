from __future__ import annotations

import asyncio
import json

from app.mcp_servers import cn_queries as mmsql_server
from app.agent_experiments import (
    _build_research_steps,
    _public_tool_name,
    _strip_execution_metadata,
    _tool_output_failed,
)


def _mcp_output(payload: dict) -> str:
    return json.dumps([{"type": "text", "text": json.dumps(payload)}])


def test_research_steps_include_complete_packaged_sql_without_result_rows() -> None:
    query = """
    WITH PaperAuthors AS (
        SELECT p.id
        FROM paper_network
        MATCH {(p: paper)-[w: written_by]->(a: author)}
    )
    SELECT * FROM PaperAuthors
    """.strip()
    output = _mcp_output(
        {
            "status": "success",
            "tool": "get_paper_author_affiliations_graph",
            "row_count": 2,
            "data": [{"paperid": "W1"}, {"paperid": "W2"}],
            "_execution": {
                "backend": "helmdb",
                "queryLanguage": "mmsql",
                "queryText": query,
                "databaseLatencyMs": 41.5,
                "rowCount": 2,
            },
        }
    )
    observations = [
        {
            "event": "tool_call",
            "observationId": 3,
            "round": 2,
            "toolName": "get_paper_author_affiliations_graph",
            "arguments": {"paper_id": "W1"},
            "status": "success",
            "latencyMs": 55.0,
            "result": _strip_execution_metadata(output),
            "execution": {
                "backend": "helmdb",
                "queryLanguage": "mmsql",
                "queryText": query,
                "databaseLatencyMs": 41.5,
                "rowCount": 2,
            },
        }
    ]

    steps = _build_research_steps(observations, [3])

    assert len(steps) == 1
    assert steps[0].public_name == "Explore paper author affiliations"
    assert steps[0].query_text == query
    assert steps[0].query_language == "mmsql"
    assert steps[0].parameters == {"paper_id": "W1"}
    assert steps[0].row_count == 2
    assert steps[0].selected_for_report is True
    cleaned = json.loads(observations[0]["result"])
    assert "_execution" not in cleaned
    assert cleaned["data"] == [{"paperid": "W1"}, {"paperid": "W2"}]


def test_nested_mcp_error_is_treated_as_failed() -> None:
    output = _mcp_output({"status": "error", "message": "query rejected"})
    assert _tool_output_failed(output) is True


def test_mcp_protocol_errors_are_not_successful_observations() -> None:
    assert _tool_output_failed(json.dumps([{"type": "text", "text": "Error calling tool 'query': timed out"}]))
    assert _tool_output_failed({"isError": True, "content": [{"type": "text", "text": "database unavailable"}]})
    assert not _tool_output_failed(_mcp_output({"status": "success", "data": [{"title": "Error calling tool analysis"}]}))


def test_actual_tool_backend_is_not_disguised() -> None:
    assert _public_tool_name("custom_neo4j_scan") == "Custom Neo4J Scan"


def test_metadata_tool_exposes_complete_sql_execution_metadata(monkeypatch) -> None:
    class _FakeDb:
        async def execute_sql_query(self, sql: str) -> dict:
            assert sql.strip().startswith("WITH input_papers")
            return {
                "status": "success",
                "row_count": 1,
                "data": [{"paperid": "W1"}],
            }

    fake_db = _FakeDb()
    monkeypatch.setattr(mmsql_server, "db", fake_db)
    payload = asyncio.run(
        mmsql_server.fetch_paper_metadata_payload(["W1"], 10)
    )

    execution = payload["_execution"]
    assert execution["queryText"].startswith("WITH input_papers")
    assert execution["queryLanguage"] == "sql"
    assert execution["rowCount"] == 1
