"""Admission UoW: one locked transaction for tasks, gateway evidence and runs.

The application receives a scoped command port, never a Session. All isolated
validation has finished before entering this transaction. Replay is resolved
again here before inspecting preparation failures, target pins or quotas.
"""

import hashlib
import json
from contextlib import contextmanager

from sqlalchemy import select

from domain.capability import CapabilityConflict
from domain.gateway import GatewayRequestCreate
from domain.platform_operations import EvaluationCreate
from domain.quality import verified_dataset
from domain.task import TaskConflict, TaskError, TaskNotFound, TaskSubmit, canonical_json
from domain.task_admission import AdmissionTarget, RecoverySource, Submission
from infra.store.model.ai_capability import AiCapability, AiCapabilityVersion
from infra.store.model.ai_operations import AiQualityDataset, AiQualityRun
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository
from infra.store.repository.gateway_requests import GatewayRequestRepository
from infra.store.repository.limits import admit_task, project_lock
from infra.store.repository.operations import audit
from infra.store.repository.quality import QualityRepository, detached
from infra.store.repository.task import TaskRepository


class AdmissionRepository(BaseRepository):
    def __init__(self, session_factory, tasks: TaskRepository, gateway: GatewayRequestRepository):
        super().__init__(session_factory)
        self._tasks, self._gateway = tasks, gateway

    def find_submission(self, submission):
        with self._read() as session:
            return self._tasks.find_submission(submission.project, submission.owner, submission.digest,
                                               submission.fingerprint, conn=session)

    @staticmethod
    def _target(session, project, key, version, *, locked=False):
        query = select(AiCapability, AiCapabilityVersion).join(AiCapabilityVersion).where(
            AiCapability.project_key == project, AiCapability.key == key, AiCapabilityVersion.version == version)
        if locked:
            query = query.with_for_update(read=True).execution_options(populate_existing=True)
        result = session.execute(query).one_or_none()
        if result is None:
            raise TaskNotFound("Task capability version not found")
        capability, artifact = result
        try:
            return AdmissionTarget.snapshot(capability.to_dict(), artifact.to_domain())
        except (ValueError, TypeError, RecursionError):
            raise TaskConflict("Task capability artifact is invalid") from None

    def target(self, project, key, version):
        with self._read() as session:
            return self._target(session, project, key, version)

    def _source(self, session, project, task_id, allowed_keys, owner, *, locked=False):
        query = self._tasks._visible(project, allowed_keys, owner).where(AiTask.id == task_id)
        if locked:
            query = query.with_for_update(read=True).execution_options(populate_existing=True)
        result = session.execute(query).one_or_none()
        if result is None:
            raise TaskNotFound("Recovery source Task not found")
        row, capability = result
        body = session.scalar(select(AiTask.input).where(AiTask.id == task_id))
        return RecoverySource(row.capability_id, row.to_domain(capability.key, capability.kind).model_dump_json(),
                              canonical_json(body))

    def recovery_source(self, project, task_id, *, allowed_keys, owner):
        with self._read() as session:
            return self._source(session, project, task_id, allowed_keys, owner)

    @contextmanager
    def unit_of_work(self, project, owner):
        with self._sf.begin() as session:
            project_lock(session, project)
            scope = _AdmissionScope(self, session, project, owner)
            try:
                yield scope
            finally:
                scope.close()


