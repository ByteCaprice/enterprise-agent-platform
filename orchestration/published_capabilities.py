"""Resolve verified, detached artifacts through a read-only catalog port."""

from pydantic import TypeAdapter

from domain.capability import (
    CapabilityDependency,
    CapabilityError,
    CapabilityKind,
    CapabilityReader,
    CapabilityVersionRecord,
    Key,
)


class PublishedCapabilities:
    """Artifact lookup is neither live authorization nor runtime installation.

    Never follows default versions or caches grants. Callers authorize their own
    action and translate catalog errors into their own failure vocabulary.
    """

    def __init__(self, source: CapabilityReader):
        self.source = source

    def resolve(self, project_key: str, reference: CapabilityDependency, kind: CapabilityKind) -> CapabilityVersionRecord:
        TypeAdapter(Key).validate_python(project_key)
        identity = self.source.get(project_key, reference.key)
        if (identity["project_key"], identity["key"]) != (project_key, reference.key):
            raise CapabilityError("Capability identity does not match the requested project and key")
        if identity["kind"] != kind:
            raise CapabilityError("Capability kind does not match the requested resource")
        artifact = self.source.get_version(project_key, reference.key, reference.version).model_copy(deep=True)
        artifact.require_published(version=reference.version, capability_id=identity["id"])
        return artifact
