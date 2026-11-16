"""Install exact, project-bound MCP pins into the existing read-only Tool Gateway."""

from domain.platform_mcp import MCPCatalog, MCPConnection, MCPToolPin
from domain.platform_tool import InstalledReadonlyTool
from domain.schema_validation import JSONValidationUnavailable
from domain.task import canonical_json
from domain.task_execution import ExecutionFailure
from framework.readonly_tools import ReadonlyToolRegistry, builtin_readonly_tools
from infra.mcp_http import MCPJSONTransport
from infra.schema_validation import validate_json
from orchestration.execution_async import run_sync


def _installed_tool(connection, pin, transport):
    # Capture bytes, not a caller's mutable nested Pydantic schema dictionaries.
    connection_json = connection.model_dump_json()
    pin_json = pin.model_dump_json()

    async def execute(context, values):
        approved_connection = MCPConnection.model_validate_json(connection_json)
        approved = MCPToolPin.model_validate_json(pin_json)
        if context.project_key != approved.project_key:
            raise ExecutionFailure("TOOL_MCP_PROJECT_DENIED")
        try:
            canonical_json(values)
            await run_sync(validate_json, values, approved.input_schema)
        except JSONValidationUnavailable:
            raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
        except Exception:
            raise ExecutionFailure("TOOL_MCP_INPUT_INVALID") from None
        async with transport.open(approved_connection) as session:
            await session.initialize()
            remote = next((tool for tool in await session.tools() if tool["name"] == approved.remote_tool), None)
            if (remote is None or canonical_json(remote.get("inputSchema")) != canonical_json(approved.input_schema) or
                    ("outputSchema" in remote if approved.result_mode == "text" else
                     canonical_json(remote.get("outputSchema")) != canonical_json(approved.output_schema)) or
                    (isinstance(remote.get("execution"), dict) and remote["execution"].get("taskSupport") == "required")):
                raise ExecutionFailure("TOOL_MCP_SCHEMA_CHANGED")
            result = await session.call(approved.remote_tool, values)
            output = result.get("structuredContent")
            content = result.get("content")
            if (result.get("isError", False) is not False or
                    not isinstance(content, list) or any(not isinstance(item, dict) or item.get("type") != "text"
                                                        or not isinstance(item.get("text"), str) for item in content)):
                raise ExecutionFailure("TOOL_MCP_OUTPUT_REJECTED")
            if approved.result_mode == "text":
                if not content or output is not None:
                    raise ExecutionFailure("TOOL_MCP_OUTPUT_REJECTED")
                output = {"text": "\n".join(item["text"] for item in content)}
            elif not isinstance(output, dict):
                raise ExecutionFailure("TOOL_MCP_OUTPUT_REJECTED")
            try:
                canonical_json(output)
                await run_sync(validate_json, output, approved.output_schema)
            except JSONValidationUnavailable:
                raise ExecutionFailure("TOOL_VALIDATION_UNAVAILABLE") from None
            except Exception:
                raise ExecutionFailure("TOOL_MCP_OUTPUT_REJECTED") from None
            return output

    return InstalledReadonlyTool(pin.handler_key, pin.handler_version(connection), execute)


def mcp_readonly_tools(catalog: MCPCatalog, *, transport=None) -> ReadonlyToolRegistry:
    transport = transport or MCPJSONTransport()
    connections = {(item.project_key, item.key): item for item in catalog.connections}
    builtin = builtin_readonly_tools().resolve("json.project", "1")
    assert builtin is not None
    return ReadonlyToolRegistry([builtin, *[
        _installed_tool(connections[pin.project_key, pin.connection_key], pin, transport) for pin in catalog.tools
    ]])
