"""Bounded, disposable-process validation of self-contained JSON Schema documents."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path
from threading import BoundedSemaphore

from domain.schema_validation import JSONValidationError, JSONValidationUnavailable, ensure_self_contained_schema
from domain.task import TaskError, canonical_json

# Process-local CPU admission, not tenant admission or a global deployment limit.
_SLOTS = BoundedSemaphore(4)
_WORKER = Path(__file__).with_name("_schema_worker.py")


def _run(schema, instance, mode: str, timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or (
        not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
    ):
        raise ValueError("Validation timeout must be a positive finite bounded number")
    deadline = time.monotonic() + timeout_seconds
    try:
        schema_json = canonical_json(schema)
        instance_json = canonical_json(instance)
        body = f'{{"schema":{schema_json},"instance":{instance_json},"mode":"{mode}"}}'.encode()
    except TaskError:
        raise JSONValidationError("JSON_INVALID") from None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JSONValidationUnavailable("VALIDATION_TIMEOUT")
    if not _SLOTS.acquire(timeout=remaining):
        raise JSONValidationUnavailable("VALIDATION_CAPACITY")
    try:
        # Inspect the serialized snapshot, not a caller-held mutable dictionary.
        ensure_self_contained_schema(json.loads(schema_json))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JSONValidationUnavailable("VALIDATION_TIMEOUT")
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(_WORKER)], input=body,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={},
                timeout=remaining, check=False,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps this single-purpose child. No
            # application call or external effect runs inside it.
            raise JSONValidationUnavailable("VALIDATION_TIMEOUT") from None
        except OSError:
            raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE") from None
        if result.returncode != 0:
            raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE")
        if result.stdout == b"valid":
            return
        if result.stdout == b"json":
            raise JSONValidationError("JSON_INVALID")
        if result.stdout == b"schema":
            raise JSONValidationError("SCHEMA_INVALID")
        if result.stdout == b"instance":
            raise JSONValidationError("INSTANCE_INVALID")
        if result.stdout == b"reference":
            raise JSONValidationError("REFERENCE_UNSUPPORTED")
        raise JSONValidationUnavailable("VALIDATOR_UNAVAILABLE")
    finally:
        _SLOTS.release()


def check_json_schema(schema, *, timeout_seconds: float = 3) -> None:
    _run(schema, None, "schema", timeout_seconds)


def validate_json(instance, schema, *, timeout_seconds: float = 3) -> None:
    _run(schema, instance, "validate", timeout_seconds)


class IsolatedJSONValidator:
    """Installed implementation of the execution plane's validation port."""

    def validate(self, instance, schema) -> None:
        validate_json(instance, schema)

    def check_schema(self, schema) -> None:
        check_json_schema(schema)


def check_validation_backend() -> None:
    """Startup-only local handshake; never a Provider, model, Tool or DB call."""
    validate_json({}, {"type": "object", "maxProperties": 0})
