import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import SQLiteStorage
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider
from tests.conftest import DisabledLLM


def _client(tmp_path):
    return TestClient(
        create_app(
            storage=SQLiteStorage(tmp_path / "v2.db"),
            tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
            llm_client=DisabledLLM(),
        )
    )


def _register(client: TestClient) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/auth/register",
        json={
            "email": "User@Example.com",
            "password": "strong-password",
            "displayName": "User Name",
        },
    )
    assert response.status_code == 200
    body = response.json()
    return body, {"Authorization": f"Bearer {body['accessToken']}"}


def test_auth_register_login_me_and_logout(tmp_path) -> None:
    client = _client(tmp_path)
    body, headers = _register(client)

    assert body["user"]["email"] == "user@example.com"
    assert body["user"]["displayName"] == "User Name"
    assert body["accessToken"].startswith("bds_")

    duplicate = client.post(
        "/api/auth/register",
        json={"email": "user@example.com", "password": "strong-password", "displayName": "Other"},
    )
    assert duplicate.status_code == 409

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["id"] == body["user"]["id"]

    login = client.post("/api/auth/login", json={"email": "user@example.com", "password": "strong-password"})
    assert login.status_code == 200
    assert login.json()["user"]["id"] == body["user"]["id"]

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json() == {"ok": True}
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_paper_search_requires_auth_and_returns_frontend_nodes(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.post("/api/papers/search", json={"query": "graph RAG"}).status_code == 401
    _, headers = _register(client)

    response = client.post("/api/papers/search", headers=headers, json={"query": "graph RAG", "limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "graph RAG"
    assert body["items"]
    assert body["items"][0]["nodeType"] == "paper"
    assert body["items"][0]["position"] == {"x": 0.0, "y": 0.0}


def test_agent_workspace_restores_full_conversation_history(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "history.db")
    client = TestClient(
        create_app(
            storage=storage,
            tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
            llm_client=DisabledLLM(),
        )
    )
    _, headers = _register(client)

    workspace_response = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "agent_chat", "message": "第一句问题"},
    )
    assert workspace_response.status_code == 200
    workspace_body = workspace_response.json()
    conversation_id = workspace_body["workspace"]["conversationId"]

    storage.add_message(conversation_id, "user", "第一句问题")
    storage.add_message(conversation_id, "assistant", "第一句回答")
    storage.add_message(conversation_id, "user", "第二句问题")
    storage.add_message(conversation_id, "assistant", "第二句回答")

    restored = client.get(f"/api/workspaces/{workspace_body['workspace']['id']}", headers=headers)
    assert restored.status_code == 200
    messages = restored.json()["initialChatMessages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "第一句问题"
    assert messages[2]["content"] == "第二句问题"
    assert not any(message.get("pendingSend") for message in messages)


def test_agent_workspace_restore_does_not_return_pending_seed(tmp_path) -> None:
    client = _client(tmp_path)
    _, headers = _register(client)

    created = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "agent_chat", "message": "只在创建响应里 pending"},
    )
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["initialChatMessages"][0]["pendingSend"] is True

    restored = client.get(f"/api/workspaces/{created_body['workspace']['id']}", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["initialChatMessages"] == []


def test_chat_with_workspace_id_persists_to_paper_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    client = _client(tmp_path)
    _, headers = _register(client)

    created = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "paper_search", "query": "Graph-RAG", "limit": 2},
    )
    assert created.status_code == 200
    workspace = created.json()["workspace"]

    chat = client.post(
        "/api/chat",
        headers=headers,
        json={
            "workspaceId": workspace["id"],
            "conversationId": "ignored-client-conversation",
            "message": "search papers about graph RAG",
        },
    )
    assert chat.status_code == 200
    assert chat.json()["conversationId"] == workspace["conversationId"]

    restored = client.get(f"/api/workspaces/{workspace['id']}", headers=headers)
    assert restored.status_code == 200
    messages = restored.json()["initialChatMessages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "search papers about graph RAG"
    assert not any(message["pendingSend"] for message in messages)


def test_storage_backfills_null_workspace_conversations(tmp_path) -> None:
    db_path = tmp_path / "backfill.db"
    storage = SQLiteStorage(db_path)
    workspace, _ = storage.create_paper_workspace("user_1", "Graph-RAG", [], [], paper_count=0)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE workspaces SET conversation_id = NULL WHERE id = ?", (workspace.id,))

    backfilled = SQLiteStorage(db_path).get_workspace_by_id(workspace.id)

    assert backfilled is not None
    assert backfilled.conversation_id


def test_workspace_paper_search_and_agent_chat_flows(tmp_path) -> None:
    client = _client(tmp_path)
    _, headers = _register(client)

    paper_response = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "paper_search", "query": "Graph-RAG", "limit": 3},
    )
    assert paper_response.status_code == 200
    paper_body = paper_response.json()
    assert paper_body["workspace"]["entryType"] == "paper_search"
    assert paper_body["workspace"]["conversationId"]
    assert paper_body["initialViewMode"] == "list"
    assert paper_body["initialGraph"]["graphId"] == paper_body["workspace"]["activeGraphStateId"]
    assert paper_body["initialGraph"]["defaultViewMode"] == "list"
    assert paper_body["initialGraph"]["seedNodeId"] is None
    assert paper_body["initialGraph"]["seedPaperId"] is None
    assert paper_body["initialGraph"]["nodes"]
    assert all(node["nodeType"] == "paper" for node in paper_body["initialGraph"]["nodes"])
    assert paper_body["initialGraph"]["edges"] == []

    paper_detail = client.get(f"/api/workspaces/{paper_body['workspace']['id']}", headers=headers)
    assert paper_detail.status_code == 200
    assert paper_detail.json()["activeGraph"]["graphId"] == paper_body["initialGraph"]["graphId"]
    assert all(node["nodeType"] == "paper" for node in paper_detail.json()["activeGraph"]["nodes"])
    assert paper_detail.json()["activeGraph"]["edges"] == []

    agent_response = client.post(
        "/api/workspaces",
        headers=headers,
        json={"entryType": "agent_chat", "message": "请帮我分析 Data Agent 方向"},
    )
    assert agent_response.status_code == 200
    agent_body = agent_response.json()
    assert agent_body["workspace"]["entryType"] == "agent_chat"
    assert agent_body["workspace"]["conversationId"]
    assert agent_body["initialViewMode"] == "default"
    assert agent_body["initialGraph"]["nodes"] == []
    assert agent_body["initialChatMessages"] == [
        {
            "role": "user",
            "content": "请帮我分析 Data Agent 方向",
            "createdAt": agent_body["initialChatMessages"][0]["createdAt"],
            "pendingSend": True,
            "runId": None,
            "graphArtifact": None,
            "researchSteps": [],
        }
    ]

    history = client.get("/api/workspaces", headers=headers)
    assert history.status_code == 200
    assert [item["entryType"] for item in history.json()["items"]] == ["agent_chat", "paper_search"]

    delete_response = client.delete(f"/api/workspaces/{paper_body['workspace']['id']}", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}
    assert client.get(f"/api/workspaces/{paper_body['workspace']['id']}", headers=headers).status_code == 404

    history_after_delete = client.get("/api/workspaces", headers=headers)
    assert history_after_delete.status_code == 200
    assert [item["entryType"] for item in history_after_delete.json()["items"]] == ["agent_chat"]
