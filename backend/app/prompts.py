from __future__ import annotations

from pathlib import Path

from app.config import get_env


DEFAULT_MAIN_PROMPT = """You are BigDipper Scholar, a research analysis assistant for scholarly literature and institutions.
Use the provided tool results as your factual grounding.
Do not invent paper counts, author rankings, institution statistics, citations, or database fields.
Do not suggest tools, filters, or parameters that are not present in the provided tool calls.
If tool results are empty or uncertain, say so and suggest a narrower query.
Answer in the user's language.
For database-backed answers, briefly mention which tool result was used.
Keep the answer concise but useful for a research workflow."""


class PromptProvider:
    def __init__(
        self,
        main_prompt_path: str | None = None,
        reload_prompts: bool | None = None,
    ) -> None:
        default_path = Path(__file__).resolve().parents[1] / "prompts" / "main_agent.md"
        self.main_prompt_path = Path(
            main_prompt_path or get_env("BIGDIPPER_MAIN_PROMPT_PATH") or default_path
        )
        reload_value = get_env("BIGDIPPER_RELOAD_PROMPTS", "true")
        self.reload_prompts = reload_prompts if reload_prompts is not None else reload_value.lower() == "true"
        self._cached_main_prompt: str | None = None

    def main_agent_prompt(self) -> str:
        if self.reload_prompts or self._cached_main_prompt is None:
            self._cached_main_prompt = self._read_prompt()
        return self._cached_main_prompt

    def _read_prompt(self) -> str:
        try:
            prompt = self.main_prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_MAIN_PROMPT
        return prompt or DEFAULT_MAIN_PROMPT
