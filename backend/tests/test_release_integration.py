"""Real local PostgreSQL + MCP stdio + HTTP API; scripted LLM/embedding fixtures.

Opt in with BIGDIPPER_TEST_PG_BIN pointing to a PostgreSQL bin directory.
Only a freshly initialized temporary cluster is used, never the configured DB.
This verifies wiring and provenance, not scientific quality or MMDB operators.
"""
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.llm import LLMResult
from app.main import create_app
from app.mcp_client import load_configured_mcp_tools
from app.agent_experiments import _invoke_tool, _tool_output_failed
from app.storage import SQLiteStorage


def call(name, arguments):
    return LLMResult(content="Next research step", used_llm=True, message={
        "role": "assistant", "content": "Next research step", "tool_calls": [{
            "id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]})


class ScriptedResearchLLM:
    is_configured = True
    model = "scripted-integration-fixture"

    def __init__(self):
        self.step = 0

    def complete(self, messages, tools=None, **kwargs):
        if tools and any(t["function"]["name"] == "run_research_report" for t in tools):
            return call("run_research_report", {"researchQuery": "Compare the fixture papers and their evidence"})
        if tools:
            self.step += 1
            if self.step == 1:
                assert "fixture_papers" in str(messages)
                return call("read_research_skill", {"name": "semantic_synthesis"})
            if self.step == 2:
                return call("execute_sciscinet_sql", {"sql": "SELECT missing_column FROM fixture_papers"})
            if self.step == 3:
                return call("execute_sciscinet_sql", {"sql": "SELECT * FROM fixture_papers WHERE false"})
            if self.step == 4:
                return call("execute_sciscinet_sql", {"sql": "SELECT wid AS paperid, title, year, document->>'abstract' AS abstract, 'A1' AS authorid, 'Fixture Author' AS author_name, 'I1' AS institutionid, 'Fixture Institute' AS institution_name FROM fixture_papers ORDER BY wid"})
            if self.step == 5:
                observation = json.loads(messages[-1]["content"])
                assert "SELECT wid AS paperid" in observation["execution"]["queryText"]
                assert observation["status"] == "success"
                return call("execute_sciscinet_sql", {"sql": "SELECT citing AS citing_paperid, cited AS cited_paperid FROM fixture_citations"})
            if self.step == 6:
                return call("search_papers_by_natural_language", {"query": "temporal graphs", "limit": 2})
            if self.step == 7:
                observation = json.loads(messages[-1]["content"])
                assert "%(embedding)s" in observation["execution"]["queryText"]
                assert observation["execution"]["parameters"]["embedding"]["dimensions"] == 2
                assert observation["execution"]["parameters"]["query"] == "temporal graphs"
                return call("select_report_observations", {"mode": "finalize", "observationIds": [4, 5, 6], "rationale": "Select actual papers, citation and semantic result; reject schema/skill/error/empty.", "analyses": [{"observationIds": [4, 5, 6], "summary": "Two papers, their citation and semantic scores are available."}]})
            return LLMResult(content="Exploration complete", used_llm=True, message={"role": "assistant", "content": "Exploration complete"})
        payload = json.loads(messages[-1]["content"])
        ids = [o["observationId"] for o in payload["selectedExplorations"]]
        assert ids == [4, 5, 6], ids
        if "observedEvidence" in payload:
            assert payload["observedEvidence"]
            return LLMResult(content=json.dumps({"evidence": [{"evidenceId": "obs-4-row-0", "sourceObservationIds": [4], "supportingClaims": ["First paper"]}], "graphIntent": {"title": "Evidence Graph: Fixture", "nodes": [{"id": "invented"}]}}), used_llm=True)
        return LLMResult(content="Fixture report: P1 (2020) and P2 (2021), Fixture Author, Fixture Institute. P2 cites P1. Abstract-only analysis. [observations 4,5,6]", used_llm=True)


@pytest.fixture
def local_database(tmp_path, monkeypatch):
    bindir = os.getenv("BIGDIPPER_TEST_PG_BIN")
    image = os.getenv("BIGDIPPER_TEST_POSTGRES_IMAGE")
    if image:
        name = "bigdipper-release-test-" + uuid.uuid4().hex[:12]
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", f"POSTGRES_USER={getpass.getuser()}",
                        "-p", "127.0.0.1::5432", image], check=True, capture_output=True)
        try:
            port = subprocess.check_output(["docker", "port", name, "5432"], text=True).strip().rsplit(":", 1)[1]
            for attempt in range(100):
                try:
                    conn = psycopg2.connect(host="127.0.0.1", port=port, user=getpass.getuser(), dbname="postgres", connect_timeout=1)
                    conn.close()
                    break
                except psycopg2.OperationalError:
                    time.sleep(.2)
            else:
                pytest.fail("Temporary PostgreSQL did not become ready")
            _configure_fixture_database("127.0.0.1", port, tmp_path, monkeypatch)
            yield
        finally:
            subprocess.run(["docker", "stop", name], check=True, capture_output=True)
        return
    if not bindir:
        pytest.skip("Set BIGDIPPER_TEST_PG_BIN or BIGDIPPER_TEST_POSTGRES_IMAGE for isolated integration")
    pg = Path(bindir)
    if not (pg / "initdb").is_file():
        pytest.fail("BIGDIPPER_TEST_PG_BIN does not contain initdb")
    data, sockets = tmp_path / "pg-data", tmp_path / "sockets"
    sockets.mkdir()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run([str(pg / "initdb"), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([str(pg / "pg_ctl"), "-D", str(data), "-l", str(tmp_path / "postgres.log"), "-o", f"-k {sockets} -p {port} -h ''", "-w", "start"], check=True, capture_output=True)
    try:
        _configure_fixture_database(str(sockets), port, tmp_path, monkeypatch)
        yield
    finally:
        subprocess.run([str(pg / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"], check=True, capture_output=True)


def _configure_fixture_database(host, port, tmp_path, monkeypatch):
        conn = psycopg2.connect(host=host, port=port, user=getpass.getuser(), dbname="postgres")
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE fixture_papers(wid text PRIMARY KEY, title text, year int, document jsonb, embedding float8[])")
            cursor.execute("INSERT INTO fixture_papers VALUES ('P1','Temporal Graph Foundations',2020,'{\"abstract\":\"Fixture graph study.\"}',ARRAY[1.,0.]), ('P2','Temporal Graph Queries',2021,'{\"abstract\":\"Fixture query study.\"}',ARRAY[0.8,0.6])")
            cursor.execute("CREATE TABLE fixture_citations(citing text, cited text)")
            cursor.execute("INSERT INTO fixture_citations VALUES ('P2','P1')")
        conn.close()
        for key, value in {"DB_HOST": host, "DB_PORT": str(port), "DB_USER": getpass.getuser(), "DB_PASSWORD": "", "DB_NAME": "postgres",
                           "BIGDIPPER_DATA_PROFILE": "generic", "BIGDIPPER_TABLE_PREFIX": "fixture_", "BIGDIPPER_DB_SCHEMA": "public",
                           "BIGDIPPER_MCP_CONFIG": str(Path(__file__).resolve().parents[1] / "mcp_servers.yaml"),
                           "BIGDIPPER_RESEARCH_REPORT_CHAT_MAX_ROUNDS": "8", "BIGDIPPER_RESEARCH_REPORT_CHAT_LOG_DIR": str(tmp_path / "runs"),
                           "AGENT_BACKEND": "research_report", "BIGDIPPER_DB_STATEMENT_TIMEOUT_MS": "300"}.items():
            monkeypatch.setenv(key, value)


def test_database_mcp_report_evidence_history(local_database, tmp_path, monkeypatch):
    class EmbeddingFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/embed"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["query"] == "temporal graphs"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"embedding":[1.0,0.0]}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingFixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sql = tmp_path / "semantic.sql"
    sql.write_text("""WITH q AS (SELECT %(embedding)s::jsonb AS v)
        SELECT p.wid AS paperid, p.title, p.year,
        (p.embedding[1]*(q.v->>0)::float8 + p.embedding[2]*(q.v->>1)::float8) /
        (sqrt(p.embedding[1]^2+p.embedding[2]^2)*sqrt(((q.v->>0)::float8)^2+((q.v->>1)::float8)^2)) AS score
        FROM fixture_papers p CROSS JOIN q ORDER BY score DESC LIMIT %(limit)s""")
    monkeypatch.setenv("BIGDIPPER_SEMANTIC_SQL_PATH", str(sql))
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setenv("BIGDIPPER_EMBEDDING_MODEL", "test-fixture-2d")
    try:
        storage = SQLiteStorage(tmp_path / "app.db")
        client = TestClient(create_app(storage=storage, llm_client=ScriptedResearchLLM()))
        registration = client.post("/api/auth/register", json={"email": "fixture@example.test", "password": "fixture-password", "displayName": "Fixture"}).json()
        headers = {"Authorization": f"Bearer {registration['accessToken']}"}
        workspace = client.post("/api/workspaces", headers=headers, json={"entryType": "agent_chat", "message": "Analyse fixture papers"}).json()["workspace"]
        response = client.post("/api/chat", headers=headers, json={"workspaceId": workspace["id"], "message": "Analyse fixture papers", "runId": "release-integration", "includeTrace": True})
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed", result
        assert "Fixture report" in result["message"]
        evidence = client.get(f"/api/chat/runs/release-integration/evidence?workspace_id={workspace['id']}", headers=headers)
        assert evidence.status_code == 200
        bundle = evidence.json()["evidenceBundle"]
        assert "invented" not in json.dumps(bundle)
        assert {o["observationId"] for o in evidence.json()["selectedObservations"]} == {4, 5, 6}
        semantic = [e for e in bundle["evidence"] if e["evidenceType"] == "semantic"]
        assert sorted(round(e["content"]["score"], 2) for e in semantic) == [.8, 1.0]
        assert semantic[0]["sources"][0]["query"]
        graph = client.post("/api/chat/runs/release-integration/evidence-graph", headers=headers, json={"workspaceId": workspace["id"]}).json()
        assert graph["status"] == "completed", graph
        assert {e["type"] for e in graph["graph"]["edges"]} >= {"cites", "authored_by", "affiliated_with", "has_field"}
        restored = client.get(f"/api/workspaces/{workspace['id']}", headers=headers).json()
        assert restored["initialChatMessages"][-1]["graphArtifact"]["status"] == "available"
        assert restored["activeGraph"]["graphId"] == graph["graph"]["graphId"]
        assert client.get(f"/api/chat/runs/release-integration/evidence?workspace_id={workspace['id']}").status_code == 401

        # Actual MCP transport and server-side statement timeout/read-only boundary.
        tools = {tool.name: tool for tool in load_configured_mcp_tools()}
        write = _invoke_tool(tools["execute_sciscinet_sql"], {"sql": "WITH gone AS (DELETE FROM fixture_papers RETURNING wid) SELECT * FROM gone"})
        assert _tool_output_failed(write)
        timeout = _invoke_tool(tools["execute_sciscinet_sql"], {"sql": "SELECT pg_sleep(2)"})
        assert _tool_output_failed(timeout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
