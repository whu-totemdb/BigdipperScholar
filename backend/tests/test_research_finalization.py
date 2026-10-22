import json

import pytest
from langchain_core.tools import StructuredTool

from app.agent_experiments import AgentExperimentService, _build_selected_report_observations, _generation_observations
from app.evidence_provenance import ground_bundle
from app.llm import LLMResult
from app.models import AgentExperimentRequest
from app.research_observations import ObservationSelection, record_analyses


def observed(oid, count):
    return {"observationId": oid, "round": 1, "toolName": "query_data", "status": "success",
            "result": {"data": [{"paper_count": count}]}, "arguments": {}}


def call(name, **arguments):
    return {"id": name, "name": name, "arguments": arguments}


def result(*calls, content="Planning the next query"):
    return LLMResult(content=content, used_llm=True, message={"role": "assistant", "content": content,
        "tool_calls": [{"id": f"{i}-{c['id']}", "type": "function", "function": {
            "name": c["name"], "arguments": json.dumps(c["arguments"])}} for i,c in enumerate(calls)]})


class ScriptedLLM:
    is_configured = True
    model = "fixture"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.payloads = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if tools:
            if not self.steps:
                raise AssertionError("Unexpected extra model call")
            return self.steps.pop(0)
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        return LLMResult(content='{"evidence": []}' if 'observedEvidence' in payload else 'Report from selected data', used_llm=True)


def run(tmp_path, steps, rounds=8):
    def query_data(count: int) -> dict:
        """Return fixture data for control-flow verification."""
        return {"status": "success", "data": [{"paper_count": count}]}
    llm = ScriptedLLM(steps)
    service = AgentExperimentService(llm_client=llm,
        tool_loader=lambda _: [StructuredTool.from_function(query_data)], log_path=tmp_path/'events.jsonl')
    return service.run(AgentExperimentRequest(query="Count distinct papers", maxRounds=rounds, includeTrace=True)), llm


def test_selection_supports_retraction_empty_final_and_sealing():
    selection = ObservationSelection()
    selection.update([1], "append", "preliminary")
    selection.update([2], "replace", "corrected")
    assert selection.ids == [2]
    selection.update([2], "remove", "out of scope")
    selection.update([], "finalize", "no evidence")
    assert selection.ids == [] and selection.finalized
    with pytest.raises(ValueError):
        selection.update([1], "append", "late")


def test_finalization_replaces_old_result_and_saves_a_model_round(tmp_path):
    response, llm = run(tmp_path, [
        result(call("query_data", count=30)),
        result(call("select_report_observations", observationIds=[0], mode="append"), call("query_data", count=12)),
        result(call("select_report_observations", observationIds=[1], mode="finalize", rationale="Replace author-row count with distinct papers",
                    analyses=[{"observationIds":[1], "summary":"12 distinct papers"}])),
    ])
    assert response.status == "completed"
    assert [o['observationId'] for o in response.selected_observations] == [1]
    assert response.metrics['selectionRepairCalls'] == 0
    assert llm.calls == 5  # 3 research + report + evidence, no redundant stop round.
    assert llm.payloads[0]['selectedExplorations'] == llm.payloads[1]['selectedExplorations']
    assert response.selected_observations[0]['resultAnalyses'][0]['summary'] == '12 distinct papers'
    assert 'llmResponse' not in response.selected_observations[0]


def test_query_in_finalization_response_is_executed_but_not_prematurely_finalized(tmp_path):
    response, llm = run(tmp_path, [
        result(call('query_data',count=30)),
        result(call('select_report_observations',observationIds=[0],mode='finalize'),call('query_data',count=12)),
        result(call('select_report_observations',observationIds=[1],mode='finalize')),
    ])
    assert response.status == 'completed'
    assert [o['observationId'] for o in response.selected_observations] == [1]


def test_empty_final_does_not_restore_draft(tmp_path):
    response, llm = run(tmp_path, [result(call('query_data',count=30)),
        result(call('select_report_observations',observationIds=[0],mode='append')),
        result(call('select_report_observations',observationIds=[],mode='finalize'))])
    assert response.status == 'completed' and response.selected_observations == []
    assert not response.evidence_bundle['shouldRenderGraph']
    assert llm.calls == 5


