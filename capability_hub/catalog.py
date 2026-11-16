"""Explicit catalog composition, without filesystem discovery or configuration IO."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from domain.business_code import CodeReference
from domain.capability import CapabilityConflict, CapabilityDependency, CapabilityNotFound
from domain.capability_bundle import CapabilityBundle
from domain.platform_tool import InstalledReadonlyTool
from framework.business_code import CodeWorkflowRegistry, InstalledCodeWorkflow


@dataclass(frozen=True)
class HubBundle:
    key: str
    version: str
    name: str
    description: str
    resources: tuple[tuple[str, str], ...]
    workflows: tuple[InstalledCodeWorkflow, ...]
    tools: tuple[InstalledReadonlyTool, ...]
    _builder: Callable[[CapabilityDependency], CapabilityBundle] = field(repr=False)

    def build(self, *, policy: CapabilityDependency) -> CapabilityBundle:
        for workflow in self.workflows:
            workflow.verify()
        policy = CapabilityDependency.model_validate_json(policy.model_dump_json())
        return self._builder(policy)

    def describe(self) -> dict:
        return {"key": self.key, "version": self.version, "name": self.name, "description": self.description,
                "capabilities": [{"key": key, "kind": kind, "version": self.version}
                                 for key, kind in self.resources]}


@dataclass(frozen=True, init=False)
class HubCatalog:
    bundles: tuple[HubBundle, ...]
    workflows: CodeWorkflowRegistry
    tools: tuple[InstalledReadonlyTool, ...]
    references: tuple[CodeReference, ...]

    def __init__(self, bundles: Iterable[HubBundle] = ()):
        bundles = tuple(bundles)
        if any(not isinstance(bundle, HubBundle) for bundle in bundles):
            raise ValueError("Install explicit HubBundle descriptors")
        if len({bundle.key for bundle in bundles}) != len(bundles):
            raise CapabilityConflict("Duplicate capability hub bundle key")
        bundles = tuple(sorted(bundles, key=lambda bundle: bundle.key))
        workflows = CodeWorkflowRegistry(workflow for bundle in bundles for workflow in bundle.workflows)
        tools = tuple(tool for bundle in bundles for tool in bundle.tools)
        if len({(tool.key, tool.version) for tool in tools}) != len(tools):
            raise CapabilityConflict("Duplicate capability hub tool key/version")
        references = {}
        for reference in (*workflows.references, *(tool.implementation for tool in tools if tool.implementation is not None)):
            identity = (reference.key, reference.version)
            if identity in references and references[identity] != reference:
                raise CapabilityConflict("Conflicting capability hub code reference")
            references[identity] = reference
        for attr, value in (("bundles", bundles), ("workflows", workflows), ("tools", tools),
                            ("references", tuple(references[key] for key in sorted(references)))):
            object.__setattr__(self, attr, value)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(bundle.key for bundle in self.bundles)

    def resolve(self, key: str) -> HubBundle:
        for bundle in self.bundles:
            if bundle.key == key:
                return bundle
        raise CapabilityNotFound(f"Capability hub bundle not found: {key}")

    def select(self, keys: Iterable[str]) -> HubCatalog:
        if isinstance(keys, str):
            raise ValueError("Select hub bundles with an iterable of complete keys, not a string")
        return HubCatalog(self.resolve(key) for key in dict.fromkeys(keys))

    def build(self, key: str, *, policy: CapabilityDependency) -> CapabilityBundle:
        return self.resolve(key).build(policy=policy)

    def describe(self) -> list[dict]:
        return [bundle.describe() for bundle in self.bundles]


def builtin_catalog() -> HubCatalog:
    # Audited function composition only. No entry points, directory scans, config,
    # installation side effects, provider selection, grants or live endpoint probes.
    from .remitter_search.bundle import bundle_descriptor as remitter_search_bundle
    from .weather_query.bundle import bundle_descriptor as weather_query_bundle

    return HubCatalog((remitter_search_bundle(), weather_query_bundle()))


def load_catalog() -> HubCatalog:
    return builtin_catalog()
