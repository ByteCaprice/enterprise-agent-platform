"""Immutable datasets, atomic bounded evaluation admission and deterministic scoring."""

import hashlib
from uuid import uuid4

from sqlalchemy import select, text

from domain import utc_now
from domain.governance import AccessDenied
from domain.platform_operations import DatasetCreate
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import TaskConflict, TaskNotFound, TaskSubmit, TaskValidationUnavailable, canonical_json
from infra.schema_validation import check_json_schema, validate_json
from infra.store.model.ai_operations import AiQualityDataset, AiQualityRun, AiTaskReview
from infra.store.repository.limits import project_lock
from infra.store.repository.operations import audit, record
from orchestration.task_service import TaskService


def verified_dataset(row):
    if row is None:
        raise TaskNotFound("Dataset not found")
    try:
        spec = DatasetCreate.model_validate({"key": row.key, "version": row.version, "cases": row.cases})
        digest = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
    except (ValueError, TypeError):
        raise TaskConflict("Evaluation dataset is invalid") from None
    if digest != row.content_hash:
        raise TaskConflict("Evaluation dataset content does not match its published hash")
    return row


class QualityService:
    def __init__(self, store, auth):
        self.store, self.auth = store, auth
        self.tasks = TaskService(store, auth)

    def datasets(self, principal, project, spec=None):
        owner = self.tasks.owner(principal)
        # Datasets have no executable capability until a run is admitted; V1 authoring is admin-only.
        with self.store.unit_of_work() as session:
            project_lock(session, project)
            if spec is not None:
                for case in spec.cases:
                    if case.matcher == "schema":
                        try:
                            check_json_schema(case.expected)
                        except JSONValidationUnavailable as exc:
                            raise TaskValidationUnavailable(exc.code) from None
                        except JSONValidationError:
                            raise TaskConflict("Evaluation schema is invalid") from None
                if session.get(AiQualityDataset, (project, spec.key, spec.version)):
                    raise TaskConflict("Dataset versions are immutable")
                row = AiQualityDataset(project_key=project, owner=owner, key=spec.key, version=spec.version,
                    content_hash=hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest(),
                    cases=[case.model_dump() for case in spec.cases])
                session.add(row)
                audit(session, project, owner, "quality_dataset_created", f"{spec.key}@{spec.version}")
                session.flush()
                return record(row)
            return [record(row) for row in session.scalars(select(AiQualityDataset).where(
                AiQualityDataset.project_key == project).order_by(AiQualityDataset.key, AiQualityDataset.version).limit(100))]

    def create(self, principal, project, spec):
        self.auth.allowed_resources(principal, project, "quality:run", spec.capability_key)
        owner = self.tasks.owner(principal)
        run_id = hashlib.sha256(canonical_json([project, owner, spec.request_key]).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
        with self.store.unit_of_work() as session:
            project_lock(session, project)
            existing = session.get(AiQualityRun, run_id)
            if existing:
                if existing.fingerprint != fingerprint:
                    raise TaskConflict("Evaluation request key already used")
                return record(existing)
            dataset = verified_dataset(session.get(AiQualityDataset, (project, spec.dataset_key, spec.dataset_version)))
            tasks = []
            for case in dataset.cases:
                task, _ = self.tasks.submit(principal, project, TaskSubmit(capability_key=spec.capability_key,
                    version=spec.capability_version, input=case["input"]),
                    "eval:" + hashlib.sha256(f"{run_id}:{case['key']}".encode()).hexdigest(), conn=session)
                tasks.append({"case_key": case["key"], "task_id": task.id})
            row = AiQualityRun(id=run_id, project_key=project, dataset_key=spec.dataset_key,
                dataset_version=spec.dataset_version, owner=owner, capability_key=spec.capability_key,
                capability_version=spec.capability_version, fingerprint=fingerprint, task_ids=tasks, scores=[], state="pending")
            session.add(row)
            audit(session, project, owner, "quality_run_admitted", run_id)
            session.flush()
            return record(row)

    def runs(self, principal, project):
        allowed = self.auth.allowed_resources(principal, project, "quality:run")
        with self.store.unit_of_work() as session:
            query = select(AiQualityRun).where(AiQualityRun.project_key == project)
            if not principal.has_scope("admin"):
                query = query.where(AiQualityRun.owner == self.tasks.owner(principal))
            if "*" not in allowed:
                query = query.where(AiQualityRun.capability_key.in_(allowed))
            return [record(row) for row in session.scalars(query.order_by(AiQualityRun.id).limit(100))]

    def score(self, principal, project, run_id):
        with self.store.unit_of_work() as session:
            row = session.scalar(select(AiQualityRun).where(AiQualityRun.project_key == project,
                AiQualityRun.id == run_id).with_for_update())
            if row is None or (not principal.has_scope("admin") and row.owner != self.tasks.owner(principal)):
                raise TaskNotFound("Evaluation not found")
            self.auth.allowed_resources(principal, project, "quality:run", row.capability_key)
            dataset = verified_dataset(session.get(AiQualityDataset, (project, row.dataset_key, row.dataset_version)))
            cases = {case["key"]: case for case in dataset.cases}
            scores = []
            for item in row.task_ids:
                result = self.tasks.result(principal, project, item["task_id"])
                case = cases[item["case_key"]]
                actual = result.result
                passed = False
                if result.status == "succeeded":
                    if case["matcher"] == "exact":
                        passed = canonical_json(actual) == canonical_json(case["expected"])
                    elif case["matcher"] == "contains":
                        passed = isinstance(actual, dict) and all(key in actual and canonical_json(actual[key]) == canonical_json(value)
                                                                 for key, value in case["expected"].items())
                    else:
                        try:
                            validate_json(actual, case["expected"])
                        except JSONValidationUnavailable as exc:
                            raise TaskValidationUnavailable(exc.code) from None
                        except JSONValidationError as exc:
                            if exc.code == "INSTANCE_INVALID":
                                passed = False
                            else:
                                raise TaskConflict("Evaluation schema is invalid") from None
                        else:
                            passed = True
                scores.append(item | {"task_status": result.status,
                    "passed": passed if result.status not in {"queued", "running"} else None})
            row.scores = scores
            row.state = "pending" if any(score["passed"] is None for score in scores) else "completed"
            audit(session, project, self.tasks.owner(principal), "quality_run_scored", run_id)
            session.flush()
            return record(row)

    def compare(self, principal, project, left_id, right_id):
        """Compare completed runs over the same immutable dataset and capability."""
        owner = self.tasks.owner(principal)
        allowed = self.auth.allowed_resources(principal, project, "quality:run")
        with self.store.unit_of_work() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            rows = session.scalars(select(AiQualityRun).where(
                AiQualityRun.project_key == project, AiQualityRun.id.in_({left_id, right_id}),
            )).all()
            by_id = {row.id: row for row in rows}
            left, right = by_id.get(left_id), by_id.get(right_id)
            if left is None or right is None or (
                not principal.has_scope("admin") and (left.owner != owner or right.owner != owner)
            ):
                raise TaskNotFound("Evaluation not found")
            if "*" not in allowed and (left.capability_key not in allowed or right.capability_key not in allowed):
                raise AccessDenied("Resource access denied")
            if (left.dataset_key, left.dataset_version, left.capability_key) != (
                right.dataset_key, right.dataset_version, right.capability_key,
            ):
                raise TaskConflict("Evaluation runs must use the same dataset version and capability")
            if left.state != "completed" or right.state != "completed":
                raise TaskConflict("Both evaluation runs must be scored and complete")
            dataset = verified_dataset(session.get(AiQualityDataset, (project, left.dataset_key, left.dataset_version)))
            case_keys = [case["key"] for case in dataset.cases]

            def validated_scores(run):
                if not isinstance(run.scores, list) or not isinstance(run.task_ids, list):
                    raise TaskConflict("Evaluation evidence is incomplete")
                if any(not isinstance(item, dict) for item in (*run.scores, *run.task_ids)):
                    raise TaskConflict("Evaluation evidence is incomplete")
                scores = {item.get("case_key"): item for item in run.scores}
                task_ids = {item.get("case_key"): item.get("task_id") for item in run.task_ids}
                if (len(scores) != len(run.scores) or len(task_ids) != len(run.task_ids) or
                        set(scores) != set(case_keys) or set(task_ids) != set(case_keys)):
                    raise TaskConflict("Evaluation cases do not match the dataset")
                for key, item in scores.items():
                    if (not isinstance(task_ids[key], str) or not task_ids[key] or
                            item.get("task_id") != task_ids[key] or type(item.get("passed")) is not bool or
                            item.get("task_status") not in {"succeeded", "failed", "timed_out", "cancelled"} or
                            (item["passed"] and item["task_status"] != "succeeded")):
                        raise TaskConflict("Evaluation evidence is incomplete")
                return scores

            left_scores, right_scores = validated_scores(left), validated_scores(right)
            cases = [{"case_key": key,
                      "left": {"task_status": left_scores[key]["task_status"], "passed": left_scores[key]["passed"]},
                      "right": {"task_status": right_scores[key]["task_status"], "passed": right_scores[key]["passed"]}}
                     for key in case_keys]

            def summary(run, scores):
                return {"run_id": run.id, "capability_version": run.capability_version,
                        "technical_succeeded": sum(item["task_status"] == "succeeded" for item in scores.values()),
                        "evaluation_passed": sum(item["passed"] for item in scores.values())}

            return {"dataset": {"key": dataset.key, "version": dataset.version, "content_hash": dataset.content_hash},
                    "capability_key": left.capability_key, "case_count": len(case_keys),
                    "left": summary(left, left_scores), "right": summary(right, right_scores), "cases": cases}

    def reviews(self, principal, project, task_id, spec=None):
        task = self.tasks.get(principal, project, task_id)
        if spec is not None:
            self.auth.allowed_resources(principal, project, "quality:review", task.capability_key)
        with self.store.unit_of_work() as session:
            if spec is not None:
                row = AiTaskReview(id=uuid4().hex, project_key=project, task_id=task_id,
                    reviewer=self.tasks.owner(principal), create_time=utc_now(), **spec.model_dump())
                session.add(row)
                audit(session, project, row.reviewer, "task_review_added", task_id)
                session.flush()
                return record(row)
            return [record(row) for row in session.scalars(select(AiTaskReview).where(AiTaskReview.project_key == project,
                AiTaskReview.task_id == task_id).order_by(AiTaskReview.create_time).limit(200))]