@pytest.mark.parametrize('rounds,stop', [(1,False),(8,True)])
def test_missing_finalization_gets_exactly_one_review(tmp_path,rounds,stop):
    steps=[result(call('query_data',count=12))]
    if stop:
        steps.append(result(content='Done without selecting'))
    steps.append(result(call('select_report_observations',observationIds=[0],mode='finalize',
                             analyses=[{'observationIds':[0],'summary':'12 papers'}])))
    response,llm=run(tmp_path,steps,rounds)
    assert response.status == 'completed'
    assert response.metrics['selectionRepairCalls'] == 1


def test_failed_review_does_not_generate_from_draft(tmp_path):
    response,llm=run(tmp_path,[result(call('query_data',count=30)),
        result(call('select_report_observations',observationIds=[0],mode='append')),
        result(content='No final decision')],rounds=2)
    assert response.status == 'failed'
    assert not llm.payloads and llm.calls == 3


def test_invalid_final_id_requires_correction(tmp_path):
    response,llm=run(tmp_path,[result(call('query_data',count=12)),
        result(call('select_report_observations',observationIds=[0,999],mode='finalize')),
        result(call('select_report_observations',observationIds=[0],mode='finalize'))])
    assert response.status=='completed' and response.metrics['rounds']==3


def test_post_result_analysis_can_accompany_next_queries(tmp_path):
    response,llm=run(tmp_path,[result(call('query_data',count=30)),
        result(call('record_observation_analysis',analyses=[{'observationIds':[0],'summary':'Initial count, needs deduplication'}]),call('query_data',count=12)),
        result(call('select_report_observations',observationIds=[0,1],mode='finalize',
                    analyses=[{'observationIds':[0,1],'summary':'30 author rows correspond to 12 distinct papers'}]))])
    assert response.status == 'completed'
    assert len(response.selected_observations[0]['resultAnalyses']) == 2
    assert response.selected_observations[1]['resultAnalyses'][0]['sourceObservationIds']==[0,1]


def test_analysis_is_atomic_and_does_not_reference_future_results():
    observations=[observed(0,12)]
    with pytest.raises(ValueError):
        record_analyses([{'observationIds':[0],'summary':'valid'}, {'observationIds':[1],'summary':'not yet returned'}],observations,2)
    assert 'resultAnalyses' not in observations[0]


def test_analysis_from_excluded_observation_cannot_leak_downstream():
    observations=[observed(0,30), observed(1,12)]
    record_analyses([{'observationIds':[0,1],'summary':'Joint comparison'}, {'observationIds':[1],'summary':'Corrected count 12'}],observations,2)
    selected=_build_selected_report_observations(observations,[{'round':1,'content':'Before query'}],[1])
    assert selected[0]['resultAnalyses']==[{'sourceObservationIds':[1],'summary':'Corrected count 12','recordedAtRound':2}]
    assert 'Before query' not in json.dumps(selected)
    assert len(observations[1]['resultAnalyses'])==2  # full history retained
    assert ground_bundle(selected)['evidence'][0]['content']['paper_count']==12


def test_joint_analysis_is_sent_once_without_losing_facts_or_persisted_associations():
    observations=[observed(0,30),observed(1,12)]
    record_analyses([{'observationIds':[0,1],'summary':'30 author rows, 12 distinct papers'}],observations,3)
    selected=_build_selected_report_observations(observations,[],[0,1])
    prepared=_generation_observations(selected)
    assert len(prepared[0]['resultAnalyses'])==1 and prepared[1]['resultAnalyses']==[]
    assert prepared[0]['resultAnalyses'][0]['sourceObservationIds']==[0,1]
    assert [o['result'] for o in prepared]==[o['result'] for o in selected]
    assert len(selected[1]['resultAnalyses'])==1
    assert _generation_observations(prepared)==prepared


@pytest.mark.parametrize('ids',[None,[True],[0.5]])
def test_malformed_final_id_list_cannot_silently_finalize_empty_or_coerce_ids(tmp_path,ids):
    response,llm=run(tmp_path,[result(call('query_data',count=12)),
        result(call('select_report_observations',observationIds=ids,mode='finalize')),
        result(call('select_report_observations',observationIds=[0],mode='finalize'))])
    assert response.status=='completed' and response.metrics['rounds']==3
    assert [o['observationId'] for o in response.selected_observations]==[0]
