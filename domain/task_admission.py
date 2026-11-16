"""Detached admission commands and a project/owner-scoped transactional port.

JSON strings deliberately freeze nested input/artifact data, not just the outer
dataclass. Preparation is untrusted until the write scope rechecks its pins.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from domain.capability import CapabilityVersionRecord
from domain.gateway import GatewayRequestCreate, GatewayRequestRecord
from domain.task import TaskRecord, TaskSubmit, canonical_json


@dataclass(frozen=True)
class Submission:
    project: str
    owner: str
    spec_json: str = field(repr=False)
    digest: str
    fingerprint: str
    origin_task_id: str | None = None

    @classmethod
    def snapshot(cls, project, owner, spec, key, origin_task_id=None):
        frozen = canonical_json(spec.model_dump(mode="json", exclude_none=True))
        spec = TaskSubmit.model_validate_json(frozen)
        fingerprint = spec.fingerprint()
        if origin_task_id is not None:
            fingerprint = hashlib.sha256(f"{fingerprint}:{origin_task_id}".encode()).hexdigest()
        return cls(project, owner, frozen, hashlib.sha256(key.encode()).hexdigest(), fingerprint, origin_task_id)

    def spec(self) -> TaskSubmit:
        return TaskSubmit.model_validate_json(self.spec_json)


@dataclass(frozen=True)
class AdmissionTarget:
    capability_id: str
    key: str
    kind: str
    artifact_json: str = field(repr=False)

    @classmethod
    def snapshot(cls, capability, artifact):
        return cls(capability["id"], capability["key"], capability["kind"], artifact.model_dump_json())

    def artifact(self) -> CapabilityVersionRecord:
        return CapabilityVersionRecord.model_validate_json(self.artifact_json)


@dataclass(frozen=True)
class RecoverySource:
    capability_id: str
    task_json: str = field(repr=False)
    input_json: str = field(repr=False)

    def task(self) -> TaskRecord:
        return TaskRecord.model_validate_json(self.task_json)

    def input(self):
        return json.loads(self.input_json)


@dataclass(frozen=True)
class PreparedTask:
    submission: Submission
    target: AdmissionTarget | None = None
    failure: Exception | None = None
    recovery: RecoverySource | None = None


@dataclass(frozen=True)
class EvaluationAdmission:
    id: str
    project: str
    owner: str
    fingerprint: str
    spec_json: str = field(repr=False)
    dataset_json: str = field(repr=False)
    tasks: tuple[tuple[str, PreparedTask], ...]


class AdmissionScope(Protocol):
    """One transaction; methods reject commands outside its project/owner."""

    def admit(self, prepared: PreparedTask) -> tuple[TaskRecord, bool]: ...

    def record_gateway(self, command: GatewayRequestCreate) -> GatewayRequestRecord: ...

    def find_evaluation(self, run_id: str, fingerprint: str) -> dict | None: ...

    def admit_evaluation(self, command: EvaluationAdmission) -> dict: ...


class TaskAdmission(Protocol):
    def find_submission(self, submission: Submission) -> TaskRecord | None: ...

    def target(self, project: str, key: str, version: str) -> AdmissionTarget: ...

    def recovery_source(self, project: str, task_id: str, *, allowed_keys, owner: str | None) -> RecoverySource: ...

    def unit_of_work(self, project: str, owner: str) -> AbstractContextManager[AdmissionScope]: ...
