from __future__ import annotations

from app.agent import ReactScholarAgent, ScholarAgent
from app.config import get_env
from app.llm import DeepSeekClient
from app.prompts import PromptProvider
from app.tools.base import ToolProvider


def create_agent(
    tool_provider: ToolProvider,
    llm_client: DeepSeekClient | None = None,
    prompt_provider: PromptProvider | None = None,
):
    backend = (get_env("AGENT_BACKEND", "research_report") or "research_report").lower()
    if backend == "langgraph":
        from app.langgraph_agent import LangGraphScholarAgent

        return LangGraphScholarAgent(tool_provider, llm_client=llm_client, prompt_provider=prompt_provider)
    if backend == "rule":
        return ScholarAgent(tool_provider, llm_client=llm_client, prompt_provider=prompt_provider)
    if backend == "react":
        return ReactScholarAgent(tool_provider, llm_client=llm_client, prompt_provider=prompt_provider)
    if backend == "research_report":
        from app.research_report_agent import ResearchReportChatAgent

        return ResearchReportChatAgent(llm_client=llm_client)
    raise ValueError("AGENT_BACKEND must be 'react', 'rule', 'langgraph', or 'research_report'")
