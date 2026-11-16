"""Detached validation identity rechecked by the publication transaction."""

from dataclasses import dataclass

from domain.capability import CapabilityKind


@dataclass(frozen=True)
class PublicationDependency:
    key: str
    capability_id: str
    kind: CapabilityKind
    version: str
    content_hash: str


@dataclass(frozen=True)
class PublicationEvidence:
    capability_id: str
    kind: CapabilityKind
    version: str
    content_hash: str
    interpreter_profile: str
    dependencies: tuple[PublicationDependency, ...]
