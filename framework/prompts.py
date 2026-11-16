"""Bounded deterministic text templating; no catalog, authorization or model I/O."""

import json
from string import Template

from domain.capability import CapabilityError
from domain.capability_definitions import validate_prompt_template as validate_template

__all__ = ["render_template", "validate_template"]


def render_template(text: str, variables: dict) -> str:
    template = Template(text)
    if not template.is_valid():
        raise CapabilityError("Prompt uses invalid template syntax; use ${variable}")
    try:
        variables_json = json.dumps(variables, ensure_ascii=False, allow_nan=False)
        if len(variables_json.encode("utf-8")) > 1_000_000:
            raise CapabilityError("Prompt variables exceed the size limit")
        values = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
                  for key, value in variables.items()}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CapabilityError("Prompt variables must contain finite, valid JSON") from None
    try:
        rendered = template.substitute(values)
    except KeyError:
        raise CapabilityError("A required prompt template variable is missing") from None
    if len(rendered.encode("utf-8")) > 1_000_000:
        raise CapabilityError("Rendered prompt exceeds the size limit")
    return rendered
