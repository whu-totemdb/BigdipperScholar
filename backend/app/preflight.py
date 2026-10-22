"""Validate local configuration; optional schema probe, never print secrets."""
import argparse
import json
import sys
from app.config import get_env
from app.llm.deepseek import DeepSeekClient
from app.mcp_config import load_mcp_connections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="Connect to MCP and request schema")
    args = parser.parse_args()
    missing = []
    if not DeepSeekClient().is_configured:
        missing.append("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    connections, _ = load_mcp_connections()
    if not connections:
        missing.append("enabled MCP server")
    local = any("app.mcp_servers.release_server" in c.get("args", []) for c in connections.values())
    if local:
        missing.extend(key for key in ("DB_HOST", "DB_USER", "DB_NAME") if not get_env(key))
    if missing:
        print("Missing configuration: " + ", ".join(missing))
        return 1
    print("Required configuration present; external services not yet verified.")
    if args.probe:
        from app.agent_experiments import _invoke_tool, _tool_output_failed
        from app.mcp_client import load_configured_mcp_tools
        tools = {tool.name: tool for tool in load_configured_mcp_tools()}
        if "get_database_schema" not in tools:
            print("Custom MCP: verify its documented schema discovery interface.")
            return 1
        value = _invoke_tool(tools["get_database_schema"], {})
        if _tool_output_failed(value):
            print("Schema probe failed; check local configuration and database access.")
            return 1
        print(f"Schema probe succeeded; {len(tools)} tools available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
