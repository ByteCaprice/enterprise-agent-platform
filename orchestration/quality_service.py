"""Quality application policy; persistence and admission have explicit boundaries."""

import hashlib
import json

from domain.governance import AccessDenied
from domain.platform_operations import DatasetCreate, EvaluationCreate
from domain.quality import compare_runs, score_case, verified_dataset
from domain.schema_validation import JSONSchemaInspector, JSONValidationError, JSONValidationUnavailable
from domain.task import TaskConflict, TaskError, TaskSubmit, TaskValidationUnavailable, canonical_json
from domain.task_admission import EvaluationAdmission
from orchestration.task_service import TaskService


class QualityService:
    def __init__(self, store, auth, *, validator: JSONSchemaInspector | None = None):
        self.store, self.auth, self.validator = store, auth, validator
        self.tasks = TaskService(store, auth, validator=validator)

    def datasets(self, principal, project, spec=None):
        # V1 dataset authoring is admin-only at the HTTP authorization boundary.
        if spec is None:
            return self.store.quality.datasets(project)
        snapshot = canonical_json(spec.model_dump(mode="json"))
        spec = DatasetCreate.model_validate_json(snapshot)
        for case in spec.cases:
            if case.matcher == "schema":
                try:
                    if self.validator is None:
                        raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE")
                    self.validator.check_schema(case.expected)
                except JSONValidationUnavailable as exc:
                    raise TaskValidationUnavailable(exc.code) from None
                except JSONValidationError:
                    raise TaskConflict("Evaluation schema is invalid") from None
        dataset = DatasetCreate.model_validate_json(snapshot).model_dump(mode="json")
        dataset["content_hash"] = hashlib.sha256(canonical_json(dataset).encode()).hexdigest()
        return self.store.quality.create_dataset(project, self.tasks.owner(principal), dataset)

    def create(self, principal, project, spec):
        spec = EvaluationCreate.model_validate_json(spec.model_dump_json())
        self.auth.allowed_resources(principal, project, "quality:run", spec.capability_key)
        owner = self.tasks.owner(principal)
        run_id = hashlib.sha256(canonical_json([project, owner, spec.request_key]).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
        # Replay does not depend on a still-readable dataset, target or validator.
        existing = self.store.quality.find_run(project, owner, run_id, fingerprint)
        command, failure = None, None
        if existing is None:
            try:
                dataset = verified_dataset(self.store.quality.dataset(project, spec.dataset_key, spec.dataset_version))
                prepared = tuple((case["key"], self.tasks.prepare(principal, project,
                    TaskSubmit(capability_key=spec.capability_key, version=spec.capability_version, input=case["input"]),
                    "eval:" + hashlib.sha256(f"{run_id}:{case['key']}".encode()).hexdigest()))
                    for case in dataset["cases"])
                command = EvaluationAdmission(run_id, project, owner, fingerprint, spec.model_dump_json(),
                                               json.dumps(dataset, sort_keys=True, allow_nan=False), prepared)
            except (AccessDenied, TaskError, TaskValidationUnavailable) as exc:
                failure = exc
        with self.tasks.admission.unit_of_work(project, owner) as admission:
            replay = admission.find_evaluation(run_id, fingerprint)
            if replay is not None:
                return replay
            if failure is not None:
                raise failure
            if command is None:
                raise TaskConflict("Evaluation changed during admission")
            return admission.admit_evaluation(command)

    def _owner(self, principal):
        return None if principal.has_scope("admin") else self.tasks.owner(principal)

    def runs(self, principal, project):
        allowed = self.auth.allowed_resources(principal, project, "quality:run")
        return self.store.quality.runs(project, owner=self._owner(principal), allowed_keys=allowed)

    def score(self, principal, project, run_id):
        run, dataset = self.store.quality.scoring_snapshot(project, run_id, owner=self._owner(principal))
        self.auth.allowed_resources(principal, project, "quality:run", run["capability_key"])
        cases = {case["key"]: case for case in dataset["cases"]}
        scores, results = [], []
        for item in run["task_ids"]:
            result = self.tasks.result(principal, project, item["task_id"])
            results.append(result)
            case = cases[item["case_key"]]
            schema_matches = None
            if result.status == "succeeded" and case["matcher"] == "schema":
                schema_matches = self._schema_matches(result.result, case["expected"])
            scores.append(item | {"task_status": result.status,
                                  "passed": score_case(case, result, schema_matches=schema_matches)})
        return self.store.quality.save_scores(project, self.tasks.owner(principal), run, dataset, scores, results)

    def _schema_matches(self, actual, schema):
        try:
            if self.validator is None:
                raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE")
            self.validator.validate(actual, schema)
        except JSONValidationUnavailable as exc:
            raise TaskValidationUnavailable(exc.code) from None
        except JSONValidationError as exc:
            if exc.code == "INSTANCE_INVALID":
                return False
            raise TaskConflict("Evaluation schema is invalid") from None
        return True

    def compare(self, principal, project, left_id, right_id):
        allowed = self.auth.allowed_resources(principal, project, "quality:run")
        left, right, dataset = self.store.quality.comparison_snapshot(
            project, left_id, right_id, owner=self._owner(principal), allowed_keys=allowed)
        return compare_runs(left, right, dataset)

    def reviews(self, principal, project, task_id, spec=None):
        task = self.tasks.get(principal, project, task_id)
        if spec is not None:
            self.auth.allowed_resources(principal, project, "quality:review", task.capability_key)
        return self.store.quality.reviews(project, task_id, owner=self.tasks.owner(principal), spec=spec)
