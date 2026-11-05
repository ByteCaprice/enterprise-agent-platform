"""Environment-backed credentials for the independent platform model transport."""

from __future__ import annotations

import hashlib
import json
import os
import re

from pydantic import TypeAdapter, ValidationError

from domain.capability import Key
from domain.platform_model import ModelSecret

_KEY = TypeAdapter(Key)
_PREFIX = "ENTERPRISE_AI_CREDENTIAL_"


def _validated_key(value: str) -> str:
    try:
        return _KEY.validate_python(value)
    except (ValidationError, TypeError):
        raise ValueError("Invalid model credential key") from None


def credential_variable_name(project_key: str, reference: str) -> str:
    project = _validated_key(project_key)
    ref = _validated_key(reference)
    canonical = json.dumps([project, ref], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest().upper()
    return _PREFIX + digest


class EnvironmentCredentialResolver:
    def resolve(self, project_key: str, reference: str) -> ModelSecret:
        try:
            variable = credential_variable_name(project_key, reference)
            value = os.environ.get(variable)
            if value is None or not value or len(value) > 16384:
                raise ValueError
            value.encode("ascii")
            if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
                raise ValueError
            if re.fullmatch(r"[!-~]+", value) is None:
                raise ValueError
            return ModelSecret(reference=reference, value=value)
        except (ValueError, UnicodeError):
            from domain.platform_model import ModelTransportFailure

            raise ModelTransportFailure("MODEL_CREDENTIAL_UNAVAILABLE") from None
