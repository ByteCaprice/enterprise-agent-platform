"""Offline pinned-closure inspection, independent of task-input size limits."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from domain.capability import CapabilityVersionDraft, CapabilityVersionRecord
from domain.execution_requirements import (
    TOOL_INTERPRETER_PROFILE,
    WORKFLOW_ADAPTER_PROFILES,
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    ExecutionArtifact,
    ExecutionProfile,
    ExecutionRequirementsUnavailable,
    InstalledExecutionCapabilities,
    required_execution_profiles,
)
from domain.task import TaskError, canonical_json
from domain.workflow_runtime import WorkflowAdapterDescriptor
from infra.schema_validation import IsolatedJSONValidator
from orchestration.capability_validation import CapabilityValidator


def published(key, kind, definition, *, schema_text="", dependencies=()):
    draft = CapabilityVersionDraft(
        version="1", definition=definition, dependencies=list(dependencies),
        input_schema={"type": "object", "description": schema_text},
        output_schema={"type": "object", "description": schema_text},
    )
    record = CapabilityVersionRecord(
        **draft.model_dump(), id=f"{key}-1", capability_id=key,
        status="published", content_hash=draft.fingerprint(),
        created_by="test", create_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return ExecutionArtifact(key, key, kind, record)


def workflow(tool_keys=(), *, schema_text=""):
    nodes = [
        {"key": f"node-{index}", "kind": "tool", "capability": {"key": key, "version": "1"}}
        for index, key in enumerate(tool_keys)
    ] or [{"key": "project", "kind": "rule", "rule": {"operation": "project"}}]
    return published("flow", "workflow", {"nodes": nodes}, schema_text=schema_text,
                     dependencies=[{"key": key, "version": "1"} for key in dict.fromkeys(tool_keys)])


def tool(key="lookup", *, schema_text=""):
    return published(key, "tool", {"binding_key": "lookup", "operation_type": "read_only"}, schema_text=schema_text)


def inspect(root, targets=()):
    pinned = {(target.key, target.artifact.version): target for target in targets}
    return required_execution_profiles(
        root, version=root.artifact.version, content_hash=root.artifact.content_hash,
        resolve=lambda key, version: pinned.get((key, version)),
    )


def test_large_valid_artifact_is_not_subject_to_the_task_input_limit():
    root = workflow(schema_text="x" * 600_000)
    CapabilityValidator(IsolatedJSONValidator()).validate(root.kind, root.artifact)
    assert len(canonical_json(root.artifact.input_schema).encode("utf-8")) < 1_000_000
    assert len(canonical_json(root.artifact.output_schema).encode("utf-8")) < 1_000_000
    with pytest.raises(TaskError):
        canonical_json(root.artifact.model_dump(mode="json", include=set(CapabilityVersionDraft.model_fields)))

    requirements = inspect(root)

    assert requirements.kind == "workflow"
    assert requirements.profiles == frozenset({
        ExecutionProfile("interpreter", "workflow", WORKFLOW_INTERPRETER_PROFILE),
        ExecutionProfile("checkpoint", "workflow", WORKFLOW_CHECKPOINT_PROFILE),
    })


@pytest.mark.parametrize("tool_count,within_bound", [(6, True), (7, False)])
def test_closure_byte_bound_counts_utf8_and_each_pinned_artifact_once(tool_count, within_bound):
    keys = [f"tool-{index}" for index in range(tool_count)]
    root = workflow([*keys, *keys])
    # Each artifact is ~1.2 MB in UTF-8, despite having only ~400K characters.
    targets = [tool(key, schema_text="界" * 200_000) for key in keys]
    if within_bound:
        assert inspect(root, targets).kind == "workflow"
    else:
        with pytest.raises(ExecutionRequirementsUnavailable, match="exceeds inspection bounds"):
            inspect(root, targets)


@pytest.mark.parametrize("failure", ["missing", "key", "version", "kind", "unpublished", "hash"])
def test_missing_or_changed_pins_are_unavailable(failure):
    root = workflow(["lookup"])
    target = tool()
    if failure == "missing":
        target = None
    elif failure == "key":
        target = replace(target, key="different")
    elif failure == "version":
        target.artifact.version = "2"
        target.artifact.content_hash = target.artifact.fingerprint()
    elif failure == "kind":
        target = replace(target, kind="prompt")
    elif failure == "unpublished":
        target.artifact.status = "draft"
    elif failure == "hash":
        target.artifact.definition["binding_key"] = "changed"

    with pytest.raises(ExecutionRequirementsUnavailable):
        required_execution_profiles(root, version="1", content_hash=root.artifact.content_hash,
                                    resolve=lambda *_: target)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "\x00", "\ud800"])
def test_invalid_json_or_postgresql_text_is_unavailable(invalid):
    root = workflow()
    root.artifact.input_schema["default"] = invalid
    with pytest.raises(ExecutionRequirementsUnavailable):
        inspect(root)


def test_installed_profiles_must_be_present_and_match_exactly():
    requirements = inspect(workflow(["lookup"]), [tool()])
    assert requirements.profiles == frozenset({
        ExecutionProfile("interpreter", "workflow", WORKFLOW_INTERPRETER_PROFILE),
        ExecutionProfile("checkpoint", "workflow", WORKFLOW_CHECKPOINT_PROFILE),
        ExecutionProfile("interpreter", "tool", TOOL_INTERPRETER_PROFILE),
        ExecutionProfile("workflow_adapter", "tool", WORKFLOW_ADAPTER_PROFILES["tool"]),
    })
    for adapter_profile, supported in [
        (None, False), (WORKFLOW_ADAPTER_PROFILES["tool"] + ".different", False),
        (WORKFLOW_ADAPTER_PROFILES["tool"], True),
    ]:
        manifest = InstalledExecutionCapabilities.from_descriptors(
            ("workflow",), workflow_interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
            workflow_checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE,
            tool_interpreter_profile=TOOL_INTERPRETER_PROFILE,
            workflow_adapters=(() if adapter_profile is None else (
                WorkflowAdapterDescriptor(kind="tool", profile=adapter_profile),
            )),
        )
        assert manifest.supports(requirements) is supported
