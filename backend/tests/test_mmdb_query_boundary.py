"""Verify the main query path, vector provenance and explicit alternative routing."""
import asyncio
import ast
import json
from pathlib import Path

import pytest

from app import agent_experiments
from app.evidence_provenance import ground_bundle
from app.mcp_servers import release_server as server
from app.mcp_servers.mmdb_vectors import validate_vector, vector_search_sql


def test_main_modules_do_not_import_separate_stores():
    root = Path(server.__file__).parent
    for name in ("release_server.py", "cn_queries.py", "mmdb_vectors.py"):
        source = (root / name).read_text()
        tree = ast.parse(source)
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        assert not any("milvus" in x or "neo4j" in x or "db_tools_server" in x for x in imports)
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert {"get_database_schema", "search_papers_by_natural_language", "find_similar_papers_by_paperid", "expand_paper_citations"} <= names
    assert not any("neo4j" in n or "milvus" in n for n in names)


def test_alternative_requires_separate_config_and_prompt(monkeypatch):
    monkeypatch.delenv("BIGDIPPER_DB_TOOLS_MCP_CONFIG", raising=False)
    monkeypatch.setenv("BIGDIPPER_AGENT_EXPERIMENT_TOOL_MODE", "mcp")
    with pytest.raises(ValueError, match="own"):
        agent_experiments._default_tool_loader("baseline_polystore")
    configs = []
    monkeypatch.setattr(agent_experiments, "load_configured_mcp_tools", lambda config=None: configs.append(config) or [])
    monkeypatch.setenv("BIGDIPPER_DB_TOOLS_MCP_CONFIG", "/configured/db-tools.yaml")
    agent_experiments._default_tool_loader("baseline_polystore")
    agent_experiments._default_tool_loader("mmdb_scholar")
    assert configs == ["/configured/db-tools.yaml", None]
    root = Path(server.__file__).parents[2] / "prompts/agent_research_report_flow/runtime_prompts"
    assert "DB_Tools" in agent_experiments._research_prompt("baseline_polystore", "", root)
    assert "DB_Tools" not in agent_experiments._research_prompt("mmdb_scholar", "", root)


def test_seed_similarity_reads_and_searches_the_same_database(monkeypatch):
    monkeypatch.setenv("BIGDIPPER_VECTOR_TABLE", "public.work_vec")
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_DIMENSIONS", "2")
    calls = []
    def query(sql, parameters):
        calls.append((sql, parameters))
        rows = [{"embedding": "[1,0]"}] if len(calls) == 1 else [{"paperid": "W2", "distance": .2, "score": .8}]
        return {"status": "success", "data": rows, "_execution": {"backend": "mmdb", "queryText": sql}}
    monkeypatch.setattr(server, "_parameter_query", query)
    result = json.loads(asyncio.run(server.find_similar_papers_by_paperid("W1", 3)))
    assert len(calls) == 2 and all('"public"."work_vec"' in sql for sql, _ in calls)
    assert calls[1][1]["embedding"] == "[1, 0]" and calls[1][1]["paper_id"] == "W1"
    assert '<> %(paper_id)s' in calls[1][0]
    assert result["data"][0] == {"paperid": "W2", "distance": .2, "score": .8, "rank": 1, "seed_paper_id": "W1"}
    bundle = ground_bundle([{"observationId": 1, "status": "success", "toolName": "find_similar_papers_by_paperid",
                            "arguments": {"paper_id": "W1"}, "result": result}])
    edge = next(e for e in bundle["graphIntent"]["edges"] if e["type"] == "similar")
    assert edge["score"] == .8 and edge["scoreKind"] == "score"
    assert edge["distance"] == .2


def test_missing_seed_is_an_error_not_an_empty_success(monkeypatch):
    monkeypatch.setenv("BIGDIPPER_VECTOR_TABLE", "work_vec")
    monkeypatch.setattr(server, "_parameter_query", lambda *_: {"data": []})
    result = json.loads(asyncio.run(server.find_similar_papers_by_paperid("missing")))
    assert result["status"] == "error"


@pytest.mark.parametrize("vector", [[True, 0], [float("nan"), 0], [float("inf"), 0], [1], []])
def test_invalid_query_vectors_are_rejected(monkeypatch, vector):
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_DIMENSIONS", "2")
    with pytest.raises(ValueError):
        validate_vector(vector)


def test_vector_identifiers_are_configuration_not_sql_fragments(monkeypatch):
    monkeypatch.setenv("BIGDIPPER_VECTOR_TABLE", "papers; DROP TABLE papers")
    with pytest.raises(ValueError):
        vector_search_sql()


def test_citation_query_preserves_direction_and_bound_parameters(monkeypatch):
    monkeypatch.setenv("BIGDIPPER_DATA_PROFILE", "cn")
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        return {"status": "success", "data": [{"citing_paperid": "W2", "cited_paperid": "W1"}]}
    monkeypatch.setattr(server, "_parameter_query", query)
    result = json.loads(asyncio.run(server.expand_paper_citations("W1", "citations", 5)))
    assert "WHERE cited_paperid = %(paper_id)s" in calls[0][0]
    assert calls[0][1] == {"paper_id": "W1", "limit": 5}
    assert result["data"][0]["citing_paperid"] == "W2"


def test_skill_documents_cover_paper_sections():
    from app.research_skills import read_research_skill
    for name in ("basic_access", "integrated_analysis", "semantic_synthesis"):
        doc = read_research_skill(name)
        for part in ("适用场景", "所需数据", "推荐步骤", "可复用 MMSQL", "结果检查规则", "停止条件"):
            assert part in doc
    tutorial = read_research_skill("mmsql")
    assert all(part in tutorial for part in ("基本语法", "模型操作", "跨模型组合", "查询示例", "错误修正"))
    assert "不稳定" not in tutorial and "HELMDB" not in tutorial
