import json

from fastapi.testclient import TestClient

from app.global_semantic_graph import GlobalSemanticGraphService, normalize_entity_name
from app.main import create_app
from app.storage import SQLiteStorage
from app.tools import MockToolProvider
from tests.conftest import DisabledLLM


def _client(tmp_path):
    storage = SQLiteStorage(tmp_path / "global-semantic.db")
    service = GlobalSemanticGraphService(sqlite_path=storage.db_path)
    app = create_app(
        storage=storage,
        tool_provider=MockToolProvider(),
        llm_client=DisabledLLM(),
        global_semantic_graph_service=service,
    )
    client = TestClient(app)
    response = client.post(
        "/api/auth/register",
        json={
            "email": "global-semantic@example.com",
            "password": "strong-password",
            "displayName": "Global Semantic User",
        },
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['accessToken']}"}
    return client, storage, headers


def _insert_fixture(storage: SQLiteStorage) -> None:
    entities = [
        (
            "g_sql",
            "ng_sql",
            "SQL",
            "sql",
            "QueryLanguage",
            "querylanguage",
            5,
            ["KG-ICDE", "ASTRO-KG"],
            ["DBLP-KG-ICDE-1093", "ASTRO-KG-00001"],
        ),
        (
            "g_mapreduce",
            "ng_mapreduce",
            "MapReduce",
            "mapreduce",
            "System",
            "system",
            4,
            ["KG-ICDE"],
            ["p1", "p3"],
        ),
        (
            "g_optimizer",
            "ng_optimizer",
            "query-optimizer",
            "query optimizer",
            "Method",
            "method",
            3,
            ["KG-ICDE"],
            ["p2", "p3"],
        ),
    ]
    with storage._connect() as connection:
        connection.execute(
            """
            CREATE TABLE cn_paperdetails (
                paperid TEXT PRIMARY KEY,
                title TEXT,
                year INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO cn_paperdetails (paperid, title, year) VALUES (?, ?, ?)",
            [
                ("DBLP-KG-ICDE-1093", "Scalable SQL Processing", 2018),
                ("ASTRO-KG-00001", "Mapping the Early Universe", 2020),
                ("p1", "Distributed Data Processing", 2019),
                ("p2", "Semantic Query Optimization", 2021),
            ],
        )
        connection.execute(
            """
            INSERT INTO global_semantic_graph_builds (
                build_id, dataset_scope, normalization_version,
                min_paper_count, status, parameters, stats, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("build-1", "all", "v1", 2, "completed", "{}", "{}", "2026-08-23T12:00:00+00:00"),
        )
        for entity_id, name_group_id, name, normalized_name, entity_type, normalized_type, paper_count, datasets, examples in entities:
            connection.execute(
                """
                INSERT INTO global_semantic_entities (
                    dataset_scope, global_entity_id, name_group_id,
                    normalization_version, canonical_name, normalized_name,
                    canonical_entity_type, normalized_entity_type,
                    type_distribution, build_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "all",
                    entity_id,
                    name_group_id,
                    "v1",
                    name,
                    normalized_name,
                    entity_type,
                    normalized_type,
                    json.dumps({entity_type: paper_count}),
                    "build-1",
                ),
            )
            connection.execute(
                """
                INSERT INTO global_semantic_entity_stats (
                    dataset_scope, global_entity_id, paper_count, mention_count,
                    datasets, example_papers, eligible, build_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "all",
                    entity_id,
                    paper_count,
                    paper_count,
                    json.dumps(datasets),
                    json.dumps(examples),
                    1,
                    "build-1",
                ),
            )
        connection.execute(
            """
            INSERT INTO global_semantic_entity_mentions (
                dataset_scope, paperid, entity_id, global_entity_id,
                dataset, dataset_group, original_name, original_entity_type, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("all", "p1", "ent-sql", "g_sql", "KG-ICDE", "dblp", "SQL", "QueryLanguage", "build-1"),
        )
        connection.executemany(
            """
            INSERT INTO global_semantic_edges (
                dataset_scope, edge_id, edge_type,
                source_global_entity_id, target_global_entity_id,
                relation_name, normalized_relation_name, directed,
                paper_count, mention_count, evidence_papers, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "all",
                    "edge-co",
                    "co_occurrence",
                    "g_sql",
                    "g_mapreduce",
                    None,
                    None,
                    0,
                    3,
                    3,
                    json.dumps(["p1", "p2", "p3"]),
                    "build-1",
                ),
                (
                    "all",
                    "edge-semantic",
                    "semantic_relation",
                    "g_optimizer",
                    "g_sql",
                    "optimizesFor",
                    "optimizesfor",
                    1,
                    2,
                    2,
                    json.dumps(["p2", "p3"]),
                    "build-1",
                ),
                (
                    "all",
                    "edge-weak",
                    "co_occurrence",
                    "g_mapreduce",
                    "g_optimizer",
                    None,
                    None,
                    0,
                    1,
                    1,
                    json.dumps(["p3"]),
                    "build-1",
                ),
            ],
        )


