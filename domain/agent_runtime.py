"""Framework-neutral runtime port; the platform retains execution authority."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.agent_turn import AgentToolDescriptor, RuntimeTurn
from domain.capability import Key
from domain.task import canonical_json


class AgentRuntimeDescriptor(BaseModel):
    """Capabilities of an explicitly installed, trusted adapter, not a user-supplied target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Key
    profile: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    cancellation: Literal["cooperative"] = "cooperative"
    events: Literal["platform_broker"] = "platform_broker"
    supports_resume: Literal[False] = False


class AgentRuntimeStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    instructions: str = Field(min_length=1, max_length=262144, repr=False)
    input_text: str = Field(min_length=1, max_length=262144, repr=False)
    tools: tuple[AgentToolDescriptor, ...] = Field(max_length=64, repr=False)
    max_turns: int = Field(strict=True, ge=1, le=128)
    max_tool_calls: int = Field(strict=True, ge=0, le=512)
    timeout_seconds: float = Field(strict=True, gt=0, le=86400)

    @model_validator(mode="after")
    def bounded_snapshot(self):
        aliases = [tool.alias for tool in self.tools]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Runtime tool aliases must be unique")
        canonical_json(self.model_dump(mode="json"))
        return self


class AgentRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_text: str = Field(min_length=1, max_length=262144, repr=False)
    turns: int = Field(strict=True, ge=1, le=128)
    tool_calls: int = Field(strict=True, ge=0, le=512)


class AgentRuntimeCallbacks(Protocol):
    async def request_model(self) -> RuntimeTurn: ...

    async def dispatch_tool(self, dispatch_id: str) -> dict: ...


class AgentRuntime(Protocol):
    descriptor: AgentRuntimeDescriptor

    async def check_ready(self) -> None: ...

    async def run(self, start: AgentRuntimeStart, callbacks: AgentRuntimeCallbacks) -> AgentRuntimeResult: ...


@dataclass(frozen=True)
class InstalledAgentRuntime:
    descriptor: AgentRuntimeDescriptor
    adapter: AgentRuntime


@dataclass(frozen=True, init=False, eq=False)
class AgentRuntimeRegistry:
    """A sealed catalog of explicitly composed runtime adapters."""

    _runtimes: Mapping[str, InstalledAgentRuntime] = field(repr=False)

    def __init__(self, runtimes: Iterable[AgentRuntime]):
        installed: dict[str, InstalledAgentRuntime] = {}
        for runtime in runtimes:
            if not callable(getattr(runtime, "check_ready", None)) or not callable(getattr(runtime, "run", None)):
                raise ValueError("Install runtime adapters implementing check_ready and run")
            descriptor = AgentRuntimeDescriptor.model_validate(runtime.descriptor)
            if descriptor.key in installed:
                raise ValueError("Duplicate Agent runtime key")
            installed[descriptor.key] = InstalledAgentRuntime(descriptor=descriptor, adapter=runtime)
        if not installed:
            raise ValueError("At least one Agent runtime must be installed")
        object.__setattr__(self, "_runtimes", MappingProxyType(installed.copy()))

    def get(self, key: str) -> InstalledAgentRuntime | None:
        return self._runtimes.get(key)

    @property
    def descriptors(self) -> tuple[AgentRuntimeDescriptor, ...]:
        return tuple(runtime.descriptor for runtime in self._runtimes.values())

    async def check_ready(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.adapter.check_ready()


class AgentRuntimeUnstoppable(RuntimeError):
    """The child could not be reaped; its owning Worker must never claim again."""
