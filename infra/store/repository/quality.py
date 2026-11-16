"""Focused quality persistence; detached records cross the application boundary."""

from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select, text

from domain import utc_now
from domain.governance import AccessDenied
from domain.quality import verified_dataset
from domain.task import TaskConflict, TaskNotFound, canonical_json
from infra.store.model.ai_governance import AiProject
from infra.store.model.ai_operations import AiQualityDataset, AiQualityRun, AiTaskReview
from infra.store.model.ai_task import AiTask
from infra.store.repository.base import BaseRepository
from infra.store.repository.limits import project_lock
from infra.store.repository.operations import audit, record


def detached(row):
    return deepcopy(record(row)) if row is not None else None


class QualityRepository(BaseRepository):
    def datasets(self, project):
        with self._read() as session:
            if not session.scalar(select(AiProject.enabled).where(AiProject.key == project)):
                raise TaskConflict("Project is unavailable")
            return [detached(row) for row in session.scalars(select(AiQualityDataset).where(
                AiQualityDataset.project_key == project).order_by(AiQualityDataset.key, AiQualityDataset.version).limit(100))]

    def create_dataset(self, project, owner, dataset):
        dataset = verified_dataset(deepcopy(dataset))
        with self._sf.begin() as session:
            project_lock(session, project)
            if session.get(AiQualityDataset, (project, dataset["key"], dataset["version"])):
                raise TaskConflict("Dataset versions are immutable")
            row = AiQualityDataset(project_key=project, owner=owner, **dataset)
            session.add(row)
            audit(session, project, owner, "quality_dataset_created", f"{row.key}@{row.version}")
            session.flush()
            return detached(row)

    def dataset(self, project, key, version):
        with self._read() as session:
            return verified_dataset(detached(session.get(AiQualityDataset, (project, key, version))))

    @staticmethod
    def find_in(session, project, owner, run_id, fingerprint):
        row = session.get(AiQualityRun, run_id)
        if row is None:
            return None
        if row.project_key != project or row.owner != owner:
            raise TaskNotFound("Evaluation not found")
        if row.fingerprint != fingerprint:
            raise TaskConflict("Evaluation request key already used")
        return detached(row)

    def find_run(self, project, owner, run_id, fingerprint):
        with self._read() as session:
            return self.find_in(session, project, owner, run_id, fingerprint)

    @staticmethod
    def visible(row, owner, allowed_keys):
        if row is None or (owner is not None and row.owner != owner):
            raise TaskNotFound("Evaluation not found")
        if "*" not in allowed_keys and row.capability_key not in allowed_keys:
            raise AccessDenied("Resource access denied")
        return detached(row)

    def runs(self, project, *, owner, allowed_keys):
        with self._read() as session:
            query = select(AiQualityRun).where(AiQualityRun.project_key == project)
            if owner is not None:
                query = query.where(AiQualityRun.owner == owner)
            if "*" not in allowed_keys:
                query = query.where(AiQualityRun.capability_key.in_(allowed_keys))
            return [detached(row) for row in session.scalars(query.order_by(AiQualityRun.id).limit(100))]

    def scoring_snapshot(self, project, run_id, *, owner):
        with self._sf.begin() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            row = session.scalar(select(AiQualityRun).where(AiQualityRun.project_key == project, AiQualityRun.id == run_id))
            run = self.visible(row, owner, {"*"})
            dataset = verified_dataset(detached(session.get(AiQualityDataset, (project, row.dataset_key, row.dataset_version))))
            return run, dataset

    def save_scores(self, project, owner, baseline, dataset, scores, results):
        with self._sf.begin() as session:
            row = session.scalar(select(AiQualityRun).where(
                AiQualityRun.project_key == project, AiQualityRun.id == baseline["id"]).with_for_update())
            if row is None:
                raise TaskNotFound("Evaluation not found")
            if detached(row) != baseline:
                raise TaskConflict("Evaluation changed while scoring; retry scoring")
            current = session.scalar(select(AiQualityDataset).where(
                AiQualityDataset.project_key == project, AiQualityDataset.key == row.dataset_key,
                AiQualityDataset.version == row.dataset_version).with_for_update(read=True))
            if verified_dataset(detached(current)) != dataset:
                raise TaskConflict("Evaluation dataset changed while scoring")
            for result in sorted(results, key=lambda item: item.task_id):
                current = session.execute(select(AiTask.status, AiTask.result, AiTask.error_code).where(
                    AiTask.project_key == project, AiTask.id == result.task_id).with_for_update(read=True)).one_or_none()
                if current is None or (current.status, canonical_json(current.result), current.error_code) != (
                    result.status, canonical_json(result.result), result.error_code
                ):
                    raise TaskConflict("Evaluation task changed while scoring; retry scoring")
            row.scores = deepcopy(scores)
            row.state = "pending" if any(score["passed"] is None for score in scores) else "completed"
            audit(session, project, owner, "quality_run_scored", row.id)
            session.flush()
            return detached(row)

    def comparison_snapshot(self, project, left_id, right_id, *, owner, allowed_keys):
        with self._sf.begin() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            rows = {row.id: row for row in session.scalars(select(AiQualityRun).where(
                AiQualityRun.project_key == project, AiQualityRun.id.in_({left_id, right_id})))}
            left = self.visible(rows.get(left_id), owner, allowed_keys)
            right = self.visible(rows.get(right_id), owner, allowed_keys)
            dataset = detached(session.get(AiQualityDataset, (project, left["dataset_key"], left["dataset_version"])))
            return left, right, dataset

    def reviews(self, project, task_id, *, owner, spec=None):
        with self._sf.begin() as session:
            if spec is not None:
                row = AiTaskReview(id=uuid4().hex, project_key=project, task_id=task_id,
                                   reviewer=owner, create_time=utc_now(), **spec.model_dump())
                session.add(row)
                audit(session, project, owner, "task_review_added", task_id)
                session.flush()
                return detached(row)
            return [detached(row) for row in session.scalars(select(AiTaskReview).where(
                AiTaskReview.project_key == project, AiTaskReview.task_id == task_id)
                .order_by(AiTaskReview.create_time).limit(200))]
