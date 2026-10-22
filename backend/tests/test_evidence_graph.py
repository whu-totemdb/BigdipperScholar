from fastapi.testclient import TestClient

from app.main import create_app
from app.models import PaperSearchItem
from app.storage import SQLiteStorage
from tests.conftest import DisabledLLM
from app.evidence_graph import EvidenceGraphBuilder, _assemble_deterministic_graph_intent


def test_evidence_graph_endpoint_builds_saves_and_caches(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "evidence.db")
    client = TestClient(create_app(storage=storage, llm_client=DisabledLLM(), graph_service=FakeGraphService()))
    _, headers = _register(client, "user@example.com")
    workspace = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "agent_chat", "message": "分析 GNN"},
    ).json()["workspace"]
    run_id = "run_evidence_1"
    storage.save_agent_run_artifact(
        run_id=run_id,
        conversation_id=workspace["conversationId"],
        workspace_id=workspace["id"],
        route="research_report",
        status="completed",
        final_answer="最终回答",
        research_report="报告内容",
        evidence_bundle=_bundle(),
        research_response={"status": "completed"},
        selected_observations=[
            {
                "observationId": 0,
                "round": 1,
                "toolName": "search_papers_by_keywords",
                "status": "success",
                "result": '[{"paperId":"W1","title":"Graph Neural Networks","year":2021,"authors":["Alice"],"venue":"Graph University"},{"paperId":"W2","title":"Graph Attention Networks","year":2022,"authors":["Bob"],"venue":"Graph Institute"}]',
            }
        ],
        events_log_path="logs/research_report_chat/run_evidence_1/events.jsonl",
        graph_artifact={"status": "available", "runId": run_id, "title": "Evidence Graph: GNN", "reason": "fake"},
    )

    unauthorized = client.post(f"/api/chat/runs/{run_id}/evidence-graph", json={"workspaceId": workspace["id"]})
    assert unauthorized.status_code == 401

    first = client.post(f"/api/chat/runs/{run_id}/evidence-graph", headers=headers, json={"workspaceId": workspace["id"], "maxNodes": 20})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "completed"
    assert body["graph"]["graphId"].startswith("graph_")
    assert body["graph"]["workspaceId"] == workspace["id"]
    assert {node["nodeType"] for node in body["graph"]["nodes"]} >= {"paper", "author"}
    assert not any(node["nodeType"] == "institution" for node in body["graph"]["nodes"])
    assert not body["warnings"]

    saved = storage.get_agent_run_artifact(run_id)
    assert saved is not None
    assert saved["graph_state_id"] == body["graph"]["graphId"]
    assert storage.get_graph_response(workspace["id"], body["graph"]["graphId"]) is not None

    second = client.post(f"/api/chat/runs/{run_id}/evidence-graph", headers=headers, json={"workspaceId": workspace["id"]})
    assert second.status_code == 200
    assert second.json()["graph"]["graphId"] == body["graph"]["graphId"]

    _, other_headers = _register(client, "other@example.com")
    other_workspace = client.post(
        "/api/workspaces",
        headers=other_headers,
        json={"entryType": "agent_chat", "message": "其他"},
    ).json()["workspace"]
    forbidden = client.post(f"/api/chat/runs/{run_id}/evidence-graph", headers=other_headers, json={"workspaceId": other_workspace["id"]})
    assert forbidden.status_code == 404


def test_storage_persists_agent_run_artifacts(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "artifact.db")
    conversation_id = storage.ensure_conversation()

    storage.save_agent_run_artifact(
        run_id="run_1",
        conversation_id=conversation_id,
        workspace_id="ws_1",
        route="direct",
        status="completed",
        final_answer="hello",
        evidence_bundle={"shouldRenderGraph": False},
        selected_observations=[],
        graph_artifact={"status": "none"},
    )

    artifact = storage.get_agent_run_artifact("run_1")
    assert artifact is not None
    assert artifact["conversation_id"] == conversation_id
    assert artifact["workspace_id"] == "ws_1"
    assert artifact["evidence_bundle"] == {"shouldRenderGraph": False}


