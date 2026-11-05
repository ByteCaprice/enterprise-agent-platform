"""Independent read-only registry and pure JSON tool; no legacy Tool/MCP imports."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from copy import deepcopy
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from domain.platform_tool import InstalledReadonlyTool, ToolContext
from domain.task_execution import ExecutionFailure


class ReadonlyToolRegistry:
    __slots__ = ("_handlers",)

    def __init__(self, handlers: Iterable[InstalledReadonlyTool]):
        installed: dict[tuple[str, str], InstalledReadonlyTool] = {}
        for handler in handlers:
            if not isinstance(handler, InstalledReadonlyTool) or (handler.key, handler.version) in installed:
                raise ValueError("Install unique, explicit read-only tool handler versions")
            installed[handler.key, handler.version] = handler
        object.__setattr__(self, "_handlers", MappingProxyType(installed.copy()))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Installed read-only tool catalogs are immutable")

    def resolve(self, key: str, version: str) -> InstalledReadonlyTool | None:
        return self._handlers.get((key, version))


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    data: dict[str, JsonValue]
    fields: list[str] = Field(max_length=100)


def _project(values: dict) -> dict:
    try:
        args = _Projection.model_validate(values)
        if len(set(args.fields)) != len(args.fields):
            raise ValueError
        return deepcopy({key: args.data[key] for key in args.fields})
    except (ValueError, KeyError):
        raise ExecutionFailure("TOOL_INPUT_INVALID") from None


async def project_fields(_context: ToolContext, values: dict) -> dict:
    # Only bounded, pure JSON operations run in a thread; no IO or dynamic code.
    return await asyncio.to_thread(_project, values)


def builtin_readonly_tools() -> ReadonlyToolRegistry:
    return ReadonlyToolRegistry([InstalledReadonlyTool("json.project", "1", project_fields)])
