from __future__ import annotations

from app import main


def test_default_llm_client_uses_agent_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeDeepSeekClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    values = {
        "BIGDIPPER_AGENT_EXPERIMENT_MODEL": "deepseek-v4-flash",
        "BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT": "120",
    }
    monkeypatch.setattr(main, "get_env", lambda name, default=None: values.get(name, default))
    monkeypatch.setattr(main, "DeepSeekClient", _FakeDeepSeekClient)

    main._default_llm_client()

    assert captured == {"model": "deepseek-v4-flash", "timeout": 120.0}


def test_default_llm_client_handles_invalid_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeDeepSeekClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        main,
        "get_env",
        lambda name, default=None: "invalid" if name == "BIGDIPPER_AGENT_EXPERIMENT_LLM_TIMEOUT" else default,
    )
    monkeypatch.setattr(main, "DeepSeekClient", _FakeDeepSeekClient)

    main._default_llm_client()

    assert captured["timeout"] == 120.0
