"""Dataset integrity and deterministic evaluation policy, independent of storage."""

import hashlib

from domain.platform_operations import DatasetCreate
from domain.task import TaskConflict, TaskNotFound, canonical_json


def verified_dataset(dataset):
    if dataset is None:
        raise TaskNotFound("Dataset not found")
    try:
        spec = DatasetCreate.model_validate({key: dataset[key] for key in ("key", "version", "cases")})
        digest = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
    except (ValueError, TypeError, KeyError):
        raise TaskConflict("Evaluation dataset is invalid") from None
    if digest != dataset["content_hash"]:
        raise TaskConflict("Evaluation dataset content does not match its published hash")
    return dataset


def score_case(case, result, *, schema_matches: bool | None = None):
    """Pure score policy; schema matching evidence is supplied by the use case."""
    if result.status != "succeeded":
        return None if result.status in {"queued", "running"} else False
    actual = result.result
    if case["matcher"] == "exact":
        return canonical_json(actual) == canonical_json(case["expected"])
    if case["matcher"] == "contains":
        return isinstance(actual, dict) and all(
            key in actual and canonical_json(actual[key]) == canonical_json(value)
            for key, value in case["expected"].items())
    if case["matcher"] != "schema" or type(schema_matches) is not bool:
        raise TaskConflict("Evaluation schema matching evidence is required")
    return schema_matches


def compare_runs(left, right, dataset):
    if tuple(left[key] for key in ("dataset_key", "dataset_version", "capability_key")) != tuple(
        right[key] for key in ("dataset_key", "dataset_version", "capability_key")
    ):
        raise TaskConflict("Evaluation runs must use the same dataset version and capability")
    if left["state"] != "completed" or right["state"] != "completed":
        raise TaskConflict("Both evaluation runs must be scored and complete")
    dataset = verified_dataset(dataset)
    case_keys = [case["key"] for case in dataset["cases"]]

    def validated_scores(run):
        if not isinstance(run["scores"], list) or not isinstance(run["task_ids"], list):
            raise TaskConflict("Evaluation evidence is incomplete")
        if any(not isinstance(item, dict) for item in (*run["scores"], *run["task_ids"])):
            raise TaskConflict("Evaluation evidence is incomplete")
        scores = {item.get("case_key"): item for item in run["scores"]}
        tasks = {item.get("case_key"): item.get("task_id") for item in run["task_ids"]}
        if (len(scores) != len(run["scores"]) or len(tasks) != len(run["task_ids"]) or
                set(scores) != set(case_keys) or set(tasks) != set(case_keys)):
            raise TaskConflict("Evaluation cases do not match the dataset")
        for key, item in scores.items():
            if (not isinstance(tasks[key], str) or not tasks[key] or item.get("task_id") != tasks[key] or
                    type(item.get("passed")) is not bool or
                    item.get("task_status") not in {"succeeded", "failed", "timed_out", "cancelled"} or
                    (item["passed"] and item["task_status"] != "succeeded")):
                raise TaskConflict("Evaluation evidence is incomplete")
        return scores

    left_scores, right_scores = validated_scores(left), validated_scores(right)

    def summary(run, scores):
        return {"run_id": run["id"], "capability_version": run["capability_version"],
                "technical_succeeded": sum(item["task_status"] == "succeeded" for item in scores.values()),
                "evaluation_passed": sum(item["passed"] for item in scores.values())}

    return {"dataset": {key: dataset[key] for key in ("key", "version", "content_hash")},
            "capability_key": left["capability_key"], "case_count": len(case_keys),
            "left": summary(left, left_scores), "right": summary(right, right_scores),
            "cases": [{"case_key": key,
                       "left": {field: left_scores[key][field] for field in ("task_status", "passed")},
                       "right": {field: right_scores[key][field] for field in ("task_status", "passed")}}
                      for key in case_keys]}