class _AdmissionScope:
    def __init__(self, repository, session, project, owner):
        self._repository, self._session = repository, session
        self._project, self._owner = project, owner
        self._active = True

    def close(self):
        self._active = False

    def _scope(self, project, owner):
        if not self._active:
            raise RuntimeError("Admission scope is closed")
        if (project, owner) != (self._project, self._owner):
            raise TaskConflict("Admission command is outside its project/owner scope")

    def admit(self, prepared):
        request = prepared.submission
        self._scope(request.project, request.owner)
        tasks = self._repository._tasks
        tasks.lock_submission(request.project, request.owner, request.digest, conn=self._session)
        existing = tasks.find_submission(request.project, request.owner, request.digest,
                                         request.fingerprint, conn=self._session)
        if existing is not None:
            return existing, False
        if prepared.failure is not None:
            raise prepared.failure
        if prepared.target is None:
            raise TaskConflict("Submission changed during admission")
        spec = request.spec()
        current = self._repository._target(self._session, request.project, spec.capability_key, spec.version, locked=True)
        artifact, expected = current.artifact(), prepared.target.artifact()
        if current.kind not in {"agent", "workflow"}:
            raise TaskError("Only Agent and Workflow capabilities can be submitted as tasks")
        if (current.capability_id, current.key, current.kind, artifact.id) != (
            prepared.target.capability_id, prepared.target.key, prepared.target.kind, expected.id
        ):
            raise TaskConflict("Task capability changed during admission")
        try:
            artifact.require_published(version=spec.version, capability_id=prepared.target.capability_id,
                                       content_hash=expected.content_hash)
        except CapabilityConflict as exc:
            raise TaskConflict(str(exc)) from None
        if request.origin_task_id is not None:
            source = prepared.recovery
            if source is None or source.task().id != request.origin_task_id:
                raise TaskConflict("Recovery source does not match admission")
            current_source = self._repository._source(self._session, request.project, request.origin_task_id,
                                                      {"*"}, None, locked=True)
            if current_source != source or current.capability_id != source.capability_id or (
                artifact.content_hash != source.task().content_hash
            ):
                raise TaskConflict("Recovery source changed during admission")
        admit_task(self._session, request.project)
        capability = {"id": current.capability_id, "key": current.key, "kind": current.kind}
        return tasks.create(request.project, request.owner, spec, capability, artifact, request.digest,
                            request.fingerprint, conn=self._session, origin_task_id=request.origin_task_id), True

    def record_gateway(self, command: GatewayRequestCreate):
        self._scope(command.project_key, command.submitted_by)
        return self._repository._gateway.create(command, conn=self._session)

    def find_evaluation(self, run_id, fingerprint):
        self._scope(self._project, self._owner)
        return QualityRepository.find_in(self._session, self._project, self._owner, run_id, fingerprint)

    def admit_evaluation(self, command):
        self._scope(command.project, command.owner)
        existing = self.find_evaluation(command.id, command.fingerprint)
        if existing is not None:
            return existing
        spec = EvaluationCreate.model_validate_json(command.spec_json)
        expected_id = hashlib.sha256(canonical_json([command.project, command.owner, spec.request_key]).encode()).hexdigest()
        expected_fingerprint = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
        if command.id != expected_id or command.fingerprint != expected_fingerprint:
            raise TaskConflict("Evaluation request does not match admission")
        dataset = self._session.scalar(select(AiQualityDataset).where(
            AiQualityDataset.project_key == command.project, AiQualityDataset.key == spec.dataset_key,
            AiQualityDataset.version == spec.dataset_version).with_for_update(read=True))
        current = verified_dataset(detached(dataset))
        if current != json.loads(command.dataset_json):
            raise TaskConflict("Evaluation dataset changed during admission")
        if tuple(key for key, _ in command.tasks) != tuple(case["key"] for case in current["cases"]):
            raise TaskConflict("Evaluation cases do not match the dataset")
        task_ids = []
        for case, (case_key, prepared) in zip(current["cases"], command.tasks, strict=True):
            key = "eval:" + hashlib.sha256(f"{command.id}:{case_key}".encode()).hexdigest()
            expected = Submission.snapshot(command.project, command.owner, TaskSubmit(
                capability_key=spec.capability_key, version=spec.capability_version, input=case["input"]), key)
            if prepared.submission != expected:
                raise TaskConflict("Evaluation case does not match admission")
            task, _ = self.admit(prepared)
            task_ids.append({"case_key": case_key, "task_id": task.id})
        row = AiQualityRun(id=command.id, project_key=command.project, owner=command.owner,
                           dataset_key=spec.dataset_key, dataset_version=spec.dataset_version,
                           capability_key=spec.capability_key, capability_version=spec.capability_version,
                           fingerprint=command.fingerprint, task_ids=task_ids, scores=[], state="pending")
        self._session.add(row)
        audit(self._session, command.project, command.owner, "quality_run_admitted", command.id)
        self._session.flush()
        return detached(row)
