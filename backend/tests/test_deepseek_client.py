from __future__ import annotations

from typing import Any

from app.llm.deepseek import DeepSeekClient


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "private reasoning",
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class _FakeHttpClient:
    def __init__(self, captured: list[dict[str, Any]], **kwargs: Any) -> None:
        del kwargs
        self.captured = captured

    def __enter__(self) -> "_FakeHttpClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, endpoint: str, *, headers: dict[str, str], json: dict[str, Any]) -> _FakeResponse:
        del endpoint, headers
        self.captured.append(json)
        return _FakeResponse()


def test_deepseek_v4_flash_supports_per_call_thinking(monkeypatch) -> None:
    monkeypatch.setenv("LLM_SEND_THINKING", "true")
    monkeypatch.setenv("LLM_SEND_REASONING_EFFORT", "true")
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.llm.deepseek.httpx.Client",
        lambda **kwargs: _FakeHttpClient(captured, **kwargs),
    )
    client = DeepSeekClient(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model="deepseek-v4-flash",
        thinking_enabled=False,
    )

    thinking_result = client.complete(
        [{"role": "user", "content": "research"}],
        thinking=True,
        reasoning_effort="high",
    )
    client.complete([{"role": "user", "content": "route"}], thinking=False)

    assert thinking_result.message is not None
    assert thinking_result.message["reasoning_content"] == "private reasoning"
    assert captured[0]["model"] == "deepseek-v4-flash"
    assert captured[0]["thinking"] == {"type": "enabled"}
    assert captured[0]["reasoning_effort"] == "high"
    assert "temperature" not in captured[0]
    assert captured[1]["thinking"] == {"type": "disabled"}
    assert "temperature" not in captured[1]
