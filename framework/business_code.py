"""Explicit installation of reviewed business code, never artifact-directed imports.

A manifest attests only to the bytes of its explicitly listed local package files.
It is not build attestation: dependencies, interpreter, loaded bytecode, environment
and a callable's provenance remain the responsibility of trusted composition.
Business steps are cooperative, pure async functions, not a Python sandbox. IO
belongs in the platform's Tool/Agent/LLM gateways.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import stat
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from domain.business_code import CODE_WORKFLOW_RESULT_KEY, CodeReference

_MAX_FILES = 128
_MAX_FILE_BYTES = 1_000_000
_MAX_SOURCE_BYTES = 8_000_000


class CodeWorkflowRegistryError(ValueError):
    """The installed code catalog cannot satisfy an exact reviewed reference."""


@dataclass(frozen=True)
class SourceManifest:
    package: str
    files: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.package, str) or len(self.package) > 200 or re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", self.package, flags=re.ASCII
        ) is None:
            raise ValueError("Source manifests require an explicit package name")
        if not isinstance(self.files, tuple) or not 1 <= len(self.files) <= _MAX_FILES:
            raise ValueError("Source manifests require a bounded tuple of audited files")
        for name in self.files:
            if (not isinstance(name, str) or not name or len(name) > 512 or "\\" in name or ":" in name
                    or any(ord(char) < 32 for char in name)):
                raise ValueError("Source files must be relative package resource names")
            path = PurePosixPath(name)
            if not path.parts or path.is_absolute() or path.as_posix() != name or any(part in {".", ".."} for part in path.parts):
                raise ValueError("Source file traversal is forbidden")
        if len(set(self.files)) != len(self.files):
            raise ValueError("Duplicate audited source file")

    def reference(self, key: str, version: str) -> CodeReference:
        """Hash a framed, order-independent inventory of bounded local resources.

        Only filesystem packages are supported; namespace/remote/custom resource
        loaders and archives fail closed. Symlinks below the package root are not
        accepted. Concurrent hostile filesystem replacement is not an attestation
        threat model; deploy reviewed packages read-only.
        """
        try:
            root = resources.files(self.package)
            if not isinstance(root, Path) or root.is_symlink() or not root.is_dir():
                raise ValueError("Source manifest requires a local filesystem package")
            root = root.resolve(strict=True)
            digest = hashlib.sha256(b"enterprise-ai.business-source.v1\0")

            def frame(value: bytes):
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)

            frame(self.package.encode("utf-8"))
            total = 0
            for name in sorted(self.files):
                path = root
                for part in PurePosixPath(name).parts:
                    path = path / part
                    if path.is_symlink():
                        raise ValueError("Audited source resources cannot be symlinks")
                path.resolve(strict=True).relative_to(root)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_FILE_BYTES:
                        raise ValueError("Audited source resources must be bounded regular files")
                    content = stream.read(_MAX_FILE_BYTES + 1)
                total += len(content)
                if len(content) > _MAX_FILE_BYTES or total > _MAX_SOURCE_BYTES:
                    raise ValueError("Audited source manifest exceeds its byte budget")
                frame(name.encode("utf-8"))
                frame(content)
            return CodeReference(key=key, version=version, digest=digest.hexdigest())
        except (OSError, ImportError, TypeError, ValueError) as exc:
            raise CodeWorkflowRegistryError("Audited local source resources could not be verified") from exc


@dataclass(frozen=True, slots=True)
class BusinessStepContext:
    """Trusted metadata only: deliberately no claim, lease, store or gateway."""

    project_key: str
    submitted_by: str
    task_id: str
    task_attempt_id: str
    node_id: str


StepContext = BusinessStepContext
BusinessStep = Callable[[BusinessStepContext, dict], Awaitable[dict]]
BusinessWorkflow = Callable[..., Awaitable[dict]]


def _async_callable(value) -> bool:
    return callable(value) and (inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(value.__call__))


@dataclass(frozen=True)
class InstalledCodeWorkflow:
    reference: CodeReference
    run: BusinessWorkflow = field(repr=False)
    steps: Mapping[str, BusinessStep] = field(default_factory=dict, repr=False)
    source: SourceManifest | None = field(default=None, repr=False)

    def __post_init__(self):
        reference = CodeReference.model_validate(self.reference)
        if not _async_callable(self.run) or not isinstance(self.steps, Mapping) or len(self.steps) > 99:
            raise ValueError("Install bounded, explicitly async business callables")
        steps = dict(self.steps)
        for key, step in steps.items():
            CodeReference(key=key, version=reference.version, digest=reference.digest)
            if key == CODE_WORKFLOW_RESULT_KEY or not _async_callable(step):
                raise ValueError("Install explicit async steps under non-reserved activity keys")
        if self.source is not None and not isinstance(self.source, SourceManifest):
            raise ValueError("Install an explicit source manifest")
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "steps", MappingProxyType(steps))

    def verify(self) -> None:
        if self.source is not None and self.source.reference(self.reference.key, self.reference.version) != self.reference:
            raise CodeWorkflowRegistryError("Installed business source digest does not match its reference")


@dataclass(frozen=True, init=False, eq=False)
class CodeWorkflowRegistry:
    _installed: Mapping[tuple[str, str], InstalledCodeWorkflow] = field(repr=False)

    def __init__(self, workflows: Iterable[InstalledCodeWorkflow] = ()):
        installed = {}
        for workflow in workflows:
            if not isinstance(workflow, InstalledCodeWorkflow):
                raise CodeWorkflowRegistryError("Install explicit business Workflow instances")
            identity = (workflow.reference.key, workflow.reference.version)
            if identity in installed:
                raise CodeWorkflowRegistryError("Duplicate or ambiguous business Workflow key/version")
            workflow.verify()
            installed[identity] = workflow
        object.__setattr__(self, "_installed", MappingProxyType(installed))

    @property
    def references(self) -> tuple[CodeReference, ...]:
        return tuple(self._installed[key].reference for key in sorted(self._installed))

    def resolve(self, reference: CodeReference) -> InstalledCodeWorkflow:
        reference = CodeReference.model_validate(reference)
        installed = self._installed.get((reference.key, reference.version))
        if installed is None or installed.reference != reference:
            raise CodeWorkflowRegistryError("Exact business Workflow reference is not installed")
        installed.verify()
        return installed