def _insert_expand_fixture(storage: SQLiteStorage) -> None:
    """Create separate DBLP/ASTRO scopes for the append-only expansion contract."""

    with storage._connect() as connection:
        connection.execute(
            """
            CREATE TABLE cn_paperdetails (
                paperid TEXT PRIMARY KEY,
                title TEXT,
                year INTEGER
            )
            """
        )
        paper_rows = [
            ("DBLP-KG-ICDE-1000", "Incremental Semantic Graphs", 2020),
            ("DBLP-KG-ICDE-1001", "Distributed SQL Processing", 2021),
            ("DBLP-KG-ICDE-1002", "Query Planning at Scale", 2022),
            ("ASTRO-KG-2000", "Astro Semantic Graphs", 2020),
            ("ASTRO-KG-2001", "Stellar Query Methods", 2021),
        ]
        connection.executemany(
            "INSERT INTO cn_paperdetails (paperid, title, year) VALUES (?, ?, ?)",
            paper_rows,
        )
        connection.executemany(
            """
            INSERT INTO paper_kg_sources (
                paperid, corpus_id, dataset, venue, source_path,
                source_checksum, raw_record
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    paper_id,
                    f"corpus-{paper_id}",
                    "KG-ICDE" if paper_id.startswith("DBLP") else "ASTRO-KG",
                    "ICDE" if paper_id.startswith("DBLP") else "arXiv astro-ph",
                    "fixture",
                    f"checksum-{paper_id}",
                    "{}",
                )
                for paper_id, _title, _year in paper_rows
            ],
        )
        for scope in ("dblp", "astro"):
            connection.execute(
                """
                INSERT INTO global_semantic_graph_builds (
                    build_id, dataset_scope, normalization_version,
                    min_paper_count, status, parameters, stats, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"build-{scope}",
                    scope,
                    "v1",
                    2,
                    "completed",
                    "{}",
                    "{}",
                    "2026-08-23T12:00:00+00:00",
                ),
            )

        entities = [
            (
                "dblp",
                "g_d_sql",
                "SQL",
                "sql",
                "QueryLanguage",
                "querylanguage",
                3,
                5,
                ["KG-ICDE"],
                ["DBLP-KG-ICDE-1000", "DBLP-KG-ICDE-1001", "DBLP-KG-ICDE-1002"],
            ),
            (
                "dblp",
                "g_d_map",
                "MapReduce",
                "mapreduce",
                "System",
                "system",
                3,
                3,
                ["KG-ICDE"],
                ["DBLP-KG-ICDE-1000", "DBLP-KG-ICDE-1001", "DBLP-KG-ICDE-1002"],
            ),
            (
                "dblp",
                "g_d_opt",
                "Query Optimizer",
                "query optimizer",
                "Method",
                "method",
                2,
                2,
                ["KG-ICDE"],
                ["DBLP-KG-ICDE-1001", "DBLP-KG-ICDE-1002"],
            ),
            (
                "astro",
                "g_a_sql",
                "SQL",
                "sql",
                "QueryLanguage",
                "querylanguage",
                2,
                2,
                ["ASTRO-KG"],
                ["ASTRO-KG-2000", "ASTRO-KG-2001"],
            ),
            (
                "astro",
                "g_a_star",
                "Star Survey",
                "star survey",
                "Topic",
                "topic",
                2,
                2,
                ["ASTRO-KG"],
                ["ASTRO-KG-2000", "ASTRO-KG-2001"],
            ),
        ]
        for (
            scope,
            entity_id,
            name,
            normalized_name,
            entity_type,
            normalized_type,
            paper_count,
            mention_count,
            datasets,
            examples,
        ) in entities:
            connection.execute(
                """
                INSERT INTO global_semantic_entities (
                    dataset_scope, global_entity_id, name_group_id,
                    normalization_version, canonical_name, normalized_name,
                    canonical_entity_type, normalized_entity_type,
                    type_distribution, build_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    entity_id,
                    f"ng-{entity_id}",
                    "v1",
                    name,
                    normalized_name,
                    entity_type,
                    normalized_type,
                    json.dumps({entity_type: paper_count}),
                    f"build-{scope}",
                ),
            )
            connection.execute(
                """
                INSERT INTO global_semantic_entity_stats (
                    dataset_scope, global_entity_id, paper_count, mention_count,
                    datasets, example_papers, eligible, build_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    entity_id,
                    paper_count,
                    mention_count,
                    json.dumps(datasets),
                    json.dumps(examples),
                    1,
                    f"build-{scope}",
                ),
            )

        mentions = [
            ("dblp", "DBLP-KG-ICDE-1000", "ent002", "g_d_sql", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1001", "ent-sql-1", "g_d_sql", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1002", "ent-sql-2", "g_d_sql", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1000", "ent-map-0", "g_d_map", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1001", "ent-map-1", "g_d_map", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1002", "ent-map-2", "g_d_map", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1001", "ent-opt-1", "g_d_opt", "KG-ICDE"),
            ("dblp", "DBLP-KG-ICDE-1002", "ent-opt-2", "g_d_opt", "KG-ICDE"),
            ("astro", "ASTRO-KG-2000", "ent002", "g_a_sql", "ASTRO-KG"),
            ("astro", "ASTRO-KG-2001", "ent-sql-1", "g_a_sql", "ASTRO-KG"),
            ("astro", "ASTRO-KG-2000", "ent-star-0", "g_a_star", "ASTRO-KG"),
            ("astro", "ASTRO-KG-2001", "ent-star-1", "g_a_star", "ASTRO-KG"),
        ]
        connection.executemany(
            """
            INSERT INTO global_semantic_entity_mentions (
                dataset_scope, paperid, entity_id, global_entity_id,
                dataset, dataset_group, original_name, original_entity_type, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scope,
                    paper_id,
                    local_id,
                    global_id,
                    dataset_name,
                    scope,
                    global_id,
                    "QueryLanguage",
                    f"build-{scope}",
                )
                for scope, paper_id, local_id, global_id, dataset_name in mentions
            ],
        )

        edges = [
            (
                "dblp",
                "edge-d-co",
                "co_occurrence",
                "g_d_sql",
                "g_d_map",
                None,
                None,
                0,
                3,
                3,
                ["DBLP-KG-ICDE-1000", "DBLP-KG-ICDE-1001", "DBLP-KG-ICDE-1002"],
            ),
            (
                "dblp",
                "edge-d-sem",
                "semantic_relation",
                "g_d_sql",
                "g_d_opt",
                "optimizesFor",
                "optimizesfor",
                1,
                2,
                2,
                ["DBLP-KG-ICDE-1001", "DBLP-KG-ICDE-1002"],
            ),
            (
                "astro",
                "edge-a-co",
                "co_occurrence",
                "g_a_sql",
                "g_a_star",
                None,
                None,
                0,
                2,
                2,
                ["ASTRO-KG-2000", "ASTRO-KG-2001"],
            ),
        ]
        connection.executemany(
            """
            INSERT INTO global_semantic_edges (
                dataset_scope, edge_id, edge_type,
                source_global_entity_id, target_global_entity_id,
                relation_name, normalized_relation_name, directed,
                paper_count, mention_count, evidence_papers, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scope,
                    edge_id,
                    edge_type,
                    source_id,
                    target_id,
                    relation_name,
                    normalized_relation_name,
                    directed,
                    paper_count,
                    mention_count,
                    json.dumps(evidence),
                    f"build-{scope}",
                )
                for (
                    scope,
                    edge_id,
                    edge_type,
                    source_id,
                    target_id,
                    relation_name,
                    normalized_relation_name,
                    directed,
                    paper_count,
                    mention_count,
                    evidence,
                ) in edges
            ],
        )


