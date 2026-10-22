from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from app.config import get_env


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "mcp_servers.yaml"


def load_mcp_connections(config_path: str | None = None) -> tuple[dict[str, dict[str, Any]], bool]:
    path = Path(config_path or get_env("BIGDIPPER_MCP_CONFIG") or DEFAULT_CONFIG_PATH)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    connections: dict[str, dict[str, Any]] = {}
    use_prefix = False
    server_configs = data.get("servers") or {}
    for name, raw_config in server_configs.items():
        if not raw_config or not raw_config.get("enabled", False):
            continue
        connection = _normalize_connection(raw_config)
        connections[name] = connection
        use_prefix = use_prefix or bool(raw_config.get("tool_name_prefix", False))
    return connections, use_prefix


def _normalize_connection(raw_config: dict[str, Any]) -> dict[str, Any]:
    transport = str(raw_config.get("transport", "")).replace("-", "_")
    if transport in {"http", "streamable_http"}:
        return {
            "transport": "http",
            "url": raw_config["url"],
            "headers": _headers(raw_config),
            "timeout": float(raw_config.get("timeout_seconds", 60)),
            "sse_read_timeout": float(raw_config.get("sse_read_timeout_seconds", 300)),
        }
    if transport == "sse":
        return {
            "transport": "sse",
            "url": raw_config["url"],
            "headers": _headers(raw_config),
            "timeout": float(raw_config.get("timeout_seconds", 60)),
            "sse_read_timeout": float(raw_config.get("sse_read_timeout_seconds", 300)),
        }
    if transport == "stdio":
        env = {**os.environ, **(raw_config.get("env") or {})}
        return {
            "transport": "stdio",
            "command": sys.executable if raw_config["command"] == "python" else raw_config["command"],
            "args": raw_config.get("args", []),
            "cwd": raw_config.get("cwd"),
            "env": env,
        }
    raise ValueError(f"Unsupported MCP transport: {transport}")


def _headers(raw_config: dict[str, Any]) -> dict[str, str]:
    headers = dict(raw_config.get("headers") or {})
    api_key_env = raw_config.get("api_key_env")
    header_name = raw_config.get("api_key_header", "x-internal-key")
    if api_key_env and os.getenv(api_key_env):
        headers[header_name] = os.getenv(api_key_env, "")
    return headers
