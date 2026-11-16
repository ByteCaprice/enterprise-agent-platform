"""Offline smoke tests for the isolated JSON Schema validator."""

import asyncio
import subprocess
import sys
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domain.capability import CapabilityError, CapabilityValidationUnavailable
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from domain.task import TaskError, TaskValidationUnavailable
from domain.task_execution import CompletionOutcome
from domain.task_worker import WorkerServices
from infra import schema_validation as validator
from interfaces.http.routes import capabilities, platform_operations, tasks
from orchestration.platform_worker import PlatformWorker


def test_real_worker_local_refs_and_safe_rejections(monkeypatch, caplog):
    real_run = subprocess.run
    calls = []

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        calls.append((args, kwargs, result))
        return result

    monkeypatch.setattr(validator.subprocess, "run", run)
    schema = {"$defs": {"private_schema_marker": {"type": "integer"}},
              "$ref": "#/$defs/private_schema_marker"}
    validator.check_json_schema(schema)
    validator.validate_json(42, schema)
    for instance, bad_schema, expected in [
        ("private_data_marker", schema, "INSTANCE_INVALID"),
        (1, {"type": "not-a-type"}, "SCHEMA_INVALID"),
        (1, {"$schema": "http://json-schema.org/draft-07/schema#"}, "SCHEMA_INVALID"),
        (1, {"$ref": "https://example.invalid/private_ref"}, "REFERENCE_UNSUPPORTED"),
    ]:
        with pytest.raises(JSONValidationError) as error:
            validator.validate_json(instance, bad_schema)
        assert str(error.value) == expected
    assert len(calls) == 4  # remote references are rejected before spawning
    validator.subprocess.run(
        [sys.executable, "-I", str(validator._WORKER)],
        input=b'{"schema":{"$ref":"https://example.invalid/private_ref"},"instance":1,"mode":"validate"}',
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, timeout=3, check=False,
    )
    assert [call[2].stdout for call in calls] == [b"valid", b"valid", b"instance", b"schema", b"reference"]
    for args, kwargs, result in calls:
        assert args[0] == [sys.executable, "-I", str(validator._WORKER)]
        assert kwargs["env"] == {} and kwargs["stderr"] == subprocess.DEVNULL
        assert result.returncode == 0
        assert b"private" not in result.stdout
    assert "private" not in caplog.text
    # A $ref-shaped object inside const is literal instance data, not a reference.
    validator.validate_json({"$ref": "https://example.invalid/value"},
                            {"const": {"$ref": "https://example.invalid/value"}})
    validator.validate_json({"$ref": "literal"}, {"properties": {"$ref": {"type": "string"}}})
    with pytest.raises(JSONValidationError, match="^REFERENCE_UNSUPPORTED$"):
        validator.check_json_schema({"const": {"$ref": "https://example.invalid/value"}, "$ref": "#/const"})


def test_bounds_fail_before_spawning(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid JSON must not spawn a worker")

    monkeypatch.setattr(validator.subprocess, "run", forbidden)
    too_deep = value = []
    for _ in range(65):
        child = []
        value.append(child)
        value = child
    for instance, schema in [("x" * 1_000_000, {}), (None, {"description": "x" * 1_000_000}),
                             (float("nan"), {}), (too_deep, {})]:
        with pytest.raises(JSONValidationError, match="^JSON_INVALID$"):
            validator.validate_json(instance, schema)


def test_capacity_rejection_releases_no_unowned_slot(monkeypatch):
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(validator, "_SLOTS", slots)
    assert slots.acquire(blocking=False)
    try:
        with pytest.raises(JSONValidationUnavailable, match="^VALIDATION_CAPACITY$"):
            validator.check_json_schema({}, timeout_seconds=0.03)
    finally:
        slots.release()
    validator.check_json_schema({})  # capacity is usable after rejection


@pytest.mark.parametrize("queue_seconds", [0.2, 0.31])
def test_queue_wait_is_part_of_deadline_even_before_spawn(monkeypatch, queue_seconds):
    clock, released, calls = [100.0], [], []

    def acquire(*, timeout):
        assert timeout == pytest.approx(0.3)
        clock[0] += queue_seconds
        return True

    def run(command, *, timeout, **kwargs):
        calls.append(timeout)
        assert timeout == pytest.approx(0.1)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(validator, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(validator, "_SLOTS", SimpleNamespace(acquire=acquire, release=lambda: released.append(True)))
    monkeypatch.setattr(validator.subprocess, "run", run)
    with pytest.raises(JSONValidationUnavailable, match="^VALIDATION_TIMEOUT$"):
        validator.check_json_schema({}, timeout_seconds=0.3)
    assert len(calls) == (1 if queue_seconds < 0.3 else 0)
    assert released == [True]


def test_started_worker_timeout_kills_and_reaps_child(monkeypatch, tmp_path):
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(validator, "_SLOTS", slots)
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    monkeypatch.setattr(validator, "_WORKER", worker)
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(validator.subprocess, "Popen", popen)
    with pytest.raises(JSONValidationUnavailable, match="^VALIDATION_TIMEOUT$"):
        validator.check_json_schema({}, timeout_seconds=1)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].wait(timeout=0.1) is not None
    assert slots.acquire(blocking=False)
    slots.release()


def test_http_unavailability_is_503_not_invalid_422():
    def fail(error):
        raise error

    for helper, unavailable, invalid in [
        (tasks._call, TaskValidationUnavailable("VALIDATION_TIMEOUT"), TaskError("private")),
        (capabilities._call, CapabilityValidationUnavailable("VALIDATION_CAPACITY"), CapabilityError("invalid schema")),
        (platform_operations.call, TaskValidationUnavailable("VALIDATOR_UNAVAILABLE"), TaskError("private")),
        (platform_operations.call, CapabilityValidationUnavailable("VALIDATION_TIMEOUT"), ValueError("private")),
    ]:
        with pytest.raises(HTTPException) as error:
            helper(fail, unavailable)
        assert error.value.status_code == 503
        assert error.value.headers == {"Retry-After": "1"}
        assert "VALIDATION_" not in str(error.value.detail)
        with pytest.raises(HTTPException) as error:
            helper(fail, invalid)
        assert error.value.status_code == 422


def test_final_schema_unavailability_settles_failure_without_reexecuting():
    from worker_support import worker_capabilities

    calls = []

    def complete(lease, **kwargs):
        calls.append((lease, kwargs))
        if kwargs["status"] == "succeeded":
            raise TaskValidationUnavailable("VALIDATION_CAPACITY")
        return CompletionOutcome.COMPLETED

    async def executor(claim) -> dict:
        raise AssertionError("Finalization must never repeat execution")

    services = WorkerServices(
        queue=SimpleNamespace(), control=SimpleNamespace(), completion=SimpleNamespace(complete=complete),
        authorization=SimpleNamespace(allowed=lambda _: True), validation=validator.IsolatedJSONValidator(),
    )
    worker = PlatformWorker(services, "worker", {"workflow": executor},
                            capabilities=worker_capabilities("workflow"))
    claim = SimpleNamespace(lease="same-live-fence")
    asyncio.run(worker._finalize(claim, None, status="succeeded", result={"result": "private"}))
    assert [call[0] for call in calls] == ["same-live-fence", "same-live-fence"]
    assert calls[0][1]["status"] == "succeeded"
    assert calls[1][1] == {"status": "failed", "error_code": "TASK_VALIDATION_UNAVAILABLE"}
