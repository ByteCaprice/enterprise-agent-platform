"""Offline smoke tests for immutable workflow plans."""

import pytest

from domain.workflow import WorkflowBinding, WorkflowDefinition, WorkflowNode
from framework.workflow_plan import WorkflowPlanError, compile_workflow


def executable(key, *, depends_on=(), mapping=None):
    return WorkflowNode(
        key=key,
        kind="agent",
        capability={"key": "worker", "version": "1"},
        depends_on=list(depends_on),
        input_mapping=mapping or {},
    )


def rule(key, *, depends_on=()):
    return WorkflowNode(key=key, kind="rule", rule={"operation": "project"}, depends_on=list(depends_on))


def test_plan_snapshots_and_materializations_are_isolated_and_repr_safe():
    nested = {"items": [1]}
    literal = WorkflowBinding(source="literal", value=nested)
    definition = WorkflowDefinition(nodes=[rule("check")], output_mapping={"result": literal})
    plan = compile_workflow(definition)

    nested["items"].append(2)
    definition.nodes[0].rule.operation = "equals"
    first = plan.nodes[0].materialize()
    first.rule.operation = "assert"
    assert plan.nodes[0].materialize().rule.operation == "project"
    output = plan.output_mapping()
    output["result"].value["items"].append(3)
    assert plan.output_mapping()["result"].value == {"items": [1]}
    assert "project" not in repr(plan.nodes[0])
    assert "items" not in repr(plan)


def test_ready_batches_preserve_rule_overlap_and_external_serialization():
    definition = WorkflowDefinition(
        nodes=[executable("adapter"), rule("r1"), executable("other"), rule("r2"), executable("after", depends_on=["r1", "r2"])],
        max_parallel_rules=2,
    )
    plan = compile_workflow(definition)
    assert plan.next_batch(frozenset()) == (plan.nodes[0],)
    after_adapter = frozenset({"adapter"})
    assert tuple(node.key for node in plan.next_batch(after_adapter)) == ("r1", "r2")
    after_rules = after_adapter | {"r1", "r2"}
    assert tuple(node.key for node in plan.next_batch(after_rules)) == ("other",)
    assert tuple(node.key for node in plan.next_batch(after_rules | {"other"})) == ("after",)
    assert plan.next_batch(after_rules | {"other", "after"}) == ()


def test_invalid_progress_and_retry_are_classified_without_definition_leakage():
    plan = compile_workflow(WorkflowDefinition(nodes=[executable("first"), executable("second", depends_on=["first"])]))
    for completed in (frozenset({"second"}), frozenset({"unknown"})):
        with pytest.raises(WorkflowPlanError) as error:
            plan.next_batch(completed)
        assert error.value.code == "WORKFLOW_PROGRESS_INVALID"
        assert "unknown" not in str(error.value)

    retrying = WorkflowDefinition(nodes=[executable("sensitive")])
    retrying.nodes[0].max_attempts = 2
    with pytest.raises(WorkflowPlanError) as error:
        compile_workflow(retrying)
    assert error.value.code == "WORKFLOW_RETRY_NOT_SUPPORTED"
    assert "sensitive" not in str(error.value)
