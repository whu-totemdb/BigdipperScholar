"""Browser-test-only API with synthetic, explicitly labelled observations.

Run only through the Playwright test server; never imported by app.main.
"""
from tempfile import TemporaryDirectory
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import SQLiteStorage
from app.evidence_provenance import ground_bundle

directory = TemporaryDirectory(prefix="bigdipper-browser-")
storage = SQLiteStorage(Path(directory.name) / "workspace.db")
app = create_app(storage=storage)
client = TestClient(app)
session = client.post("/api/auth/register", json={"email": "browser-fixture@example.test", "password": "browser-fixture-only", "displayName": "Browser Fixture"}).json()
headers = {"Authorization": f"Bearer {session['accessToken']}"}
workspace = client.post("/api/workspaces", headers=headers, json={"entryType": "agent_chat", "message": "Show the fixture evidence"}).json()["workspace"]
observations = [{"observationId": 0, "status": "success", "toolName": "execute_sciscinet_sql",
                 "arguments": {"sql": "SELECT fixture facts"}, "execution": {"backend": "fixture", "queryText": "SELECT fixture facts"},
                 "result": {"data": [
                     {"paperid": "fixture-P1", "title": "Fixture: Temporal Graph Foundations", "year": 2020, "abstract": "Synthetic browser-test abstract, not research evidence.", "authorid": "fixture-A1", "author_name": "Fixture Author", "institutionid": "fixture-I1", "institution_name": "Fixture Institute"},
                     {"paperid": "fixture-P2", "title": "Fixture: Temporal Graph Queries", "year": 2021, "authorid": "fixture-A1", "author_name": "Fixture Author"},
                     {"citing_paperid": "fixture-P2", "cited_paperid": "fixture-P1"}
                 ]}},
                {"observationId": 1, "status": "success", "toolName": "search_papers_by_natural_language",
                 "arguments": {"query": "fixture graphs"}, "result": {"data": [{"paperid": "fixture-P2", "title": "Fixture: Temporal Graph Queries", "score": .8, "rank": 1}]}}]
bundle = ground_bundle(observations, {"graphIntent": {"title": "Evidence Graph: Browser fixture"}})
answer = "Browser fixture report. Two synthetic papers share an author; P2 cites P1. The retrieval observation has score 0.8. These records are test fixtures."
artifact = {"status": "available", "runId": "browser-fixture", "title": "Evidence Graph: Browser fixture", "nodePreviewCount": len(bundle["graphIntent"]["nodes"]), "edgePreviewCount": len(bundle["graphIntent"]["edges"]), "warnings": []}
storage.add_message(workspace["conversationId"], "user", "Show the fixture evidence")
storage.add_message(workspace["conversationId"], "assistant", answer)
storage.save_agent_run_artifact(run_id="browser-fixture", conversation_id=workspace["conversationId"], workspace_id=workspace["id"],
                                route="research_report", status="completed", final_answer=answer, research_report=answer,
                                evidence_bundle=bundle, selected_observations=observations, graph_artifact=artifact)


@app.get("/test/session")
def fixture_session():
    return {"session": session, "workspaceId": workspace["id"]}
