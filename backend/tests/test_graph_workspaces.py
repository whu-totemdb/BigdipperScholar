from fastapi.testclient import TestClient


from app.main import create_app
from app.storage import SQLiteStorage
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider
from tests.conftest import DisabledLLM


def _client(tmp_path):
    return TestClient(
        create_app(
            storage=SQLiteStorage(tmp_path / "graph.db"),
            tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
            llm_client=DisabledLLM(),
        )
    )


def _register(client: TestClient, email: str = "graph@example.com") -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "strong-password", "displayName": "Test User"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


def _workspace(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "paper_search", "query": "Graph-RAG", "limit": 2},
    )
    assert response.status_code == 200
    return response.json()["workspace"]["id"]


def test_paper_search_and_detail_fallback_shape(tmp_path) -> None:
    client = _client(tmp_path)
    headers = _register(client)

    search = client.post(
        "/api/papers/search",
        headers=headers,
        json={"query": "Graph-RAG", "limit": 3, "yearFrom": 2020, "yearTo": 2026},
    )

    assert search.status_code == 200
    body = search.json()
    assert body["items"]
    assert body["items"][0]["nodeType"] == "paper"
    assert "warnings" in body

    paper_id = body["items"][0]["id"]
    detail = client.get(f"/api/papers/{paper_id}", headers=headers)

    assert detail.status_code == 200
    assert detail.json()["paper"]["id"] == paper_id


def test_graph_query_persists_graph_state(tmp_path) -> None:
    client = _client(tmp_path)
    headers = _register(client)
    workspace_id = _workspace(client, headers)

    graph_response = client.post(
        "/api/graphs/query",
        headers=headers,
        json={
            "queryType": "paper_references",
            "workspaceId": workspace_id,
            "seedPaperId": "p-graph-rag-2025",
            "limit": 5,
            "includeAuthors": True,
            "includeInstitutions": True,
        },
    )

    assert graph_response.status_code == 200
    graph = graph_response.json()
    assert graph["workspaceId"] == workspace_id
    assert graph["seedPaperId"] == "p-graph-rag-2025"
    assert graph["defaultViewMode"] == "graph"
    node_types = {node["nodeType"] for node in graph["nodes"]}
    assert node_types >= {"paper", "author"}
    assert "Research Research Institute" not in {node["label"] for node in graph["nodes"]}
    assert any(edge["type"] == "cites" for edge in graph["edges"])

    states = client.get(f"/api/workspaces/{workspace_id}/graph-states", headers=headers)
    assert states.status_code == 200
    assert states.json()["items"][0]["id"] == graph["graphId"]

    restored = client.get(f"/api/workspaces/{workspace_id}/graph-states/{graph['graphId']}", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["graphId"] == graph["graphId"]
    assert restored.json()["papers"]


def test_graph_state_workspace_ownership(tmp_path) -> None:
    client = _client(tmp_path)
    owner_headers = _register(client, "owner@example.com")
    other_headers = _register(client, "other@example.com")
    workspace_id = _workspace(client, owner_headers)

    response = client.post(
        "/api/graphs/query",
        headers=other_headers,
        json={
            "queryType": "paper_search",
            "workspaceId": workspace_id,
            "query": "Graph-RAG",
            "limit": 2,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "workspace_not_found"
