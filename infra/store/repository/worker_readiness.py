"""Bounded queue coverage, based on advisory (never claim-authorizing) evidence."""

import hashlib
import json
import re

from sqlalchemy import select

from infra.store.model.ai_task import AiTask
from infra.store.repository.task_queue import read_task_requirements

QUEUE_SCAN_LIMIT = 32
ARTIFACT_LOOKUP_LIMIT = 128
_MANIFEST_KEY = "execution_manifest_v1"
_MANIFEST_ITEM_LIMIT = 128
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_EXECUTOR_KINDS = ("agent", "workflow")


def _fingerprints(capabilities):
    """Project installed and required capabilities to identical, role-scoped atoms."""
    def fingerprint(*parts):
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("ascii")).hexdigest()

    result = {fingerprint("profile", p.component, p.key, p.profile) for p in capabilities.profiles}
    for role, references in (("workflow", capabilities.workflow_implementations),
                              ("tool", capabilities.tool_implementations)):
        result.update(fingerprint(role, ref.key, ref.version, ref.digest) for ref in references)
    return result


def heartbeat_executors(executors, capabilities):
    # The existing JSON array starts with the legacy executor names. The
    # versioned tail is internal, bounded, and contains no implementation names
    # or parameters. Oversized installations advertise only legacy coverage.
    payload = list(executors)
    if capabilities is not None:
        if frozenset(payload) != capabilities.executor_kinds:
            raise ValueError("Worker heartbeat executors do not match installed capabilities")
        size = len(capabilities.profiles) + len(capabilities.workflow_implementations) + len(capabilities.tool_implementations)
        if size <= _MANIFEST_ITEM_LIMIT:
            payload.append({_MANIFEST_KEY: sorted(_fingerprints(capabilities))})
    return payload


def executor_names(payload):
    if not isinstance(payload, list):
        return []
    return [item for item in payload[:len(_EXECUTOR_KINDS)] if isinstance(item, str) and item in _EXECUTOR_KINDS]


def _manifest(payload):
    names = executor_names(payload)
    if not names or len(set(names)) != len(names) or len(payload) != len(names) + 1:
        return None
    tail = payload[-1]
    if not isinstance(tail, dict) or len(tail) != 1:
        return None
    items = tail.get(_MANIFEST_KEY)
    if (not isinstance(items, list) or len(items) > _MANIFEST_ITEM_LIMIT or
            any(not isinstance(item, str) or len(item) != 64 or not _FINGERPRINT.fullmatch(item) for item in items)):
        return None
    return frozenset(items) if len(set(items)) == len(items) else None


class _LookupLimitReached(RuntimeError):
    pass


def _queued_sample(session, project):
    return session.execute(select(AiTask.id, AiTask.project_key, AiTask.capability_id, AiTask.version, AiTask.content_hash)
        .where(AiTask.project_key == project, AiTask.status == "queued")
        .order_by(AiTask.create_time, AiTask.id).limit(QUEUE_SCAN_LIMIT + 1)).all()


def queued_readiness(session, project, *, enabled, workers, workers_truncated):
    """Classify sampled tasks against individual live Task Worker observations."""
    tasks = _queued_sample(session, project) if enabled else []
    if not tasks:
        return {"scan_limit": QUEUE_SCAN_LIMIT, "scanned": 0, "queue_truncated": False,
                "artifact_lookup_limit_reached": False, "compatible": 0, "issues": []}

    manifests = {name: set() for name in _EXECUTOR_KINDS}
    unknown_kinds = set()
    for worker in workers:
        if worker["role"] != "task":
            continue
        payload = worker["executors"]
        observed = _manifest(payload)
        names = executor_names(payload)
        if observed is None:
            unknown_kinds.update(names or _EXECUTOR_KINDS)
        else:
            for kind in names:
                manifests[kind].add(observed)

    lookups = 0

    def count_lookup():
        nonlocal lookups
        if lookups >= ARTIFACT_LOOKUP_LIMIT:
            raise _LookupLimitReached
        lookups += 1

    inspected = {}
    issues = []
    compatible = 0
    lookup_limit_reached = False
    for task in tasks[:QUEUE_SCAN_LIMIT]:
        pin = (task.capability_id, task.version, task.content_hash)
        if pin not in inspected:
            try:
                result = read_task_requirements(session, task, before_load=count_lookup)
            except _LookupLimitReached:
                outcome = "inspection_limit"
                lookup_limit_reached = True
            else:
                if result is None:
                    outcome = "requirements_unavailable"
                else:
                    requirements = result[1]
                    required = _fingerprints(requirements)
                    if any(required <= manifest for manifest in manifests[requirements.kind]):
                        outcome = "compatible"
                    elif workers_truncated or requirements.kind in unknown_kinds:
                        outcome = "worker_coverage_unknown"
                    else:
                        outcome = "no_compatible_worker"
            inspected[pin] = outcome
        outcome = inspected[pin]
        if outcome == "compatible":
            compatible += 1
        else:
            issues.append({"task_id": task.id, "reason": outcome})
    return {"scan_limit": QUEUE_SCAN_LIMIT, "scanned": min(len(tasks), QUEUE_SCAN_LIMIT),
            "queue_truncated": len(tasks) > QUEUE_SCAN_LIMIT, "artifact_lookup_limit_reached": lookup_limit_reached,
            "compatible": compatible, "issues": issues}
