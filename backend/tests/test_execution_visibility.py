import asyncio
import json
from unittest.mock import MagicMock

import pytest

from app.agent_experiments import (
    _extract_execution_metadata, _strip_execution_metadata, _tool_observation_content,
)
from app.mcp_servers import release_server as server


@pytest.mark.parametrize("failed", [False, True])
def test_sql_and_actual_parameters_reach_model_on_success_and_failure(monkeypatch, failed):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("paperid",)]
    cursor.fetchall.return_value = [("W1",)]
    if failed:
        cursor.execute.side_effect = RuntimeError("query timeout")
    monkeypatch.setattr(server.cn.db, "get_connection", lambda: connection)
    sql = "SELECT paperid FROM papers WHERE year >= %(year)s LIMIT %(limit)s"
    params = {"year": 2020, "limit": 5}
    result = server._parameter_query(sql, params)
    cursor.execute.assert_called_once_with(sql, params)
    connection.close.assert_called_once()
    observation = {"observationId": 7, "toolName": "packaged_query",
                   "status": "failed" if failed else "success",
                   "execution": _extract_execution_metadata(result)}
    if failed:
        observation["error"] = result["message"]
    else:
        observation["result"] = _strip_execution_metadata(result)
    message = json.loads(_tool_observation_content(observation))
    assert message["execution"]["queryText"] == sql
    assert message["execution"]["parameters"] == params
    if failed:
        assert message["error"] == "query timeout" and "result" not in message
    else:
        assert json.loads(message["result"])["data"] == [{"paperid": "W1"}]
        assert "_execution" not in json.loads(message["result"])


def test_vector_is_bound_in_full_but_summarized_for_model(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [("paperid",)]
    cursor.fetchall.return_value = []
    monkeypatch.setattr(server.cn.db, "get_connection", lambda: connection)
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_MODEL", "fixture-model")
    params = {"embedding": "[0.125,0.375]", "paper_id": "W1", "limit": 3}
    result = server._parameter_query("SELECT ...", params)
    assert cursor.execute.call_args.args[1]["embedding"] == "[0.125,0.375]"
    visible = result["_execution"]["parameters"]
    assert visible["paper_id"] == "W1" and visible["limit"] == 3
    assert visible["embedding"]["dimensions"] == 2
    assert visible["embedding"]["model"] == "fixture-model"
    assert visible["embedding"]["source"] == "stored_paper_vector"
    assert len(visible["embedding"]["sha256"]) == 64
    assert "0.125" not in json.dumps(visible)
    assert params["embedding"] == "[0.125,0.375]"


def test_multistage_queries_survive_extraction_and_model_message():
    execution = {"queryText": "SELECT neighbors", "parameters": {"paper_id": "W1"},
                 "seedQuery": {"queryText": "SELECT embedding", "parameters": {"paper_id": "W1"}},
                 "statements": [{"stage": "candidate_query", "queryText": "SELECT candidates"},
                                {"stage": "graph_query", "queryText": "SELECT graph results"}]}
    observation = {"status": "success", "result": "{}", "execution": _extract_execution_metadata(
        {"status": "success", "_execution": execution})}
    assert json.loads(_tool_observation_content(observation))["execution"] == execution


def test_schema_query_failure_is_not_reported_as_success(monkeypatch):
    failure = {"status": "error", "message": "connection failed", "data": [],
               "_execution": {"queryText": "SELECT columns", "parameters": {"schema": "public"}}}
    monkeypatch.setattr(server, "_parameter_query", lambda *_: failure)
    assert json.loads(asyncio.run(server.get_database_schema())) == failure
