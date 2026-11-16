"""Detached, immutable capability publication plans, not deployment installers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import TypeAdapter

from domain.business_code import CodeReference
from domain.capability import (
    CapabilityConflict,
    CapabilityDependency,
    CapabilityError,
    CapabilityKind,
    CapabilitySpec,
    CapabilityVersionDraft,
    Key,
    Version,
)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True, init=False)
class BundleCapability:
    """Canonical strings own the content; each property returns a fresh model."""

    _spec_json: str = field(repr=False)
    _draft_json: str = field(repr=False)
    content_hash: str

    def __init__(self, spec: CapabilitySpec, draft: CapabilityVersionDraft):
        spec = CapabilitySpec.model_validate_json(spec.model_dump_json())
        draft = CapabilityVersionDraft.model_validate_json(draft.model_dump_json())
        content_hash = draft.fingerprint()
        object.__setattr__(self, "_spec_json", _canonical(spec.model_dump(mode="json")))
        object.__setattr__(self, "_draft_json", _canonical(draft.model_dump(mode="json")))
        object.__setattr__(self, "content_hash", content_hash)

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec.model_validate_json(self._spec_json)

    @property
    def draft(self) -> CapabilityVersionDraft:
        return CapabilityVersionDraft.model_validate_json(self._draft_json)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def kind(self) -> CapabilityKind:
        return self.spec.kind

    @property
    def version(self) -> str:
        return self.draft.version

    def export(self) -> dict:
        return {"spec": json.loads(self._spec_json), "draft": json.loads(self._draft_json),
                "content_hash": self.content_hash}


@dataclass(frozen=True, init=False)
class CapabilityBundle:
    """An explicit DAG of resources, with external dependencies left operator-owned.

    Resource order is deterministic and dependency-first. This is a plan for
    individual publication transactions, never an atomic multi-resource commit.
    """

    key: str
    version: str
    name: str
    description: str
    capabilities: tuple[BundleCapability, ...]
    references: tuple[CodeReference, ...]
    _external_json: tuple[str, ...] = field(repr=False)

    def __init__(self, key: str, version: str, name: str, capabilities: Iterable[BundleCapability], *,
                 description: str = "", references: Iterable[CodeReference] = ()):
        TypeAdapter(Key).validate_python(key)
        TypeAdapter(Version).validate_python(version)
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise CapabilityError("Bundle name must be nonempty and at most 200 characters")
        if not isinstance(description, str) or len(description) > 4000:
            raise CapabilityError("Bundle description must be at most 4000 characters")
        resources: dict[tuple[str, str], BundleCapability] = {}
        kinds: dict[str, CapabilityKind] = {}
        for item in capabilities:
            if not isinstance(item, BundleCapability):
                raise CapabilityError("Bundles contain explicit BundleCapability snapshots")
            identity = (item.key, item.version)
            if item.key in kinds and kinds[item.key] != item.kind:
                raise CapabilityConflict("Bundle capability key has conflicting kinds")
            kinds[item.key] = item.kind
            if identity in resources and resources[identity] != item:
                raise CapabilityConflict("Bundle contains conflicting capability content")
            resources[identity] = item
        if not resources:
            raise CapabilityError("A bundle must contain at least one capability")

        pending = set(resources)
        ordered: list[BundleCapability] = []
        external: set[tuple[str, str]] = set()
        dependencies = {}
        for identity, item in resources.items():
            refs = [(ref.key, ref.version) for ref in item.draft.dependencies]
            if len(set(refs)) != len(refs):
                raise CapabilityError("Bundle resource has duplicate dependencies")
            if any(ref[0] == item.key for ref in refs):
                raise CapabilityError("A bundle capability cannot depend on itself")
            dependencies[identity] = set(refs) & resources.keys()
            external.update(set(refs) - resources.keys())
        while pending:
            ready = sorted(identity for identity in pending if not (dependencies[identity] & pending))
            if not ready:
                raise CapabilityError("Bundle capability dependency cycle")
            ordered.extend(resources[identity] for identity in ready)
            pending.difference_update(ready)

        code = {}
        for reference in references:
            reference = CodeReference.model_validate_json(reference.model_dump_json())
            identity = (reference.key, reference.version)
            if identity in code and code[identity] != reference:
                raise CapabilityConflict("Bundle has conflicting installed code references")
            code[identity] = reference
        for attr, value in (("key", key), ("version", version), ("name", name), ("description", description),
                            ("capabilities", tuple(ordered)),
                            ("references", tuple(code[identity] for identity in sorted(code))),
                            ("_external_json", tuple(_canonical({"key": k, "version": v})
                                                     for k, v in sorted(external)))):
            object.__setattr__(self, attr, value)

    @property
    def external_dependencies(self) -> tuple[CapabilityDependency, ...]:
        return tuple(CapabilityDependency.model_validate_json(value) for value in self._external_json)

    @property
    def resources(self) -> tuple[BundleCapability, ...]:
        return self.capabilities

    @property
    def implementations(self) -> tuple[CodeReference, ...]:
        return self.references

    def export(self) -> dict:
        return {"key": self.key, "version": self.version, "name": self.name, "description": self.description,
                "capabilities": [item.export() for item in self.capabilities],
                "external_dependencies": [ref.model_dump(mode="json") for ref in self.external_dependencies],
                "references": [ref.model_dump(mode="json") for ref in self.references]}

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.export()).encode("utf-8")).hexdigest()
