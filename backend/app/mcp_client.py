from __future__ import annotations

import asyncio
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_env
from app.logging_config import get_logger
from app.mcp_config import load_mcp_connections


logger = get_logger("bigdipper.mcp_client")


def load_configured_mcp_tools(config_path: str | None = None):
    _disable_proxy_env_for_mcp()
    connections, use_prefix = load_mcp_connections(config_path)
    if not connections:
        raise RuntimeError("No enabled MCP servers found in mcp_servers.yaml")
    client = MultiServerMCPClient(
        connections,
        tool_name_prefix=use_prefix,
        handle_tool_errors=True,
    )
    tools = asyncio.run(client.get_tools())
    logger.info(
        "mcp_client=loaded servers=%s tool_count=%d tools=%s",
        list(connections.keys()),
        len(tools),
        [tool.name for tool in tools],
    )
    return tools


def _disable_proxy_env_for_mcp() -> None:
    trust_env = (get_env("BIGDIPPER_MCP_TRUST_ENV", "false") or "false").lower() == "true"
    if trust_env:
        return
    for name in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        os.environ.pop(name, None)
