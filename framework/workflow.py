"""Pure, deterministic evaluation helpers for declarative workflows."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from domain.task import TaskError, canonical_json
from domain.task_execution import ExecutionFailure
from domain.workflow import WorkflowBinding, WorkflowRule


def _validate_json(value: Any) -> None:
    try:
        canonical_json(value)
    except TaskError:
        raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID") from None


def _pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
    tokens = []
    for token in pointer[1:].split("/"):
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 == len(token) or token[index + 1] not in "01":
                    raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
                index += 2
            else:
                index += 1
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens


def _resolve_pointer(value: Any, pointer: str) -> Any:
    for token in _pointer_tokens(pointer):
        if isinstance(value, dict):
            if token not in value:
                raise KeyError
            value = value[token]
        elif isinstance(value, list):
            if token == "0":
                index = 0
            elif token.isascii() and token.isdecimal() and token[0] != "0":
                index = int(token)
            else:
                raise KeyError
            if index >= len(value):
                raise KeyError
            value = value[index]
        else:
            raise KeyError
    return value


def resolve_binding(binding: WorkflowBinding, task_input: dict, outputs: dict[str, dict]) -> Any:
    """Resolve a JSON-only input, node-output, or literal binding."""
    if binding.source == "literal":
        value = binding.value
    else:
        try:
            if binding.source == "input":
                source = task_input
            elif binding.node is not None:
                source = outputs[binding.node]
            else:
                raise KeyError
            value = _resolve_pointer(source, binding.pointer)
        except (KeyError, TypeError):
            if not binding.required:
                value = binding.default
            else:
                raise ExecutionFailure("WORKFLOW_MAPPING_MISSING") from None
    _validate_json(value)
    return deepcopy(value)


def map_values(mapping: dict[str, WorkflowBinding], task_input: dict, outputs: dict[str, dict]) -> dict:
    """Resolve a mapping into a bounded JSON object without sharing mutable values."""
    result = {key: resolve_binding(binding, task_input, outputs) for key, binding in mapping.items()}
    _validate_json(result)
    return result


def apply_rule(rule: WorkflowRule, values: dict) -> dict:
    """Evaluate one supported deterministic rule operation."""
    _validate_json(values)
    operation = rule.operation
    if operation == "project":
        return deepcopy(values)
    if operation == "equals":
        if set(values) != {"left", "right"}:
            raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
        result = {"value": canonical_json(values["left"]) == canonical_json(values["right"])}
        _validate_json(result)
        return result
    if operation == "assert":
        if set(values) not in ({"condition"}, {"condition", "output"}):
            raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
        condition = values["condition"]
        if type(condition) is not bool:
            raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
        output = values.get("output", {})
        if not isinstance(output, dict):
            raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
        if not condition:
            raise ExecutionFailure("WORKFLOW_ASSERTION_FAILED")
        result = deepcopy(output)
        _validate_json(result)
        return result
    raise ExecutionFailure("WORKFLOW_RULE_INPUT_INVALID")
