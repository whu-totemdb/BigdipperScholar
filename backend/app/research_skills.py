"""Task-driven skill selection, independent of benchmark task-family labels."""
from pathlib import Path
from langchain_core.tools import StructuredTool
from app.config import get_env

NAMES = {"basic_access", "integrated_analysis", "semantic_synthesis", "mmsql"}
ROOT = Path(__file__).resolve().parents[1] / "prompts/agent_research_report_flow/runtime_docs"


def read_research_skill(name: str) -> str:
    """Read a task skill: basic_access, integrated_analysis, semantic_synthesis, or mmsql. Choose from the user's actual task."""
    if name not in NAMES:
        raise ValueError(f"Unknown skill. Available: {', '.join(sorted(NAMES))}")
    root = Path(get_env("BIGDIPPER_AGENT_PROMPT_DOCS_DIR") or ROOT)
    filename = "mmsql_tutorial_for_sql_agent.md" if name == "mmsql" else f"{name}_skill.md"
    return (root / filename).read_text(encoding="utf-8")


def skill_tool():
    return StructuredTool.from_function(read_research_skill)
