"""Pure contract tests for declarative workflow evaluation."""

import pytest
from pydantic import ValidationError

from domain.task_execution import ExecutionFailure
from domain.workflow import WorkflowBinding, WorkflowDefinition, WorkflowNode, WorkflowRule
from framework.workflow import apply_rule, map_values, resolve_binding


def binding(source="input", **kwargs):
    return WorkflowBinding(source=source, **kwargs)


def executable(key, *, dependencies=(), mapping=None, when=None):
    return WorkflowNode(
        key=key,
        kind="agent",
        capability={"key": "worker", "version": "1"},
        depends_on=list(dependencies),
        input_mapping=mapping or {},
        when=when,
    )


def rule_node(key="check", *, operation="project", **kwargs):
    return WorkflowNode(key=key, kind="rule", rule={"operation": operation}, **kwargs)


def test_workflow_schema_constraints_and_deterministic_ordering():
    source = executable("source")
    child = executable("child", dependencies=["source"], mapping={"x": binding("node", node="source")})
    final = executable("final", dependencies=["child"])
    assert [n.key for n in WorkflowDefinition(nodes=[final, child, source]).ordered_nodes()] == [
        "source", "child", "final"
    ]
    with pytest.raises(ValueError, match="unique"):
        WorkflowDefinition(nodes=[source, source]).ordered_nodes()
    with pytest.raises(ValueError, match="cycle or unknown"):
        WorkflowDefinition(nodes=[executable("a", dependencies=["missing"])]).ordered_nodes()
    with pytest.raises(ValueError, match="cycle or unknown"):
        WorkflowDefinition(nodes=[executable("a", dependencies=["b"]), executable("b", dependencies=["a"])]).ordered_nodes()
    with pytest.raises(ValidationError, match="Duplicate"):
        executable("a", dependencies=["b", "b"])
    with pytest.raises(ValueError, match="explicit dependency ancestor"):
        WorkflowDefinition(nodes=[executable("a", mapping={"x": binding("node", node="b")}), executable("b")]).ordered_nodes()
    with pytest.raises(ValueError, match="unknown node"):
        WorkflowDefinition(nodes=[source], output_mapping={"x": binding("node", node="missing")}).ordered_nodes()


def test_workflow_rule_and_node_caps_and_skip_defaults():
    with pytest.raises(ValidationError):
        WorkflowNode(key="bad", kind="rule", rule={"operation": "project"}, capability={"key": "worker", "version": "1"})
    with pytest.raises(ValidationError):
        WorkflowNode(key="bad", kind="agent", capability={"key": "worker", "version": "1"}, rule={"operation": "project"})
    with pytest.raises(ValidationError):
        WorkflowNode(key="bad", kind="rule", rule={"operation": "project"}, max_attempts=11)
    with pytest.raises(ValidationError):
        WorkflowNode(key="bad", kind="agent", capability={"key": "worker", "version": "1"}, timeout_seconds=0)

    skipped = binding("node", node="not_run", pointer="/value", required=False, default={"safe": [1]})
    assert resolve_binding(skipped, {}, {}) == {"safe": [1]}
    assert WorkflowDefinition(nodes=[rule_node()], output_mapping={"result": binding("node", node="check")}).ordered_nodes()


def test_pointer_escapes_arrays_and_root():
    data = {"a/b": {"~key": [{"v": 7}]}}
    assert resolve_binding(binding(pointer="/a~1b/~0key/0/v"), data, {}) == 7
    assert resolve_binding(binding(pointer=""), data, {}) == data
    for pointer in ("x", "/bad~", "/bad~2"):
        with pytest.raises((ValidationError, ExecutionFailure)):
            b = binding(pointer=pointer)
            resolve_binding(b, data, {})
    for token in ("-", "01", "+1", "١"):
        with pytest.raises(ExecutionFailure, match="WORKFLOW_MAPPING_MISSING"):
            resolve_binding(binding(pointer=f"/items/{token}"), {"items": ["x"]}, {})


def test_mapping_missing_and_default_semantics():
    for source, kwargs, inputs, outputs in [
        ("input", {"pointer": "/absent"}, {}, {}),
        ("node", {"node": "skipped"}, {}, {}),
        ("node", {"node": "done", "pointer": "/absent"}, {}, {"done": {}}),
        ("input", {"pointer": "/arr/1"}, {"arr": [1]}, {}),
    ]:
        with pytest.raises(ExecutionFailure, match="WORKFLOW_MAPPING_MISSING"):
            resolve_binding(binding(source, **kwargs), inputs, outputs)
        fallback = binding(source, **kwargs, required=False, default="fallback")
        assert resolve_binding(fallback, inputs, outputs) == "fallback"
    assert map_values({"a": binding("literal", value=1)}, {}, {}) == {"a": 1}


def test_typed_json_equality_and_assert_contract():
    equals = WorkflowRule(operation="equals")
    assert apply_rule(equals, {"left": True, "right": 1}) == {"value": False}
    assert apply_rule(equals, {"left": {"b": 2, "a": 1}, "right": {"a": 1, "b": 2}}) == {"value": True}
    assertion = WorkflowRule(operation="assert")
    assert apply_rule(assertion, {"condition": True}) == {}
    assert apply_rule(assertion, {"condition": True, "output": {"x": 1}}) == {"x": 1}
    with pytest.raises(ExecutionFailure, match="WORKFLOW_ASSERTION_FAILED"):
        apply_rule(assertion, {"condition": False})
    for values in ({}, {"condition": 1}, {"condition": "yes"}, {"condition": True, "output": []}, {"condition": True, "extra": 1}):
        with pytest.raises(ExecutionFailure, match="WORKFLOW_RULE_INPUT_INVALID"):
            apply_rule(assertion, values)
    for values in ({"left": 1}, {"left": 1, "right": 1, "extra": 0}):
        with pytest.raises(ExecutionFailure, match="WORKFLOW_RULE_INPUT_INVALID"):
            apply_rule(equals, values)


def test_outputs_are_bounded_copied_and_hostile_literals_are_inert():
    original = {"nested": [1]}
    literal = binding("literal", value=original)
    result = resolve_binding(literal, {}, {})
    result["nested"].append(2)
    assert original == {"nested": [1]}

    task_input = {"payload": {"x": [1]}}
    mapped = map_values({"payload": binding(pointer="/payload")}, task_input, {})
    mapped["payload"]["x"].append(2)
    assert task_input == {"payload": {"x": [1]}}
    outputs = {"prior": {"x": [1]}}
    projected = apply_rule(WorkflowRule(operation="project"), outputs["prior"])
    projected["x"].append(2)
    assert outputs == {"prior": {"x": [1]}}

    hostile = "__import__('os').system('touch /tmp/workflow-owned')"
    assert resolve_binding(binding("literal", value=hostile), {}, {}) == hostile
    too_deep = value = []
    for _ in range(66):
        value.append([])
        value = value[0]
    with pytest.raises(ExecutionFailure, match="WORKFLOW_RULE_INPUT_INVALID"):
        resolve_binding(binding("literal", value=too_deep), {}, {})
    with pytest.raises(ValidationError):
        binding("literal", value=float("inf"))
