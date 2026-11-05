"""Explicit, immutable installation contract for non-rule Workflow node adapters.

These descriptors describe trusted process composition, not resource grants.
Published definitions cannot install handlers or mark external work as pure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from domain.task_execution import TaskClaim
from domain.workflow import WorkflowNode

WorkflowNodeExecutor = Callable[[TaskClaim, WorkflowNode, dict, str], Awaitable[dict]]


class WorkflowAdapterDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent", "tool", "llm"]
    profile: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    execution: Literal["serial"] = "serial"
    retry: Literal["unsupported"] = "unsupported"
    resume: Literal["unsupported"] = "unsupported"


class WorkflowNodeAdapter(Protocol):
    descriptor: WorkflowAdapterDescriptor

    async def __call__(self, claim: TaskClaim, node: WorkflowNode, values: dict, node_id: str) -> dict: ...


@dataclass(frozen=True)
class InstalledWorkflowAdapter:
    descriptor: WorkflowAdapterDescriptor
    adapter: WorkflowNodeExecutor


class WorkflowAdapterUnavailable(ValueError):
    pass


@dataclass(frozen=True, init=False, eq=False)
class WorkflowNodeRegistry:
    """A sealed adapter catalog; no imports, lookup-by-Python-path or mutable fallback."""

    _installed: Mapping[str, InstalledWorkflowAdapter] = field(repr=False)

    def __init__(self, adapters: Iterable[WorkflowNodeAdapter | InstalledWorkflowAdapter] = ()):
        installed = {}
        for adapter in adapters:
            executor = adapter.adapter if isinstance(adapter, InstalledWorkflowAdapter) else adapter
            if not callable(executor):
                raise ValueError("Workflow adapters must be explicitly installed callable instances")
            descriptor = WorkflowAdapterDescriptor.model_validate(adapter.descriptor)
            if descriptor.kind in installed:
                raise ValueError("Duplicate Workflow node adapter kind")
            installed[descriptor.kind] = InstalledWorkflowAdapter(descriptor, executor)
        object.__setattr__(self, "_installed", MappingProxyType(installed))

    @classmethod
    def from_mapping(cls, adapters: Mapping[str, WorkflowNodeExecutor]) -> WorkflowNodeRegistry:
        """Explicit function injection remains available; every function receives a descriptor.

        Concrete adapters may declare their own descriptor; a mapping cannot silently
        relabel it. Bare callables always have the conservative serial/no-retry contract.
        """
        installed = {}
        for kind, adapter in adapters.items():
            if kind not in {"agent", "tool", "llm"} or not callable(adapter):
                raise ValueError("Install explicit supported node adapters")
            declared = getattr(adapter, "descriptor", None)
            descriptor = WorkflowAdapterDescriptor.model_validate(declared) if declared is not None else (
                WorkflowAdapterDescriptor.model_validate({"kind": kind, "profile": f"enterprise-ai.workflow.{kind}.callable.v1"})
            )
            if descriptor.kind != kind:
                raise ValueError("Workflow adapter kind does not match its installation key")
            installed[kind] = InstalledWorkflowAdapter(descriptor, adapter)
        return cls(installed.values())

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._installed)

    @property
    def descriptors(self) -> tuple[WorkflowAdapterDescriptor, ...]:
        return tuple(item.descriptor for item in self._installed.values())

    @property
    def executors(self) -> Mapping[str, WorkflowNodeExecutor]:
        return MappingProxyType({kind: item.adapter for kind, item in self._installed.items()})

    def get(self, kind: str) -> InstalledWorkflowAdapter | None:
        return self._installed.get(kind)

    def require(self, kinds: Iterable[str]) -> None:
        if any(kind not in self._installed for kind in kinds):
            raise WorkflowAdapterUnavailable("Workflow requires an adapter not installed in this process")