def test_deterministic_assembler_preserves_retrieval_provenance() -> None:
    intent = _assemble_deterministic_graph_intent(
        {"graphIntent": {"title": "Evidence", "nodes": [], "edges": []}},
        [
            {
                "observationId": 4,
                "toolName": "search_papers_by_natural_language",
                "status": "success",
                "arguments": {"query": "temporal graph query"},
                "result": '[{"paperId":"W1","title":"Temporal Graph Index","score":0.83,"rank":1}]',
            }
        ],
    )
    assert any(node["nodeType"] == "field" and node["toolName"] == "search_papers_by_natural_language" for node in intent["nodes"])
    assert any(node["nodeType"] == "paper" and node["id"] == "W1" for node in intent["nodes"])
    provenance_edges = [edge for edge in intent["edges"] if edge["type"] == "has_field"]
    assert provenance_edges
    assert provenance_edges[0]["sourceObservationIds"] == [4]
    assert provenance_edges[0]["score"] == 0.83


def test_report_mentioned_paper_is_reserved_before_graph_limit() -> None:
    bundle = _bundle()
    bundle["graphIntent"]["nodes"][0]["mentionedInReport"] = False
    builder = EvidenceGraphBuilder(FakeGraphService())

    result = builder.build(
        run_id="run_report_entity_reservation",
        workspace_id="ws_1",
        evidence_bundle=bundle,
        selected_observations=[{"observationId": 0, "status": "success", "toolName": "query", "result": {"data": [{"paperid": "W1", "title": "Graph Neural Networks"}, {"paperid": "W2", "title": "Graph Attention Networks"}]}}],
        report="代表论文是 Graph Attention Networks（W2）。",
        max_nodes=1,
        include_authors=False,
        include_institutions=False,
        include_citation_edges=False,
    )

    assert result.graph is not None
    assert [node.id for node in result.graph.nodes] == ["W2"]
    assert result.missing_report_entities == []


def _register(client: TestClient, email: str):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "strong-password", "displayName": "User"},
    )
    assert response.status_code == 200
    body = response.json()
    return body, {"Authorization": f"Bearer {body['accessToken']}"}


def _bundle():
    return {
        "shouldRenderGraph": True,
        "reason": "report contains graphable paper evidence",
        "evidence": [
            {
                "evidenceId": "ev_1",
                "evidenceType": "paper",
                "label": "Graph Neural Networks",
                "paperId": "W1",
                "title": "Graph Neural Networks",
                "sourceObservationIds": [0],
                "confidence": 0.9,
            }
        ],
        "citations": [
            {
                "paperId": "W1",
                "title": "Graph Neural Networks",
                "reason": "representative paper",
                "sourceObservationIds": [0],
                "confidence": 0.9,
            },
            {
                "paperId": "W2",
                "title": "Graph Attention Networks",
                "reason": "related representative paper",
                "sourceObservationIds": [0],
                "confidence": 0.8,
            },
        ],
        "graphIntent": {
            "title": "Evidence Graph: GNN",
            "nodes": [
                {"refId": "paper_1", "nodeType": "paper", "id": "W1", "label": "Graph Neural Networks", "sourceObservationIds": [0], "confidence": 0.9},
                {"refId": "paper_2", "nodeType": "paper", "id": "W2", "label": "Graph Attention Networks", "sourceObservationIds": [0], "confidence": 0.8},
            ],
            "edges": [],
            "expansion": {"includeCitationEdges": True, "maxNodes": 80},
        },
        "missingReportEntities": [],
    }


class FakeGraphService:
    def __init__(self) -> None:
        self.neo4j_service = FakeNeo4j()

    def get_paper(self, paper_id: str):
        return (
            PaperSearchItem(
                id=paper_id,
                nodeType="paper",
                label="Alice, 2021",
                title="Graph Attention Networks" if paper_id == "W2" else "Graph Neural Networks",
                year=2021,
                authors=["Bob"] if paper_id == "W2" else ["Alice"],
                venue="Graph Institute" if paper_id == "W2" else "Graph University",
                abstract="",
                doi=None,
                citationCount=12,
                topic="Graph Institute" if paper_id == "W2" else "Graph University",
                summary="Graph Attention Networks" if paper_id == "W2" else "Graph Neural Networks",
                metric=12.0,
                position={"x": 0, "y": 0},
            ),
            [],
        )


class FakeNeo4j:
    def induced_citation_edges(self, paper_ids):
        raise RuntimeError("neo4j disabled in test")
