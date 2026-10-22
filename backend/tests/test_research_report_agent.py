from __future__ import annotations

from pathlib import Path

from app.llm import LLMResult
from app.models import AgentExperimentResponse
from app.research_report_agent import ResearchReportChatAgent


def test_research_report_chat_agent_answers_simple_query_directly(tmp_path: Path) -> None:
    prompt_dir = _prompt_dir(tmp_path)
    agent = ResearchReportChatAgent(
        llm_client=DirectConversationLLM(),
        experiment_service=FailingExperimentService(),
        prompt_dir=prompt_dir,
        prompt_docs_dir=tmp_path,
        log_root=tmp_path / "logs",
    )

    response = agent.run("conversation", "hello", [], run_id="run_direct", include_trace=True)

    assert response.status == "completed"
    assert response.message == "你好，我是 BigDipper Scholar。"
    assert response.tool_calls == []
    assert response.graph_artifact is None
    assert response.trace is not None
    assert (tmp_path / "logs" / "run_direct" / "events.jsonl").exists()


def test_research_report_chat_agent_routes_research_query_to_pipeline(tmp_path: Path) -> None:
    prompt_dir = _prompt_dir(tmp_path)
    service = FakeExperimentService()
    agent = ResearchReportChatAgent(
        llm_client=ResearchConversationLLM(),
        experiment_service=service,
        prompt_dir=prompt_dir,
        prompt_docs_dir=tmp_path,
        log_root=tmp_path / "logs",
    )

    response = agent.run("conversation", "找GNN论文", [], run_id="run_research", include_trace=True)

    assert response.status == "completed"
    assert response.message == "研究报告"
    assert service.received_query is not None
    assert "conversation-agent 转交" in service.received_query
    assert "找GNN论文" in service.received_query
    assert [call.name for call in response.tool_calls] == ["search_papers_by_keywords"]
    assert response.graph_artifact is not None
    assert response.graph_artifact.status == "available"
    assert response.graph_artifact.node_preview_count == 1
    assert response.internal_artifact["evidenceBundle"]["shouldRenderGraph"] is True
    assert response.trace is not None
    assert len(response.research_steps) == 1
    assert response.research_steps[0].public_name == "Search papers by keywords"
    assert (tmp_path / "logs" / "run_research" / "report.md").read_text(encoding="utf-8") == "研究报告"


def _prompt_dir(tmp_path: Path) -> Path:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "conversation_agent.md").write_text("conversation prompt", encoding="utf-8")
    return prompt_dir


class DirectConversationLLM:
    is_configured = True

    def complete(self, messages, tools=None, tool_choice=None, **kwargs):
        assert tools
        assert tool_choice == "auto"
        assert kwargs.get("thinking") is False
        return LLMResult(
            content="你好，我是 BigDipper Scholar。",
            used_llm=True,
            message={"role": "assistant", "content": "你好，我是 BigDipper Scholar。"},
        )


class ResearchConversationLLM:
    is_configured = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        assert kwargs.get("thinking") is False
        if self.calls == 1:
            assert tools
            return LLMResult(
                content="需要研究。",
                used_llm=True,
                message={
                    "role": "assistant",
                    "content": "需要研究。",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "run_research_report",
                                "arguments": '{"researchQuery":"检索 GNN 论文","contextSummary":"","reason":"需要真实数据"}',
                            },
                        }
                    ],
                },
            )
        assert "researchReport" in messages[-1]["content"]
        return LLMResult(
            content="最终整合回复",
            used_llm=True,
            message={"role": "assistant", "content": "最终整合回复"},
        )


class FakeExperimentService:
    def __init__(self) -> None:
        self.received_query: str | None = None

    def run(self, request, run_record=None):
        self.received_query = request.query
        return AgentExperimentResponse(
            runId=request.run_id or "run_research",
            status="completed",
            agentMode="mmdb_scholar",
            report="研究报告",
            evidence=[{"evidenceId": "ev_1", "evidenceType": "paper", "label": "GNN Paper", "paperId": "W1", "title": "GNN Paper", "sourceObservationIds": [0], "confidence": 0.9}],
            citations=[{"paperId": "W1", "title": "GNN Paper", "sourceObservationIds": [0], "confidence": 0.9}],
            evidenceBundle={
                "shouldRenderGraph": True,
                "reason": "fake graphable evidence",
                "evidence": [{"evidenceId": "ev_1", "evidenceType": "paper", "label": "GNN Paper", "paperId": "W1", "title": "GNN Paper", "sourceObservationIds": [0], "confidence": 0.9}],
                "citations": [{"paperId": "W1", "title": "GNN Paper", "sourceObservationIds": [0], "confidence": 0.9}],
                "graphIntent": {"title": "Evidence Graph: GNN", "nodes": [{"refId": "paper_1", "nodeType": "paper", "id": "W1", "label": "GNN Paper", "sourceObservationIds": [0], "confidence": 0.9}], "edges": [], "expansion": {"maxNodes": 80}},
                "missingReportEntities": [],
            },
            selectedObservations=[{"observationId": 0, "round": 1, "toolName": "search_papers_by_keywords", "status": "success", "result": "[]"}],
            metrics={"rounds": 1, "toolCalls": 1},
            researchSteps=[
                {
                    "id": "research_step_0",
                    "round": 1,
                    "publicName": "Search papers by keywords",
                    "status": "success",
                    "parameters": {"query": "graph neural network"},
                    "toolLatencyMs": 12.0,
                    "resultSummary": "Returned 10 rows",
                    "selectedForReport": True,
                }
            ],
            trace=[
                {
                    "round": 1,
                    "event": "llm_response",
                    "content": "调用关键词工具",
                    "toolCalls": [
                        {
                            "id": "tool_1",
                            "name": "search_papers_by_keywords",
                            "arguments": {"query": "graph neural network", "year_from": 2020, "year_to": 2024},
                        }
                    ],
                }
            ],
        )


class FailingExperimentService:
    def run(self, request):
        raise AssertionError("research pipeline should not run for direct conversation")
