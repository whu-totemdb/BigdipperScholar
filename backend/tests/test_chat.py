from app.agent import ReactScholarAgent, ScholarAgent
from app.llm import LLMResult
from app.models import ToolSpec
from app.prompts import PromptProvider
from app.storage import SQLiteStorage
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider
from app.tools.base import ToolProvider
from tests.conftest import DisabledLLM


def test_agent_mock_search_fallback() -> None:
    agent = ScholarAgent(
        HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
        llm_client=DisabledLLM(),
    )

    response = agent.run("conversation", "search papers about graph RAG", [])

    assert "I found" in response.message
    assert response.tool_calls[0].name == "search_papers"
    assert response.frontend_payload is not None
    assert response.frontend_payload.papers


def test_agent_mock_graph_fallback() -> None:
    agent = ScholarAgent(
        HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
        llm_client=DisabledLLM(),
    )

    response = agent.run("conversation", "show graph for p-data-agents-2024", [])

    assert response.tool_calls[0].name == "get_mock_graph"
    assert response.frontend_payload is not None
    assert response.frontend_payload.seed_paper_id == "p-data-agents-2024"


def test_database_tool_result_can_be_answered_by_llm() -> None:
    agent = ScholarAgent(FakeDatabaseToolProvider(), llm_client=FakeLLMClient())

    response = agent.run("conversation", "有哪些学科领域可以查询？", [])

    assert response.message == "LLM grounded answer"
    assert response.tool_calls[0].name == "get_all_fields"
    assert response.frontend_payload is None


def test_react_agent_executes_llm_tool_call_and_returns_final_answer() -> None:
    agent = ReactScholarAgent(
        FakeDatabaseToolProvider(),
        llm_client=FakeToolCallingLLMClient(),
        prompt_provider=PromptProvider(reload_prompts=False),
    )

    response = agent.run("conversation", "有哪些学科领域可以查询？", [])

    assert response.message == "可以查询 Machine learning。"
    assert response.tool_calls[0].name == "get_all_fields"
    assert response.tool_calls[0].arguments == {"limit": 1}
    assert response.frontend_payload is None


def test_storage_persists_messages_and_tool_calls(tmp_path) -> None:
    storage = SQLiteStorage(tmp_path / "storage.db")
    conversation_id = storage.ensure_conversation()
    storage.add_message(conversation_id, "user", "hello")
    storage.add_message(conversation_id, "assistant", "hi")

    conversation = storage.get_conversation(conversation_id)

    assert conversation is not None
    assert conversation.conversation_id == conversation_id
    assert [message.role for message in conversation.messages] == ["user", "assistant"]
    assert storage.get_conversation("missing") is None


class FakeDatabaseToolProvider(ToolProvider):
    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_all_fields",
                description="fake fields",
                parameters={"type": "object", "properties": {}},
            )
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        assert name == "get_all_fields"
        return {
            "status": "success",
            "tool": name,
            "rowCount": 1,
            "data": [
                {
                    "display_name": "Machine learning",
                    "level": 1,
                    "works_count": 100,
                    "cited_by_count": 500,
                }
            ],
            "summary": "get_all_fields returned 1 rows",
        }


class FakeLLMClient:
    is_configured = True

    def complete(self, messages: list[dict[str, str]]) -> LLMResult:
        assert "Tool results" in messages[-1]["content"]
        assert "get_all_fields" in messages[-1]["content"]
        return LLMResult(content="LLM grounded answer", used_llm=True)


class FakeToolCallingLLMClient:
    is_configured = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=None, tool_choice=None) -> LLMResult:
        self.calls += 1
        if self.calls == 1:
            assert tools
            assert tool_choice == "auto"
            return LLMResult(
                content="",
                used_llm=True,
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_all_fields",
                                "arguments": '{"limit": 1}',
                            },
                        }
                    ],
                },
            )
        assert messages[-1]["role"] == "tool"
        assert "Machine learning" in messages[-1]["content"]
        return LLMResult(
            content="可以查询 Machine learning。",
            used_llm=True,
            message={"role": "assistant", "content": "可以查询 Machine learning。"},
        )
