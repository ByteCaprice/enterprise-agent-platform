"""Safe schema rejection versus bounded validator infrastructure failure."""

from typing import Any, Literal, Protocol
from urllib.parse import unquote


class JSONSchemaValidator(Protocol):
    """Bounded matching port; callers must distinguish invalid from unavailable."""

    def validate(self, instance: Any, schema: Any) -> None: ...


class JSONSchemaInspector(JSONSchemaValidator, Protocol):
    """Authoring needs schema checking as well as bounded instance matching."""

    def check_schema(self, schema: Any) -> None: ...


class JSONValidationError(ValueError):
    def __init__(self, code: Literal["JSON_INVALID", "SCHEMA_INVALID", "INSTANCE_INVALID", "REFERENCE_UNSUPPORTED"]):
        if code not in {"JSON_INVALID", "SCHEMA_INVALID", "INSTANCE_INVALID", "REFERENCE_UNSUPPORTED"}:
            raise ValueError("Invalid JSON validation error code")
        self.code = code
        super().__init__(code)


def ensure_self_contained_schema(schema) -> None:
    """Inspect schema positions, not literal const/default/example/enum data.

    Local pointers may activate otherwise literal data as a schema. Follow them
    under their resource root, with a bounded closure; the runtime independently
    denies ALL resource retrieval in the isolated worker.
    """
    pending: list[tuple[Any, Any]] = [(schema, schema)]
    seen = set()
    reference_bytes = 0
    maps = {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    singles = {"additionalProperties", "unevaluatedProperties", "propertyNames", "contains",
               "not", "if", "then", "else", "items", "additionalItems", "unevaluatedItems", "contentSchema"}
    while pending:
        node, root = pending.pop()
        if not isinstance(node, dict):
            continue
        if "$schema" in node and (not isinstance(node["$schema"], str) or node["$schema"] not in {
            "https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#",
        }):
            raise JSONValidationError("SCHEMA_INVALID")
        if isinstance(node.get("$id"), str):
            root = node
        identity = (id(node), id(root))
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > 10000:
            raise JSONValidationError("SCHEMA_INVALID")
        for key, child in node.items():
            children = []
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(child, str) or not child.startswith("#"):
                    raise JSONValidationError("REFERENCE_UNSUPPORTED")
                reference_bytes += len(child.encode())
                if reference_bytes > 4_000_000:
                    raise JSONValidationError("SCHEMA_INVALID")
                fragment = unquote(child[1:])
                if not fragment or fragment.startswith("/"):
                    target = root
                    parts = fragment.split("/")[1:] if fragment else []
                    if len(parts) <= 64:
                        for part in parts:
                            part = part.replace("~1", "/").replace("~0", "~")
                            if isinstance(target, dict):
                                target = target.get(part)
                            elif isinstance(target, list) and part.isascii() and part.isdigit() and len(part) <= 10:
                                index = int(part)
                                target = target[index] if index < len(target) else None
                            else:
                                target = None
                                break
                        children = [target]
            elif key in maps and isinstance(child, dict):
                children = list(child.values())
            elif key in arrays and isinstance(child, list):
                children = child
            elif key in singles:
                children = child if isinstance(child, list) else [child]
            elif key == "dependencies" and isinstance(child, dict):
                children = [value for value in child.values() if isinstance(value, (dict, bool))]
            if len(pending) + len(children) > 10000:
                raise JSONValidationError("SCHEMA_INVALID")
            pending.extend((value, root) for value in children)


class JSONValidationUnavailable(RuntimeError):
    def __init__(self, code: Literal["VALIDATION_TIMEOUT", "VALIDATION_CAPACITY", "VALIDATOR_UNAVAILABLE"]):
        if code not in {"VALIDATION_TIMEOUT", "VALIDATION_CAPACITY", "VALIDATOR_UNAVAILABLE"}:
            raise ValueError("Invalid JSON validation availability code")
        self.code = code
        super().__init__(code)
