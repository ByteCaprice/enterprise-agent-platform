"""Render a pinned published Prompt without depending on authoring/publication."""

import hashlib

from domain.capability import (
    CapabilityDependency,
    CapabilityError,
    CapabilityKind,
    CapabilityReader,
    CapabilityValidationUnavailable,
    PromptDefinition,
)
from domain.schema_validation import JSONValidationError, JSONValidationUnavailable
from framework.prompts import render_template
from infra.schema_validation import validate_json
from orchestration.published_capabilities import PublishedCapabilities


class PromptRenderer:
    """HTTP and execution use the same rendering path; each owns its authorization."""

    def __init__(self, source: CapabilityReader):
        self.catalog = PublishedCapabilities(source)

    def render(self, project_key: str, key: str, version: str, variables: dict):
        artifact = self.catalog.resolve(project_key, CapabilityDependency(key=key, version=version), CapabilityKind.PROMPT)
        try:
            validate_json(variables, artifact.input_schema)
        except JSONValidationUnavailable as exc:
            raise CapabilityValidationUnavailable(exc.code) from None
        except JSONValidationError:
            raise CapabilityError("Prompt variables do not match the published schema") from None
        rendered = render_template(PromptDefinition.model_validate(artifact.definition).template, variables)
        return {"text": rendered, "version": artifact.version, "content_hash": artifact.content_hash,
                "rendered_hash": hashlib.sha256(rendered.encode()).hexdigest()}
