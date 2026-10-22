import threading
import time

from app.llm import LLMResult
from app.tools import HelmDBToolProvider, HybridToolProvider, MockToolProvider
from app.models import ChatRequest, ToolSpec
from app.tools.base import ToolProvider
from tests.conftest import DisabledLLM


def test_api_routes_are_registered(monkeypatch) -> None:
    monkeypatch.setenv("BIGDIPPER_TOOL_MODE", "langchain")
    from app.main import create_app

    app = create_app(
        tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()])
    )

    paths = {route.path for route in app.routes}
    assert "/api/health" in paths
    assert "/api/tools" in paths
    assert "/api/chat" in paths
    assert "/api/conversations/{conversation_id}" in paths


def test_hybrid_tool_provider_lists_mock_and_helmdb_tools() -> None:
    provider = HybridToolProvider([HelmDBToolProvider(), MockToolProvider()])

    tool_names = {tool.name for tool in provider.list_tools()}
    assert {
        "get_all_fields",
        "search_institutions",
        "get_institution_field_stats",
        "get_field_top_authors",
        "paper_title_to_id",
        "get_paper_references",
        "search_papers",
        "get_paper_detail",
        "get_related_papers",
        "get_mock_graph",
    }.issubset(tool_names)


def test_chat_v1_old_request_shape_returns_run_status_and_persists(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_TOOL_MODE", "langchain")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "chat.db"))
    from app.main import create_app

    app = create_app(
        tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
        llm_client=DisabledLLM(),
    )
    chat = _route(app, "/api/chat", {"POST"})
    get_conversation = _route(app, "/api/conversations/{conversation_id}", {"GET"})

    response = chat(ChatRequest(message="search papers about graph RAG"))

    assert response.conversation_id
    assert response.run_id
    assert response.status == "completed"
    assert response.tool_calls
    conversation = get_conversation(response.conversation_id)
    assert [message.role for message in conversation.messages] == ["user", "assistant"]


def test_chat_returns_real_resource_usage_with_fake_llm_and_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "resource-usage.db"))
    from app.main import create_app

    app = create_app(
        tool_provider=TimedFieldToolProvider(),
        llm_client=FakeResourceLLM(),
    )
    chat = _route(app, "/api/chat", {"POST"})

    response = chat(
        ChatRequest(
            runId="run_resource_usage",
            message="有哪些学科领域可以查询？",
        )
    )

    usage = response.resource_usage
    assert usage is not None
    assert usage.request_id == response.run_id == "run_resource_usage"
    assert usage.conversation_id == response.conversation_id
    assert usage.status == "completed"
    assert usage.latency_ms > 0
    assert usage.summary.llm_calls == 1
    assert usage.summary.tool_calls == 1
    assert usage.should_display is True
    assert usage.hidden_reason is None
    assert usage.summary.sqlite_queries > 0
    assert usage.summary.db_queries == usage.summary.sqlite_queries
    assert usage.summary.prompt_tokens == 11
    assert usage.summary.completion_tokens == 7
    assert usage.summary.total_tokens == 18
    assert usage.summary.memory_mb is None or usage.summary.memory_mb > 0
    assert usage.summary.peak_memory_mb is None
    assert [step.name for step in usage.trace if step.type == "tool"] == ["get_all_fields"]
    assert all(step.backend != "sqlite" for step in usage.trace)
    assert all(step.latency_ms >= 0 for step in usage.trace)

    payload = response.model_dump(by_alias=True)
    assert payload["resourceUsage"]["requestId"] == "run_resource_usage"
    assert payload["resourceUsage"]["shouldDisplay"] is True
    assert payload["resourceUsage"]["summary"]["toolCalls"] == 1


def test_chat_hides_resource_usage_for_llm_and_sqlite_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "resource-usage-chat.db"))
    from app.main import create_app

    app = create_app(
        tool_provider=TimedFieldToolProvider(),
        llm_client=FakeResourceLLM(),
    )
    chat = _route(app, "/api/chat", {"POST"})

    response = chat(ChatRequest(runId="run_resource_usage_chat", message="你好"))

    usage = response.resource_usage
    assert usage is not None
    assert usage.should_display is False
    assert usage.hidden_reason == "no_external_resource_used"
    assert usage.summary.llm_calls == 1
    assert usage.summary.tool_calls == 0
    assert usage.summary.sqlite_queries > 0
    assert usage.summary.graph_queries == 0
    assert [step.type for step in usage.trace] == ["llm"]
    assert all(step.backend != "sqlite" for step in usage.trace)


