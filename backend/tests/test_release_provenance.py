import json

from app.agent_experiments import _normalize_selected_observation_ids, _build_selected_report_observations
from app.evidence_provenance import ground_bundle
from app.evidence_graph import EvidenceGraphBuilder
from app.llm.deepseek import DeepSeekClient


def observed(oid, data, tool="execute_sciscinet_sql", **extra):
    return {"observationId": oid, "status": "success", "toolName": tool,
            "arguments": {"sql": "SELECT ..."}, "execution": {"queryText": "SELECT ...", "backend": "mmdb"},
            "result": {"status": "success", "data": data, "row_count": len(data)}, **extra}


def test_failed_empty_and_schema_observations_are_not_report_inputs():
    observations = [observed(0, [{"paperid": "P1", "title": "Verified"}]),
                    observed(1, [{"paperid": "BAD"}], status="failed"), observed(2, []),
                    observed(3, [{"table_name": "papers"}], tool="get_database_schema")]
    selected, invalid = _normalize_selected_observation_ids({"observationIds": [0, 1, 2, 3, 99]}, observations)
    assert selected == [0]
    assert invalid == [1, 2, 3, 99]
    downstream = _build_selected_report_observations(observations, [], [0, 1, 2, 3])
    assert len(downstream) == 1
    assert downstream[0]["execution"]["queryText"] == "SELECT ..."


def test_evidence_rejects_model_invented_nodes_relations_and_values():
    selected = [observed(0, [{"paperid": "P1", "title": "Actual", "abstract": "Observed text"}])]
    proposal = {"evidence": [{"evidenceId": "obs-0-row-0", "content": {"paperid": "FAKE"}, "supportingClaims": ["Interpretation"]}],
                "graphIntent": {"nodes": [{"id": "FAKE", "nodeType": "paper", "sourceObservationIds": [0]}],
                                "edges": [{"source": "P1", "target": "FAKE", "type": "cites", "sourceObservationIds": [0]}]}}
    bundle = ground_bundle(selected, proposal)
    assert bundle["evidence"][0]["content"]["title"] == "Actual"
    assert "FAKE" not in json.dumps(bundle)
    assert bundle["evidence"][0]["sources"][0]["query"] == "SELECT ..."
    assert not ground_bundle([], proposal)["shouldRenderGraph"]


def test_graph_preserves_four_modalities_without_new_queries():
    selected = [observed(0, [
        {"paperid": "P1", "title": "First", "year": 2020, "abstract": "Actual abstract", "authorid": "A1", "author_name": "Author", "institutionid": "I1", "institution_name": "Institute"},
        {"paperid": "P2", "title": "Second", "year": 2021},
        {"citing_paperid": "P2", "cited_paperid": "P1"}]),
        observed(1, [{"paperid": "P2", "title": "Second", "score": .82, "rank": 1}],
                 tool="find_similar_papers_by_paperid", arguments={"paper_id": "P1"}),
        observed(2, [{"year": 2021, "paper_count": 1}])]

    class UnusableDatabase:
        def __getattr__(self, name):
            raise AssertionError("Generation attempted database access")

    result = EvidenceGraphBuilder(UnusableDatabase()).build(run_id="r", workspace_id="w", evidence_bundle={}, selected_observations=selected)
    graph = result.graph
    assert graph is not None
    assert {edge["type"] for edge in graph.edges} >= {"cites", "similar", "authored_by", "affiliated_with"}
    assert {n.id for n in graph.nodes} >= {"P1", "P2", "A1", "I1"}
    bundle = ground_bundle(selected)
    assert {e["evidenceType"] for e in bundle["evidence"]} >= {"document", "semantic", "statistic", "relation"}
    assert next(e for e in graph.edges if e["type"] == "similar")["data"]["score"] == .82


def test_query_retrieval_is_not_paper_similarity_and_venue_is_not_affiliation():
    bundle = ground_bundle([observed(4, [{"paperid": "P1", "title": "One", "score": .9, "authors": ["Alice"], "venue": "Research University"},
                                        {"paperid": "P2", "title": "Two", "score": .8}], tool="search_papers_by_natural_language")])
    assert not any(e["type"] == "similar" for e in bundle["graphIntent"]["edges"])
    assert not any(n["nodeType"] == "institution" for n in bundle["graphIntent"]["nodes"])


def test_author_and_institution_aggregates_preserve_entity_attributes():
    bundle = ground_bundle([observed(0, [{"authorid": "A1", "author_name": "Author", "paper_count": 7},
                                        {"institutionid": "I1", "institution_name": "Institute", "paper_count": 12}])])
    nodes = {n["id"]: n for n in bundle["graphIntent"]["nodes"]}
    assert nodes["A1"]["nodeType"] == "author"
    assert nodes["I1"]["observedAttributes"]["paper_count"] == 12


def test_generic_chat_provider_does_not_receive_vendor_only_parameters(monkeypatch):
    from tests.test_deepseek_client import _FakeHttpClient
    captured = []
    monkeypatch.delenv("LLM_SEND_THINKING", raising=False)
    monkeypatch.delenv("LLM_SEND_REASONING_EFFORT", raising=False)
    monkeypatch.setattr("app.llm.deepseek.httpx.Client", lambda **kwargs: _FakeHttpClient(captured, **kwargs))
    client = DeepSeekClient(api_key="test", base_url="https://llm.example.test/v1", model="test")
    assert client.complete([{"role": "user", "content": "hello"}], thinking=True).used_llm
    assert "thinking" not in captured[0] and "reasoning_effort" not in captured[0]
