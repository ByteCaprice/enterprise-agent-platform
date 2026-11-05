"""Load a bounded, explicit operator file; no discovery, environment merging or secrets."""

import json
from pathlib import Path

from domain.platform_mcp import MCPCatalog


def strict_json(body: bytes):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON")

    return json.loads(body, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def load_mcp_catalog(path: str) -> MCPCatalog:
    try:
        with Path(path).open("rb") as stream:
            body = stream.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError
        return MCPCatalog.model_validate(strict_json(body))
    except Exception:
        # Validation errors may include supplied values; never print them in a launcher.
        raise ValueError("Invalid MCP deployment catalog") from None


def catalog_metadata(catalog: MCPCatalog) -> list[dict]:
    connections = {(item.project_key, item.key): item for item in catalog.connections}
    return [{"project_key": tool.project_key, "connection_key": tool.connection_key,
             "handler_key": tool.handler_key,
             "handler_version": tool.handler_version(connections[tool.project_key, tool.connection_key])}
            for tool in catalog.tools]
