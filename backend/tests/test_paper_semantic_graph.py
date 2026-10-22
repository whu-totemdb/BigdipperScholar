from fastapi.testclient import TestClient

from app.graph_demo import GraphDemoService
from app.main import create_app
from app.storage import SQLiteStorage
from app.tools import MockToolProvider
from tests.conftest import DisabledLLM


def _client(tmp_path):
    storage = SQLiteStorage(tmp_path / "semantic.db")
    service = GraphDemoService(
        fallback_provider=MockToolProvider(),
        semantic_sqlite_path=storage.db_path,
    )
    return TestClient(create_app(storage=storage, tool_provider=MockToolProvider(), llm_client=DisabledLLM(), graph_service=service)), storage


def _register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": "semantic@example.com", "password": "strong-password", "displayName": "Semantic User"},
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


def _insert_semantic_graph(storage: SQLiteStorage) -> None:
    with storage._connect() as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS paper_semantic_properties;
            DROP TABLE IF EXISTS paper_semantic_relations;
            DROP TABLE IF EXISTS paper_semantic_entities;
            DROP TABLE IF EXISTS paper_semantic_graphs;
            DROP TABLE IF EXISTS paper_kg_sources;

            CREATE TABLE paper_kg_sources (
                paperid TEXT NOT NULL,
                corpus_id TEXT NOT NULL UNIQUE,
                dataset TEXT NOT NULL,
                venue TEXT,
                source_path TEXT NOT NULL,
                source_checksum TEXT NOT NULL,
                raw_record TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE paper_semantic_graphs (
                paperid TEXT PRIMARY KEY,
                corpus_id TEXT NOT NULL,
                annotation_type TEXT,
                language TEXT,
                schema_source TEXT,
                evidence_sentence TEXT,
                raw_annotation TEXT
            );

            CREATE TABLE paper_semantic_entities (
                paperid TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                entity_type TEXT,
                entity_comment TEXT,
                entity_source TEXT,
                raw TEXT,
                PRIMARY KEY (paperid, entity_id)
            );

            CREATE TABLE paper_semantic_relations (
                paperid TEXT NOT NULL,
                relation_id TEXT NOT NULL,
                relation_name TEXT NOT NULL,
                head_entity_id TEXT NOT NULL,
                tail_entity_id TEXT NOT NULL,
                raw TEXT,
                PRIMARY KEY (paperid, relation_id)
            );

            CREATE TABLE paper_semantic_properties (
                paperid TEXT NOT NULL,
                property_id TEXT NOT NULL,
                property_name TEXT NOT NULL,
                property_type TEXT,
                property_value TEXT,
                entity_id TEXT,
                raw TEXT,
                PRIMARY KEY (paperid, property_id)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO paper_kg_sources
                (paperid, corpus_id, dataset, venue, source_path, source_checksum, raw_record)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "DBLP-KG-ICDE-003", "KG-ICDE", "ICDE", "/tmp/kg.json", "sha", None),
        )
        conn.execute(
            """
            INSERT INTO paper_semantic_graphs
                (paperid, corpus_id, annotation_type, language, schema_source, evidence_sentence, raw_annotation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "DBLP-KG-ICDE-003", None, "eng", "schema", "evidence", None),
        )
        conn.execute(
            """
            INSERT INTO paper_semantic_entities
                (paperid, entity_id, entity_name, entity_type, entity_comment, entity_source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "ent001", "property graph", "DataModel", "graph model", "schema", None),
        )
        conn.execute(
            """
            INSERT INTO paper_semantic_entities
                (paperid, entity_id, entity_name, entity_type, entity_comment, entity_source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "ent002", "query optimization", "Technique", "optimization", "schema", None),
        )
        conn.execute(
            """
            INSERT INTO paper_semantic_relations
                (paperid, relation_id, relation_name, head_entity_id, tail_entity_id, raw)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "rel001", "optimizesFor", "ent002", "ent001", None),
        )
        conn.execute(
            """
            INSERT INTO paper_semantic_properties
                (paperid, property_id, property_name, property_type, property_value, entity_id, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DBLP-KG-ICDE-003", "prop001", "scenario", "attribute", "query performance", "ent001", None),
        )


def test_paper_semantic_graph_returns_paper_entities_and_relation(tmp_path) -> None:
    client, storage = _client(tmp_path)
    headers = _register(client)
    workspace_id = _workspace(client, headers)
    _insert_semantic_graph(storage)

    response = client.post(
        "/api/graphs/query",
        headers=headers,
        json={
            "queryType": "paper_semantic_graph",
            "workspaceId": workspace_id,
            "seedPaperId": "DBLP-KG-ICDE-003",
        },
    )

    assert response.status_code == 200
    graph = response.json()
    assert graph["seedPaperId"] == "DBLP-KG-ICDE-003"
    assert {node["nodeType"] for node in graph["nodes"]} == {"paper", "semantic_entity"}
    entity_nodes = [node for node in graph["nodes"] if node["nodeType"] == "semantic_entity"]
    assert len(entity_nodes) == 2
    assert entity_nodes[0]["data"]["raw"] is None
    assert entity_nodes[0]["data"]["properties"]
    assert entity_nodes[0]["data"]["properties"][0]["raw"] is None
    assert graph["edges"][0]["label"] == "optimizesFor"
    assert graph["edges"][0]["data"]["raw"] is None
    assert graph["edges"][0]["type"] == "semantic_relation"


def test_missing_paper_semantic_graph_returns_seed_warning(tmp_path) -> None:
    client, _storage = _client(tmp_path)
    headers = _register(client)
    workspace_id = _workspace(client, headers)

    response = client.post(
        "/api/graphs/query",
        headers=headers,
        json={
            "queryType": "paper_semantic_graph",
            "workspaceId": workspace_id,
            "seedPaperId": "DBLP-KG-ICDE-missing",
        },
    )

    assert response.status_code == 200
    graph = response.json()
    assert graph["nodes"][0]["nodeType"] == "paper"
    assert graph["edges"] == []
    assert any("Semantic graph not found" in warning for warning in graph["warnings"])
