"""Definition semantics plus bounded schema inspection, without storage locks."""

from domain.capability import (
    CapabilityError,
    CapabilityKind,
    CapabilityValidationUnavailable,
    CapabilityVersionDraft,
)
from domain.capability_definitions import DefinitionInterpretation, interpret_definition
from domain.schema_validation import (
    JSONSchemaInspector,
    JSONValidationError,
    JSONValidationUnavailable,
    ensure_self_contained_schema,
)


def require_local_schema(schema) -> None:
    try:
        ensure_self_contained_schema(schema)
    except JSONValidationError:
        raise CapabilityError("Capability schemas require bounded local references") from None


class CapabilityValidator:
    def __init__(self, schemas: JSONSchemaInspector):
        self.schemas = schemas

    def validate(self, kind: CapabilityKind | str, artifact: CapabilityVersionDraft) -> DefinitionInterpretation:
        artifact.fingerprint()
        for schema in (artifact.input_schema, artifact.output_schema):
            require_local_schema(schema)
            try:
                self.schemas.check_schema(schema)
            except JSONValidationUnavailable as exc:
                raise CapabilityValidationUnavailable(exc.code) from None
            except JSONValidationError:
                raise CapabilityError("Invalid capability JSON Schema") from None
        if artifact.input_schema.get("type") != "object":
            raise CapabilityError("Capability input schema must describe an object")
        return interpret_definition(kind, artifact)

    def require_empty_variables(self, schema) -> None:
        require_local_schema(schema)
        try:
            self.schemas.validate({}, schema)
        except JSONValidationUnavailable as exc:
            raise CapabilityValidationUnavailable(exc.code) from None
        except JSONValidationError:
            raise CapabilityError("Agent instruction Prompt must accept an empty variable object") from None