def test_cancel_unknown_run_returns_404(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_TOOL_MODE", "langchain")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "chat.db"))
    from app.main import create_app

    app = create_app(llm_client=DisabledLLM())
    cancel = _route(app, "/api/chat/runs/{run_id}/cancel", {"POST"})

    try:
        cancel("missing-run")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("Expected HTTPException 404")


def test_cancel_completed_run_returns_completed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_TOOL_MODE", "langchain")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "chat.db"))
    from app.main import create_app

    app = create_app(
        tool_provider=HybridToolProvider([HelmDBToolProvider(), MockToolProvider()]),
        llm_client=DisabledLLM(),
    )
    run_id = "test-run-completed"
    chat = _route(app, "/api/chat", {"POST"})
    cancel = _route(app, "/api/chat/runs/{run_id}/cancel", {"POST"})

    chat_response = chat(
        ChatRequest(runId=run_id, message="search papers about graph RAG"),
    )
    cancel_response = cancel(run_id)

    assert chat_response.status == "completed"
    assert cancel_response.status == "completed"


def test_cancelled_run_is_not_persisted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    monkeypatch.setenv("BIGDIPPER_TOOL_MODE", "langchain")
    monkeypatch.setenv("BIGDIPPER_DB_PATH", str(tmp_path / "chat.db"))
    from app.main import create_app

    slow_provider = SlowFieldToolProvider()
    app = create_app(tool_provider=slow_provider, llm_client=DisabledLLM())
    chat = _route(app, "/api/chat", {"POST"})
    cancel = _route(app, "/api/chat/runs/{run_id}/cancel", {"POST"})
    get_conversation = _route(app, "/api/conversations/{conversation_id}", {"GET"})
    run_id = "test-run-cancelled"
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "response",
            chat(ChatRequest(runId=run_id, message="有哪些学科领域可以查询？")),
        )
    )
    thread.start()
    assert slow_provider.started.wait(timeout=2)
    cancel_response = cancel(run_id)
    thread.join(timeout=2)

    response = result["response"]
    conversation = get_conversation(response.conversation_id)
    assert cancel_response.status == "cancelled"
    assert response.status == "cancelled"
    assert response.message == ""
    assert response.tool_calls == []
    assert response.resource_usage is not None
    assert response.resource_usage.status == "cancelled"
    assert response.resource_usage.summary.tool_calls == 1
    assert conversation.messages == []


def _route(app, path: str, methods: set[str]):
    for route in app.routes:
        if route.path == path and getattr(route, "methods", set()) == methods:
            return route.endpoint
    raise AssertionError(f"Route not found: {methods} {path}")


class SlowFieldToolProvider(ToolProvider):
    def __init__(self) -> None:
        self.started = threading.Event()

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_all_fields",
                description="slow fields",
                parameters={"type": "object", "properties": {}},
            )
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        del arguments
        assert name == "get_all_fields"
        self.started.set()
        time.sleep(0.2)
        return {
            "status": "success",
            "summary": "get_all_fields returned 1 rows",
            "data": [{"display_name": "Computer science"}],
        }


class TimedFieldToolProvider(ToolProvider):
    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="get_all_fields",
                description="fake timed fields",
                parameters={"type": "object", "properties": {}},
            )
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        del arguments
        assert name == "get_all_fields"
        time.sleep(0.005)
        return {
            "status": "success",
            "tool": name,
            "rowCount": 1,
            "data": [{"display_name": "Machine learning"}],
            "summary": "get_all_fields returned 1 rows",
        }


class FakeResourceLLM:
    is_configured = True
    model = "fake-resource-model"

    def complete(self, messages, tools=None, tool_choice=None) -> LLMResult:
        del messages, tools, tool_choice
        return LLMResult(
            content="可以查询 Machine learning。",
            used_llm=True,
            model=self.model,
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        )