def test_normalize_entity_name_handles_case_spaces_and_separators() -> None:
    assert normalize_entity_name("  Hot--Jupiter__Survey ") == "hot jupiter survey"
    assert normalize_entity_name("Exoplanets") != normalize_entity_name("Exoplanet")


def test_overview_and_entity_search(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_fixture(storage)

    overview = client.get(
        "/api/semantic-graphs/global/overview?dataset=all&minPaperCount=2",
        headers=headers,
    )
    assert overview.status_code == 200
    assert overview.json()["eligibleEntityCount"] == 3
    assert overview.json()["coOccurrenceEdgeCount"] == 2
    assert overview.json()["semanticRelationEdgeCount"] == 1
    assert overview.json()["buildId"] == "build-1"

    search = client.get(
        "/api/semantic-graphs/global/entities",
        headers=headers,
        params={
            "query": "sql",
            "dataset": "all",
            "minPaperCount": 2,
            "entityTypes": "QueryLanguage",
        },
    )
    assert search.status_code == 200
    assert [item["globalEntityId"] for item in search.json()["items"]] == ["g_sql"]
    item = search.json()["items"][0]
    assert item["paperCount"] == 5
    assert item["examplePapers"] == ["DBLP-KG-ICDE-1093", "ASTRO-KG-00001"]
    assert item["examplePaperDetails"] == [
        {
            "paperId": "DBLP-KG-ICDE-1093",
            "title": "Scalable SQL Processing",
            "year": 2018,
        },
        {
            "paperId": "ASTRO-KG-00001",
            "title": "Mapping the Early Universe",
            "year": 2020,
        },
    ]


def test_query_supports_global_local_and_text_seeds(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_fixture(storage)

    for seed in (
        {"seedGlobalEntityId": "g_sql"},
        {"seedPaperId": "p1", "seedEntityId": "ent-sql"},
        {"query": "SQL"},
    ):
        response = client.post(
            "/api/semantic-graphs/global/query",
            headers=headers,
            json={
                **seed,
                "dataset": "all",
                "minPaperCount": 2,
                "edgeMinWeight": 2,
                "edgeTypes": ["co_occurrence", "semantic_relation"],
                "depth": 1,
                "limit": 20,
            },
        )
        assert response.status_code == 200
        graph = response.json()
        assert graph["seedNodeId"] == "g_sql"
        assert {node["nodeType"] for node in graph["nodes"]} == {"global_semantic_entity"}
        assert {edge["id"] for edge in graph["edges"]} == {"edge-co", "edge-semantic"}
        assert graph["stats"]["edgeMinWeight"] == 2
        assert graph["stats"]["returnedNodeCount"] == 3
        co_occurrence = next(edge for edge in graph["edges"] if edge["id"] == "edge-co")
        assert co_occurrence["data"]["paperCount"] == 3
        assert co_occurrence["data"]["evidencePapers"] == ["p1", "p2", "p3"]
        assert co_occurrence["data"]["evidencePaperDetails"] == [
            {"paperId": "p1", "title": "Distributed Data Processing", "year": 2019},
            {"paperId": "p2", "title": "Semantic Query Optimization", "year": 2021},
            {"paperId": "p3", "title": None, "year": None},
        ]
        semantic_relation = next(
            edge for edge in graph["edges"] if edge["id"] == "edge-semantic"
        )
        assert semantic_relation["data"]["paperCount"] == 2
        assert semantic_relation["data"]["evidencePapers"] == ["p2", "p3"]
        assert semantic_relation["data"]["evidencePaperDetails"] == [
            {"paperId": "p2", "title": "Semantic Query Optimization", "year": 2021},
            {"paperId": "p3", "title": None, "year": None},
        ]
        mapreduce = next(node for node in graph["nodes"] if node["id"] == "g_mapreduce")
        assert mapreduce["data"]["examplePapers"] == ["p1", "p3"]
        assert mapreduce["data"]["examplePaperDetails"] == [
            {"paperId": "p1", "title": "Distributed Data Processing", "year": 2019},
            {"paperId": "p3", "title": None, "year": None},
        ]


def test_expand_returns_appendable_entity_and_paper_nodes(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_expand_fixture(storage)

    response = client.post(
        "/api/semantic-graphs/global/expand",
        headers=headers,
        json={
            "seedPaperId": "DBLP-KG-ICDE-1000",
            "seedEntityId": "ent002",
            "dataset": "dblp",
            "minPaperCount": 2,
            "edgeMinWeight": 2,
            "edgeTypes": ["co_occurrence", "semantic_relation"],
            "depth": 1,
            "limit": 20,
            "evidenceLimit": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["seed"] == {
        "paperId": "DBLP-KG-ICDE-1000",
        "localEntityId": "ent002",
        "globalEntityId": "g_d_sql",
        "canonicalName": "SQL",
        "entityType": "QueryLanguage",
    }
    global_nodes = [
        node for node in payload["nodes"] if node["nodeType"] == "global_semantic_entity"
    ]
    paper_nodes = [node for node in payload["nodes"] if node["nodeType"] == "paper"]
    assert {node["id"] for node in global_nodes} == {"g_d_sql", "g_d_map", "g_d_opt"}
    assert {node["id"] for node in paper_nodes} == {
        "paper_DBLP-KG-ICDE-1000",
        "paper_DBLP-KG-ICDE-1001",
        "paper_DBLP-KG-ICDE-1002",
    }
    seed_node = next(node for node in global_nodes if node["id"] == "g_d_sql")
    assert seed_node["data"]["canonicalName"] == "SQL"
    assert seed_node["data"]["datasetScope"] == "dblp"
    assert seed_node["data"]["paperCount"] == 3
    assert seed_node["data"]["mentionCount"] == 5
    assert next(node for node in paper_nodes if node["id"] == "paper_DBLP-KG-ICDE-1000")[
        "data"
    ]["venue"] == "ICDE"

    local_edge = next(edge for edge in payload["edges"] if edge["edgeType"] == "local_to_global")
    assert local_edge["source"] == "ent002"
    assert local_edge["target"] == "g_d_sql"

    mention_edges = [
        edge
        for edge in payload["edges"]
        if edge["edgeType"] == "entity_mentioned_in_paper"
    ]
    assert any(
        edge["source"] == "g_d_sql"
        and edge["target"] == "paper_DBLP-KG-ICDE-1000"
        for edge in mention_edges
    )
    global_edges = [
        edge
        for edge in payload["edges"]
        if edge["edgeType"] in {"co_occurrence", "semantic_relation"}
    ]
    assert {edge["id"] for edge in global_edges} == {"edge-d-co", "edge-d-sem"}
    assert all(len(edge["data"]["evidencePapers"]) <= 2 for edge in global_edges)
    assert payload["stats"]["paperNodeCount"] == 3
    assert payload["stats"]["globalEntityNodeCount"] == 3
    assert payload["stats"]["nodeCount"] == 6


def test_expand_isolates_dataset_and_rejects_all_scope(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_expand_fixture(storage)

    astro = client.post(
        "/api/semantic-graphs/global/expand",
        headers=headers,
        json={
            "seedPaperId": "ASTRO-KG-2000",
            "seedEntityId": "ent002",
            "dataset": "astro",
            "evidenceLimit": 1,
        },
    )
    assert astro.status_code == 200
    astro_payload = astro.json()
    assert {
        node["id"]
        for node in astro_payload["nodes"]
        if node["nodeType"] == "global_semantic_entity"
    } == {"g_a_sql", "g_a_star"}
    assert all(
        not node["id"].startswith("paper_DBLP")
        for node in astro_payload["nodes"]
        if node["nodeType"] == "paper"
    )
    assert astro_payload["stats"]["dataset"] == "astro"
    assert all(
        len(edge["data"]["evidencePapers"]) <= 1
        for edge in astro_payload["edges"]
        if edge["edgeType"] in {"co_occurrence", "semantic_relation"}
    )

    all_scope = client.post(
        "/api/semantic-graphs/global/expand",
        headers=headers,
        json={
            "seedPaperId": "DBLP-KG-ICDE-1000",
            "seedEntityId": "ent002",
            "dataset": "all",
        },
    )
    assert all_scope.status_code == 422


def test_expand_limit_and_seed_node_id_resolution_are_effective(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_expand_fixture(storage)

    limited = client.post(
        "/api/semantic-graphs/global/expand",
        headers=headers,
        json={
            "seedPaperId": "DBLP-KG-ICDE-1000",
            "seedEntityId": "DBLP-KG-ICDE-1000:semantic_entity:ent002",
            "dataset": "dblp",
            "limit": 1,
            "evidenceLimit": 1,
        },
    )
    assert limited.status_code == 200
    payload = limited.json()
    assert [
        node["id"]
        for node in payload["nodes"]
        if node["nodeType"] == "global_semantic_entity"
    ] == ["g_d_sql"]
    assert payload["stats"]["globalEntityNodeCount"] == 1
    assert payload["stats"]["paperNodeCount"] == 1
    assert payload["stats"]["truncated"] is True
    local_edge = next(edge for edge in payload["edges"] if edge["edgeType"] == "local_to_global")
    assert local_edge["source"] == "DBLP-KG-ICDE-1000:semantic_entity:ent002"
    assert local_edge["data"]["localEntityId"] == "ent002"


def test_query_filters_edge_type_and_validates_bounds(tmp_path) -> None:
    client, storage, headers = _client(tmp_path)
    _insert_fixture(storage)

    response = client.post(
        "/api/semantic-graphs/global/query",
        headers=headers,
        json={
            "seedGlobalEntityId": "g_sql",
            "dataset": "all",
            "edgeTypes": ["semantic_relation"],
            "edgeMinWeight": 2,
            "depth": 2,
            "limit": 2,
        },
    )
    assert response.status_code == 200
    assert [edge["id"] for edge in response.json()["edges"]] == ["edge-semantic"]
    assert response.json()["stats"]["truncated"] is False

    type_filtered = client.post(
        "/api/semantic-graphs/global/query",
        headers=headers,
        json={
            "seedGlobalEntityId": "g_sql",
            "entityTypes": ["QueryLanguage"],
            "limit": 200,
        },
    )
    assert type_filtered.status_code == 200
    assert [node["id"] for node in type_filtered.json()["nodes"]] == ["g_sql"]
    assert type_filtered.json()["edges"] == []

    too_deep = client.post(
        "/api/semantic-graphs/global/query",
        headers=headers,
        json={"seedGlobalEntityId": "g_sql", "depth": 3},
    )
    assert too_deep.status_code == 422

    too_many_nodes = client.post(
        "/api/semantic-graphs/global/query",
        headers=headers,
        json={"seedGlobalEntityId": "g_sql", "limit": 201},
    )
    assert too_many_nodes.status_code == 422

    incomplete_local_seed = client.post(
        "/api/semantic-graphs/global/query",
        headers=headers,
        json={"seedPaperId": "p1"},
    )
    assert incomplete_local_seed.status_code == 400
