"""Offline code-first publication, installation, closure and completion contracts."""

import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domain.agent_runtime import AgentRuntimeDescriptor
from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CODE_WORKFLOW_RESULT_KEY,
    MAX_CODE_FRAME_DEPTH,
    MAX_CODE_NODES,
    ActivityCall,
    ActivityOutcome,
    CodeActivity,
    CodeNodeScope,
    CodeReference,
    CodeWorkflowDefinition,
    is_activity_unavailable,
)
from domain.capability import CapabilityDependency, CapabilityError, CapabilityVersionDraft, CapabilityVersionRecord
from domain.capability_definitions import (
    WORKFLOW_CHECKPOINT_PROFILE,
    WORKFLOW_INTERPRETER_PROFILE,
    interpret_definition,
)
from domain.execution_requirements import (
    AGENT_INTERPRETER_PROFILE,
    AGENT_RUNTIME_PROFILES,
    TOOL_INTERPRETER_PROFILE,
    WORKFLOW_ADAPTER_PROFILES,
    ExecutionArtifact,
    ExecutionProfile,
    ExecutionRequirements,
    ExecutionRequirementsUnavailable,
    InstalledExecutionCapabilities,
    required_execution_profiles,
)
from domain.platform_tool import InstalledReadonlyTool, ToolDefinition
from domain.task import TaskRecord
from domain.task_completion import (
    AgentCompletionEvidence,
    CompletionCommand,
    CompletionEvidence,
    CompletionTarget,
    NodeCompletionEvidence,
    require_completion_evidence,
)
from domain.task_execution import ExecutionFailure, TaskClaim, TaskLease
from domain.workflow import WorkflowDefinition
from domain.workflow_invocation import WorkflowInvocation
from domain.workflow_runtime import WorkflowAdapterDescriptor
from framework.workflow_recovery import assess_workflow_recovery

FLOW_REF = CodeReference(key="business.flow", version="1", digest="a" * 64)
TOOL_REF = CodeReference(key="business.lookup", version="1", digest="b" * 64)
CODE_PROFILES = frozenset({
    ExecutionProfile("interpreter", "workflow_code", CODE_WORKFLOW_INTERPRETER_PROFILE),
    ExecutionProfile("checkpoint", "workflow_code", CODE_WORKFLOW_CHECKPOINT_PROFILE),
})


def code_definition(activities=None, *, implementation=FLOW_REF):
    return {
        "definition_version": "workflow.code.v1", "implementation": implementation.model_dump(),
        "activities": activities if activities is not None else [{"key": "prepare", "kind": "step"}],
    }


def code_tool(*, implementation=TOOL_REF):
    return {
        "definition_version": "tool.code.v1", "binding_key": "approved", "operation_type": "read_only",
        "implementation": implementation.model_dump(),
    }


def published(key, kind, definition, *, dependencies=(), schema_text=""):
    draft = CapabilityVersionDraft(
        version="1", definition=definition, dependencies=list(dependencies),
        input_schema={"type": "object", "description": schema_text},
        output_schema={"type": "object", "description": schema_text},
    )
    artifact = CapabilityVersionRecord(
        **draft.model_dump(), id=f"{key}-1", capability_id=key, status="published",
        content_hash=draft.fingerprint(), created_by="test", create_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return ExecutionArtifact(key, key, kind, artifact)


def pin(key):
    return {"key": key, "version": "1"}


def text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def call_receipt_key(parent_result_id, activity, invocation_hash):
    return "call." + text_hash(parent_result_id + "\0" + activity + "\0" + invocation_hash)


def result_receipt_key(parent_node_id=None):
    return CODE_WORKFLOW_RESULT_KEY if parent_node_id is None else "result." + text_hash(parent_node_id)


def inspect(root, targets=(), *, calls=None):
    pinned = {(target.key, target.artifact.version): target for target in targets}

    def resolve(key, version):
        if calls is not None:
            calls.append((key, version))
        return pinned.get((key, version))

    return required_execution_profiles(root, version="1", content_hash=root.artifact.content_hash, resolve=resolve)


def installed(*, profiles=CODE_PROFILES, workflow_implementations=(), tool_implementations=(),
              adapters=(), runtimes=()):
    return InstalledExecutionCapabilities.from_descriptors(
        ("workflow",), workflow_interpreter_profile=WORKFLOW_INTERPRETER_PROFILE,
        workflow_checkpoint_profile=WORKFLOW_CHECKPOINT_PROFILE,
        agent_interpreter_profile=AGENT_INTERPRETER_PROFILE if runtimes else None,
        tool_interpreter_profile=TOOL_INTERPRETER_PROFILE, workflow_adapters=adapters,
        agent_runtimes=runtimes, additional_profiles=profiles,
        workflow_implementations=workflow_implementations, tool_implementations=tool_implementations,
    )


def test_code_workflow_interpretation_is_distinct_and_resources_have_existing_roles():
    nodes = [
        {"key": "prepare", "kind": "step"},
        {"key": "review", "kind": "agent", "capability": pin("reviewer")},
        {"key": "lookup", "kind": "tool", "capability": pin("lookup")},
        {"key": "write", "kind": "llm", "capability": pin("completion"), "prompt": pin("summary")},
    ]
    draft = CapabilityVersionDraft(version="1", definition=code_definition(nodes), dependencies=[
        pin(key) for key in ("reviewer", "lookup", "completion", "summary")
    ])
    contract = interpret_definition("workflow", draft)
    definition = contract.materialize()
    assert isinstance(definition, CodeWorkflowDefinition)
    assert contract.profile == CODE_WORKFLOW_INTERPRETER_PROFILE
    assert definition.implementation == FLOW_REF
    assert isinstance(definition.activities, tuple)
    assert definition.max_parallelism == 4
    assert [(ref.key, ref.version, ref.kind, ref.role) for ref in contract.references] == [
        ("reviewer", "1", "agent", "resource"), ("lookup", "1", "tool", "resource"),
        ("completion", "1", "model_policy", "completion"), ("summary", "1", "prompt", "resource"),
    ]
    draft.definition["implementation"]["digest"] = "c" * 64
    assert contract.materialize().implementation == FLOW_REF


@pytest.mark.parametrize("kind", ["agent", "tool", "llm", "workflow"])
def test_code_workflow_external_capabilities_and_prompts_must_be_declared_pins(kind):
    node = {"key": "invoke", "kind": kind, "capability": pin("resource")}
    if kind == "llm":
        node["prompt"] = pin("prompt")
    draft = CapabilityVersionDraft(version="1", definition=code_definition([node]))
    with pytest.raises(CapabilityError, match="pinned"):
        interpret_definition("workflow", draft)
    draft.dependencies = [CapabilityDependency(**pin("resource"))]
    if kind == "llm":
        with pytest.raises(CapabilityError, match="pinned"):
            interpret_definition("workflow", draft)


@pytest.mark.parametrize("changes", [
    {"activities": []}, {"activities": [{"key": CODE_WORKFLOW_RESULT_KEY, "kind": "step"}]},
    {"activities": [{"key": "same", "kind": "step"}] * 2},
    {"activities": [{"key": "step", "kind": "step", "capability": pin("resource")}]},
    {"activities": [{"key": "agent", "kind": "agent"}]},
    {"activities": [{"key": "step", "kind": "step", "prompt": pin("prompt")}]},
    {"activities": [{"key": "step", "kind": "step", "required": "true"}]},
    {"activities": [{"key": "step", "kind": "step", "timeout_seconds": float("inf")}]},
    {"activities": [{"key": "step", "kind": "step", "timeout_seconds": 0}]},
    {"activities": [{"key": "rule", "kind": "rule"}]},
    {"activities": [{"key": "step", "kind": "step", "max_calls": 1001}]},
    {"activities": [{"key": "step", "kind": "step", "max_calls": True}]},
    {"activities": [{"key": "step", "kind": "step", "on_unavailable": "retry"}]},
    {"max_parallelism": 17}, {"max_parallelism": True}, {"nodes": []},
    {"definition_version": "workflow.code.v2"}, {"output_mapping": {}}, {"implementation": None},
])
def test_code_workflow_is_closed_and_reserves_its_result_slot(changes):
    with pytest.raises(ValueError):
        interpret_definition("workflow", CapabilityVersionDraft(version="1", definition={**code_definition(), **changes}))


def test_code_references_and_workflow_slots_are_frozen_and_strict():
    definition = CodeWorkflowDefinition.model_validate(code_definition())
    for value, key, replacement in [
        (FLOW_REF, "digest", "c" * 64), (definition, "implementation", TOOL_REF),
        (definition.activities[0], "required", False),
    ]:
        with pytest.raises(ValidationError, match="frozen"):
            setattr(value, key, replacement)
    for digest in ("A" * 64, "a" * 63, "not-a-source-digest"):
        with pytest.raises(ValidationError):
            CodeReference(key="business.flow", version="1", digest=digest)


def test_dsl_and_legacy_tools_keep_their_existing_semantics():
    flow = published("flow", "workflow", {"nodes": [
        {"key": "project", "kind": "rule", "rule": {"operation": "project"}},
    ]})
    contract = interpret_definition("workflow", flow.artifact)
    assert isinstance(contract.materialize(), WorkflowDefinition)
    assert contract.profile == WORKFLOW_INTERPRETER_PROFILE
    assert inspect(flow).implementations == frozenset()
    assert inspect(flow).workflow_implementations == inspect(flow).tool_implementations == frozenset()
    tool = ToolDefinition(binding_key="approved", operation_type="read_only")
    assert tool.definition_version == "tool.v1" and tool.implementation is None
    assert tool.timeout_seconds == 30
    assert ToolDefinition.model_validate(tool.model_dump()) == tool
    code = published("lookup", "tool", code_tool())
    assert interpret_definition("tool", code.artifact).profile == TOOL_INTERPRETER_PROFILE


@pytest.mark.parametrize("changes", [
    {"definition_version": "tool.v1"}, {"implementation": None},
    {"definition_version": "tool.code.v2"}, {"operation_type": "side_effecting"},
    {"implementation": {**TOOL_REF.model_dump(), "module": "untrusted.module"}},
])
def test_code_tools_require_an_explicit_source_pin_without_weakening_read_only_contract(changes):
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate({**code_tool(), **changes})
    missing = code_tool()
    del missing["implementation"]
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(missing)


async def handler(_context, _arguments):
    return {}


def test_installed_tool_source_pin_matches_the_handler_identity_and_is_optional_for_legacy():
    assert InstalledReadonlyTool("legacy", "1", handler).implementation is None
    tool = InstalledReadonlyTool(TOOL_REF.key, TOOL_REF.version, handler, implementation=TOOL_REF)
    assert tool.implementation == TOOL_REF
    for key, version in (("different", "1"), (TOOL_REF.key, "2")):
        with pytest.raises(ValueError, match="handler key/version"):
            InstalledReadonlyTool(key, version, handler, implementation=TOOL_REF)
    invalid = TOOL_REF.model_copy(update={"digest": "invalid"})
    with pytest.raises(ValidationError):
        InstalledReadonlyTool(TOOL_REF.key, "1", handler, implementation=invalid)


def test_step_only_code_workflow_requires_its_own_profiles_and_exact_source_not_dsl_profiles():
    requirements = inspect(published("flow", "workflow", code_definition()))
    assert requirements == ExecutionRequirements("workflow", CODE_PROFILES, workflow_implementations=frozenset({FLOW_REF}))
    assert requirements.implementations == requirements.workflow_implementations == frozenset({FLOW_REF})
    assert requirements.tool_implementations == frozenset()
    assert installed(workflow_implementations=(FLOW_REF,)).supports(requirements)
    assert not installed(profiles=(), workflow_implementations=(FLOW_REF,)).supports(requirements)
    assert not installed().supports(requirements)
    with pytest.raises(TypeError):
        InstalledExecutionCapabilities.from_descriptors(("workflow",), implementations=(FLOW_REF,))
    with pytest.raises(TypeError):
        ExecutionRequirements("workflow", CODE_PROFILES, implementations=(FLOW_REF,))
    for changes in ({"digest": "c" * 64}, {"version": "2"}, {"key": "another.flow"}):
        assert not installed(workflow_implementations=(FLOW_REF.model_copy(update=changes),)).supports(requirements)
    for profile in CODE_PROFILES:
        assert not installed(profiles=CODE_PROFILES - {profile}, workflow_implementations=(FLOW_REF,)).supports(requirements)
    with pytest.raises(FrozenInstanceError):
        requirements.implementations = frozenset()
    with pytest.raises(FrozenInstanceError):
        installed().implementations = frozenset()
    with pytest.raises(FrozenInstanceError):
        requirements.workflow_implementations = frozenset()
    with pytest.raises(FrozenInstanceError):
        installed().tool_implementations = frozenset()


@pytest.mark.parametrize("profile", [
    ExecutionProfile("interpreter", "workflow_code", "unreviewed.profile"),
    ExecutionProfile("implementation", "business.flow", "a" * 64),
    ExecutionProfile("interpreter", "workflow", CODE_WORKFLOW_INTERPRETER_PROFILE),
])
def test_additional_profiles_are_closed_semantics_not_arbitrary_source_digests(profile):
    with pytest.raises(ValueError, match="closed versioned semantics"):
        installed(profiles=(profile,))


def test_implementation_sets_normalize_duplicates_and_reject_conflicting_digests():
    duplicate = [FLOW_REF, FLOW_REF]
    assert installed(workflow_implementations=iter(duplicate)).implementations == frozenset({FLOW_REF})
    assert ExecutionRequirements("workflow", CODE_PROFILES, duplicate).implementations == frozenset({FLOW_REF})
    assert ExecutionRequirements("workflow", CODE_PROFILES).implementations == frozenset()
    assert installed(workflow_implementations=duplicate).workflow_implementations == frozenset({FLOW_REF})
    assert installed(workflow_implementations=duplicate).tool_implementations == frozenset()
    assert ExecutionRequirements("workflow", CODE_PROFILES, duplicate).workflow_implementations == frozenset({FLOW_REF})
    assert ExecutionRequirements("workflow", CODE_PROFILES, duplicate).tool_implementations == frozenset()
    conflict = [FLOW_REF, FLOW_REF.model_copy(update={"digest": "c" * 64})]
    for construct in (
        lambda: installed(workflow_implementations=conflict),
        lambda: ExecutionRequirements("workflow", CODE_PROFILES, conflict),
    ):
        with pytest.raises(ValueError, match="Conflicting code implementation digests"):
            construct()
    with pytest.raises(ValueError, match="explicit CodeReference"):
        installed(workflow_implementations=(FLOW_REF.model_dump(),))


def test_typed_implementation_sets_are_normalized_and_included_in_the_audit_union():
    kwargs = {
        "workflow_implementations": [FLOW_REF, FLOW_REF],
        "tool_implementations": [TOOL_REF, TOOL_REF],
    }
    for contract in (installed(**kwargs), ExecutionRequirements("workflow", CODE_PROFILES, **kwargs)):
        assert contract.implementations == frozenset({FLOW_REF, TOOL_REF})
        assert contract.workflow_implementations == frozenset({FLOW_REF})
        assert contract.tool_implementations == frozenset({TOOL_REF})


@pytest.mark.parametrize("first,second", [
    ("workflow_implementations", "tool_implementations"),
    ("workflow_implementations", "workflow_implementations"), ("tool_implementations", "tool_implementations"),
])
def test_source_digest_conflicts_are_rejected_across_all_typed_sets(first, second):
    kwargs = {first: [FLOW_REF]}
    kwargs.setdefault(second, []).append(FLOW_REF.model_copy(update={"digest": "c" * 64}))
    for construct in (lambda: installed(**kwargs), lambda: ExecutionRequirements("workflow", CODE_PROFILES, **kwargs)):
        with pytest.raises(ValueError, match="Conflicting code implementation digests"):
            construct()


@pytest.mark.parametrize("required_role", ["workflow", "tool"])
def test_exact_source_and_profiles_never_allow_an_implementation_installed_in_the_other_role(required_role):
    if required_role == "workflow":
        requirements = inspect(published("wrong-kind", "workflow", code_definition(implementation=TOOL_REF)))
        manifest = installed(tool_implementations=(TOOL_REF,))
        assert requirements.workflow_implementations == frozenset({TOOL_REF})
    else:
        root = published("flow", "workflow", {"nodes": [
            {"key": "lookup", "kind": "tool", "capability": pin("lookup")},
        ]}, dependencies=[pin("lookup")])
        requirements = inspect(root, [published("lookup", "tool", code_tool(implementation=FLOW_REF))])
        manifest = installed(
            workflow_implementations=(FLOW_REF,),
            adapters=(WorkflowAdapterDescriptor(kind="tool", profile=WORKFLOW_ADAPTER_PROFILES["tool"]),),
        )
        assert requirements.tool_implementations == frozenset({FLOW_REF})
    assert requirements.profiles <= manifest.profiles
    assert requirements.implementations <= manifest.implementations
    assert not manifest.supports(requirements)


def transitive_closure():
    root = published("flow", "workflow", code_definition([
        {"key": "prepare", "kind": "step"},
        {"key": "review", "kind": "agent", "capability": pin("reviewer")},
        {"key": "lookup", "kind": "tool", "capability": pin("lookup")},
        {"key": "write", "kind": "llm", "capability": pin("completion"), "prompt": pin("summary")},
    ]), dependencies=[pin(key) for key in ("reviewer", "lookup", "completion", "summary", "unused")])
    agent = published("reviewer", "agent", {
        "definition_version": "agent.v1", "runtime": "pi", "instruction_prompt": pin("instructions"),
        "model_policy": pin("turn"), "tools": [{"alias": "query", "capability": pin("transitive")}],
        "skills": [pin("guide")], "limits": {"max_turns": 2, "max_tool_calls": 1, "timeout_seconds": 60.0},
    }, dependencies=[pin(key) for key in ("instructions", "turn", "transitive", "guide")])
    targets = [
        agent, published("lookup", "tool", {"binding_key": "approved", "operation_type": "read_only"}),
        published("transitive", "tool", code_tool()),
        published("completion", "model_policy", {"provider_key": "offline", "model": "offline"}),
        published("turn", "model_policy", {"interaction": "agent_turn", "provider_key": "offline", "model": "offline"}),
        published("summary", "prompt", {"template": "Write summary"}),
        published("instructions", "prompt", {"template": "Review"}),
        published("guide", "skill", {"instructions": "Use reviewed facts"}),
    ]
    return root, targets


def test_code_closure_walks_every_transitive_agent_resource_and_code_tool_but_not_unused_dependencies():
    root, targets = transitive_closure()
    calls = []
    requirements = inspect(root, targets, calls=calls)
    assert set(calls) == {(item.key, "1") for item in targets}
    assert requirements.implementations == frozenset({FLOW_REF, TOOL_REF})
    assert requirements.workflow_implementations == frozenset({FLOW_REF})
    assert requirements.tool_implementations == frozenset({TOOL_REF})
    assert requirements.profiles == CODE_PROFILES | {
        ExecutionProfile("interpreter", "agent", AGENT_INTERPRETER_PROFILE),
        ExecutionProfile("agent_runtime", "pi", AGENT_RUNTIME_PROFILES["pi"]),
        ExecutionProfile("interpreter", "tool", TOOL_INTERPRETER_PROFILE),
        *(ExecutionProfile("workflow_adapter", key, value) for key, value in WORKFLOW_ADAPTER_PROFILES.items()),
    }
    manifest = installed(
        workflow_implementations=(FLOW_REF,), tool_implementations=(TOOL_REF,),
        runtimes=(AgentRuntimeDescriptor(key="pi", profile=AGENT_RUNTIME_PROFILES["pi"]),),
        adapters=tuple(WorkflowAdapterDescriptor(kind=key, profile=value) for key, value in WORKFLOW_ADAPTER_PROFILES.items()),
    )
    assert manifest.supports(requirements)
    assert not installed(workflow_implementations=(FLOW_REF,), tool_implementations=(TOOL_REF,)).supports(requirements)


@pytest.mark.parametrize("failure", ["missing", "kind", "unpublished", "hash", "completion-role", "agent-turn-role"])
def test_code_closure_revalidates_exact_pins_and_policy_roles(failure):
    root, targets = transitive_closure()
    if failure == "missing":
        targets = [item for item in targets if item.key != "guide"]
    elif failure == "kind":
        targets = [replace(item, kind="prompt") if item.key == "guide" else item for item in targets]
    elif failure in {"unpublished", "hash"}:
        tool = next(item for item in targets if item.key == "transitive")
        if failure == "unpublished":
            tool.artifact.status = "draft"
        else:
            tool.artifact.definition["implementation"]["digest"] = "c" * 64
    else:
        key = "completion" if failure == "completion-role" else "turn"
        policy = {"provider_key": "offline", "model": "offline"}
        if failure == "completion-role":
            policy["interaction"] = "agent_turn"
        targets = [published(key, "model_policy", policy) if item.key == key else item for item in targets]
    with pytest.raises(ExecutionRequirementsUnavailable):
        inspect(root, targets)


def test_code_closure_rejects_conflicting_source_pins_between_workflow_and_tool():
    root = published("flow", "workflow", code_definition([
        {"key": "lookup", "kind": "tool", "capability": pin("lookup")},
    ]), dependencies=[pin("lookup")])
    tool = published("lookup", "tool", code_tool(implementation=FLOW_REF.model_copy(update={"digest": "c" * 64})))
    with pytest.raises(ExecutionRequirementsUnavailable):
        inspect(root, [tool])


@pytest.mark.parametrize("count,allowed", [(6, True), (7, False)])
def test_code_closure_preserves_the_utf8_byte_bound_and_counts_shared_pins_once(count, allowed):
    keys = [f"lookup-{index}" for index in range(count)]
    root = published("flow", "workflow", code_definition([
        {"key": f"slot-{index}", "kind": "tool", "capability": pin(key)}
        for index, key in enumerate([*keys, *keys])
    ]), dependencies=[pin(key) for key in keys])
    tools = [published(key, "tool", code_tool(), schema_text="界" * 200_000) for key in keys]
    if allowed:
        assert inspect(root, tools).implementations == frozenset({FLOW_REF, TOOL_REF})
    else:
        with pytest.raises(ExecutionRequirementsUnavailable, match="exceeds inspection bounds"):
            inspect(root, tools)


def completion_case():
    root = published("flow", "workflow", code_definition([
        {"key": "prepare", "kind": "step", "max_calls": 2},
        {"key": "lookup", "kind": "tool", "capability": pin("lookup"), "required": False},
        {"key": "review", "kind": "agent", "capability": pin("reviewer")},
        {"key": "write", "kind": "llm", "capability": pin("completion")},
    ]), dependencies=[pin(key) for key in ("lookup", "reviewer", "completion")])
    command = CompletionCommand.prepare(status="succeeded", result={"ok": True})
    nodes = (
        NodeCompletionEvidence("prepare-id", "prepare", "step", "succeeded", "c" * 64),
        NodeCompletionEvidence("lookup-id", "lookup", "tool", "skipped", None),
        NodeCompletionEvidence("review-id", "review", "agent", "succeeded", "d" * 64),
        NodeCompletionEvidence("write-id", "write", "llm", "succeeded", "e" * 64),
        NodeCompletionEvidence("result-id", CODE_WORKFLOW_RESULT_KEY, "result", "succeeded", command.output_hash),
    )
    nodes = tuple(replace(node, parent_node_id="result-id", key=call_receipt_key("result-id", node.key, text_hash("once")), scope=CodeNodeScope(
        "flow", "1", root.artifact.content_hash, node.key, text_hash("once"),
    )) if node.kind != "result" else replace(node, scope=CodeNodeScope("flow", "1", root.artifact.content_hash))
        for node in nodes)
    child = AgentCompletionEvidence("reviewer", "reviewer", "1", "f" * 64, nodes[2].output_hash, nodes[2].id)
    target = CompletionTarget(root.capability_id, root.kind, "1", root.artifact.content_hash, "flow")
    return target, root.artifact, command, CompletionEvidence(nodes=nodes, agents=(child,))


@pytest.mark.parametrize("optional_status", ["skipped", "succeeded"])
def test_code_completion_accepts_dynamic_activities_and_matching_result_in_any_order(optional_status):
    target, artifact, command, evidence = completion_case()
    nodes = tuple(replace(node, status=optional_status, output_hash="c" * 64 if optional_status == "succeeded" else None)
                  if node.scope.activity_key == "lookup" else node for node in reversed(evidence.nodes))
    assert require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes)) is None


@pytest.mark.parametrize("failure", [
    "missing-result", "missing-required", "duplicate", "extra", "same-id",
    "required-skipped", "optional-failed", "optional-running", "required-failed", "wrong-kind",
    "result-kind", "result-status", "result-hash",
])
def test_code_completion_rejects_incomplete_repeated_extra_failed_or_mismatched_evidence(failure):
    target, artifact, command, evidence = completion_case()
    nodes = list(evidence.nodes)
    if failure.startswith("missing-"):
        nodes.pop({"missing-result": 4, "missing-required": 0, "missing-optional": 1}[failure])
    elif failure == "duplicate":
        nodes.append(nodes[0])
    elif failure == "extra":
        nodes.append(replace(nodes[0], id="extra", scope=replace(nodes[0].scope, activity_key="extra")))
    else:
        index, changes = {
            "same-id": (0, {"id": nodes[2].id}), "required-skipped": (0, {"status": "skipped"}),
            "optional-failed": (1, {"status": "failed"}), "optional-running": (1, {"status": "running"}),
            "required-failed": (0, {"status": "failed"}), "wrong-kind": (0, {"kind": "tool"}),
            "result-kind": (4, {"kind": "step"}), "result-status": (4, {"status": "skipped"}),
            "result-hash": (4, {"output_hash": "0" * 64}),
        }[failure]
        nodes[index] = replace(nodes[index], **changes)
    with pytest.raises(ValueError):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=tuple(nodes)))


@pytest.mark.parametrize("failure", ["missing", "duplicate", "root", "extra", "key", "version", "output_hash", "parent_node_id"])
def test_code_completion_rejects_orphan_roots_extra_children_and_mismatched_agent_evidence(failure):
    target, artifact, command, evidence = completion_case()
    child = evidence.agents[0]
    if failure == "missing":
        agents = ()
    elif failure == "duplicate":
        agents = (child, child)
    elif failure in {"root", "extra"}:
        agents = (child, replace(child, parent_node_id=None if failure == "root" else "prepare-id"))
    else:
        agents = (replace(child, **{failure: "wrong"}),)
    with pytest.raises(ValueError):
        require_completion_evidence(target, artifact, command, replace(evidence, agents=agents))


def test_skipped_optional_agent_slot_cannot_have_a_completed_child():
    root = published("flow", "workflow", code_definition([
        {"key": "review", "kind": "agent", "capability": pin("reviewer"), "required": False},
    ]), dependencies=[pin("reviewer")])
    command = CompletionCommand.prepare(status="succeeded", result={})
    nodes = (
        NodeCompletionEvidence("review-id", call_receipt_key("result-id", "review", text_hash("once")), "agent", "skipped", None, "result-id",
                               CodeNodeScope("flow", "1", root.artifact.content_hash, "review", text_hash("once"))),
        NodeCompletionEvidence("result-id", CODE_WORKFLOW_RESULT_KEY, "result", "succeeded", command.output_hash,
                               scope=CodeNodeScope("flow", "1", root.artifact.content_hash)),
    )
    target = CompletionTarget(root.capability_id, "workflow", "1", root.artifact.content_hash)
    assert require_completion_evidence(target, root.artifact, command, CompletionEvidence(nodes=nodes)) is None
    child = AgentCompletionEvidence("reviewer", "reviewer", "1", "f" * 64, None, "review-id")
    with pytest.raises(ValueError):
        require_completion_evidence(target, root.artifact, command, CompletionEvidence(nodes=nodes, agents=(child,)))


@pytest.mark.parametrize("status", ["failed", "timed_out", "cancelled"])
def test_code_workflow_recovery_explicitly_disallows_automatic_replay_even_for_step_only_work(status):
    root = published("flow", "workflow", code_definition())
    source = TaskRecord(
        id="task", project_key="project", capability_key="flow", capability_kind="workflow", version="1",
        content_hash=root.artifact.content_hash, status=status, submitted_by="test", create_time=datetime.now(UTC),
        finish_time=datetime.now(UTC),
    )
    assessment = assess_workflow_recovery(source, root.artifact, root.artifact.content_hash)
    assert not assessment.eligible and assessment.reason == "unsupported_node_kind"


def test_activity_requests_and_outcomes_are_detached_immutable_finite_objects():
    activity = CodeActivity(key="review", kind="step")
    assert activity.required and activity.max_calls == 1 and activity.on_unavailable == "fail"
    values = {"rows": [{"id": 1}]}
    call = ActivityCall("review", values, key="customer:one")
    outcome = ActivityOutcome("succeeded", values)
    values["rows"][0]["id"] = 2
    assert call.activity == "review" and call.key == "customer:one"
    assert call.values() == outcome.require_value() == {"rows": [{"id": 1}]}
    call.values()["rows"].clear()
    outcome.value["rows"].clear()
    assert call.values() == outcome.value == {"rows": [{"id": 1}]}
    for obj, attr in ((call, "key"), (outcome, "status")):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, "changed")
    assert ActivityCall("review", {}).key == "once"
    for key in ("", "x" * 201, "bad\x00", "bad\n", "bad\x85"):
        with pytest.raises(ValueError):
            ActivityCall("review", {}, key=key)
    for value in ([], {"nan": float("nan")}, {"inf": float("inf")}, {"too_large": "x" * 1_000_001}):
        with pytest.raises(ValueError):
            ActivityCall("review", value)
        with pytest.raises(ValueError):
            ActivityOutcome("succeeded", value)
    for construct in (
        lambda: ActivityOutcome("succeeded"),
        lambda: ActivityOutcome("succeeded", {}, "MODEL_OUTPUT_INVALID"),
        lambda: ActivityOutcome("skipped", {}),
        lambda: ActivityOutcome("skipped", error_code="MODEL_OUTPUT_INVALID"),
        lambda: ActivityOutcome("unavailable", {}, "MODEL_OUTPUT_INVALID"),
        lambda: ActivityOutcome("unavailable", error_code="MODEL_ACCOUNTING_FAILED"),
    ):
        with pytest.raises(ValueError):
            construct()
    with pytest.raises(ExecutionFailure, match="MODEL_OUTPUT_INVALID"):
        ActivityOutcome("unavailable", error_code="MODEL_OUTPUT_INVALID").require_value()
    with pytest.raises(ExecutionFailure, match="CODE_ACTIVITY_SKIPPED"):
        ActivityOutcome("skipped").require_value()


@pytest.mark.parametrize("code", [
    "MODEL_UPSTREAM_UNAVAILABLE", "MODEL_UPSTREAM_TIMEOUT", "MODEL_RESPONSE_INVALID",
    "MODEL_RESPONSE_REFUSED", "MODEL_OUTPUT_INVALID", "MODEL_AGENT_TURN_INVALID",
    "TOOL_TIMEOUT_RESULT_UNKNOWN", "TOOL_EXECUTION_FAILED_RESULT_UNKNOWN",
    "TOOL_OUTPUT_INVALID", "TOOL_UPSTREAM_UNAVAILABLE",
])
def test_only_explicit_settled_operational_failures_can_be_reported_unavailable(code):
    assert is_activity_unavailable(code)
    assert ActivityOutcome("unavailable", error_code=code).error_code == code


@pytest.mark.parametrize("code", [
    "MODEL_ACCOUNTING_FAILED", "MODEL_ACCOUNTING_RESULT_UNKNOWN", "MODEL_NOT_AUTHORIZED",
    "TOOL_NOT_AUTHORIZED", "TASK_CANCELLED", "TASK_LEASE_LOST", "MODEL_POLICY_INVALID",
    "CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN", "MODEL_VALIDATION_UNAVAILABLE", "TOOL_BINDING_UNAVAILABLE",
    "MODEL_UPSTREAM_FUTURE_ERROR", "EXECUTION_NOT_AUTHORIZED", "CODE_ACTIVITY_GROUP_CANCELLED", "",
])
def test_control_auth_accounting_configuration_and_unknown_errors_are_not_unavailable(code):
    assert not is_activity_unavailable(code)


def test_scope_requires_complete_bounded_activity_identity():
    scope = CodeNodeScope("flow", "1", "a" * 64)
    with pytest.raises(FrozenInstanceError):
        scope.version = "2"
    for changes in (
        {"activity_key": "review"}, {"invocation_hash": "b" * 64},
        {"content_hash": "wrong"}, {"activity_key": "review", "invocation_hash": "wrong"},
    ):
        with pytest.raises(ValueError):
            replace(scope, **changes)


def test_nested_workflow_references_use_code_role_and_walk_typed_implementation_closure():
    child_ref = CodeReference(key="business.child", version="1", digest="c" * 64)
    root = published("flow", "workflow", code_definition([
        {"key": "delegate", "kind": "workflow", "capability": pin("child")},
    ]), dependencies=[pin("child")])
    child = published("child", "workflow", code_definition(implementation=child_ref))
    assert interpret_definition("workflow", root.artifact).references[0].role == "code_workflow"
    requirements = inspect(root, [child])
    assert requirements.profiles == CODE_PROFILES
    assert requirements.workflow_implementations == frozenset({FLOW_REF, child_ref})
    legacy = published("child", "workflow", {"nodes": [{
        "key": "rule", "kind": "rule", "rule": {"operation": "project"},
    }]})
    with pytest.raises(ExecutionRequirementsUnavailable):
        inspect(root, [legacy])


def test_workflow_invocation_preserves_root_identity_budget_and_delegation():
    root = published("flow", "workflow", code_definition())
    task = TaskRecord(id="task", project_key="project", capability_key="flow", capability_kind="workflow",
                      version="1", content_hash=root.artifact.content_hash, status="running", submitted_by="test",
                      create_time=datetime.now(UTC), finish_time=None)
    claim = TaskClaim(task, TaskLease("task", "attempt", "worker", "private"), {"root": True}, root.artifact)
    invocation = WorkflowInvocation.from_root(claim).with_timeout(30)
    child = published("child", "workflow", code_definition())
    values = {"items": [1]}
    delegated = invocation.child(CapabilityDependency(**pin("child")), child.artifact, values, "delegate-id")
    values["items"].clear()
    assert delegated.input == {"items": [1]}
    assert delegated.task is invocation.task is task
    assert delegated.lease is invocation.lease is claim.lease
    assert delegated.budget is invocation.budget
    assert delegated.root is invocation.root
    assert delegated.delegated_workflows == (CapabilityDependency(**pin("child")),)
    assert delegated.parent_node_id == "delegate-id"
    assert delegated.with_timeout(1).limit_timeout(60) <= 1


def test_dynamic_calls_allow_optional_absence_and_distinct_repetition_within_bound():
    target, artifact, command, evidence = completion_case()
    first = evidence.nodes[0]
    second = replace(first, id="prepare-second", key=call_receipt_key("result-id", "prepare", text_hash("second")),
                     scope=replace(first.scope, invocation_hash=text_hash("second")))
    nodes = tuple(node for node in evidence.nodes if node.scope.activity_key != "lookup") + (second,)
    require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes))
    for extra in (replace(second, id="third", key=call_receipt_key("result-id", "prepare", text_hash("third")),
                          scope=replace(first.scope, invocation_hash=text_hash("third"))),
                  replace(second, id="third")):
        with pytest.raises(ValueError):
            require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes + (extra,)))


def test_different_activities_can_each_use_once_but_same_activity_cannot_repeat_it():
    target, artifact, command, evidence = completion_case()
    calls = tuple(node for node in evidence.nodes if node.kind != "result")
    assert len(calls) > 1
    assert {node.scope.invocation_hash for node in calls} == {text_hash("once")}
    require_completion_evidence(target, artifact, command, evidence)
    duplicate = replace(calls[0], id="duplicate-invocation")
    with pytest.raises(ValueError, match="unique per activity"):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=(*evidence.nodes, duplicate)))


def nested_completion_case(*, unavailable=False):
    root = published("flow", "workflow", code_definition([
        {"key": "delegate", "kind": "workflow", "capability": pin("child"), "on_unavailable": "continue"},
    ]), dependencies=[pin("child")])
    child = published("child", "workflow", code_definition([
        {"key": "lookup", "kind": "tool", "capability": pin("lookup")},
        {"key": "later", "kind": "step"},
    ]), dependencies=[pin("lookup")])
    command = CompletionCommand.prepare(status="succeeded", result={"ok": True})
    scope = CodeNodeScope("flow", "1", root.artifact.content_hash)
    child_scope = CodeNodeScope("child", "1", child.artifact.content_hash)
    code = "TOOL_UPSTREAM_UNAVAILABLE" if unavailable else None
    nodes = (
        NodeCompletionEvidence("root", CODE_WORKFLOW_RESULT_KEY, "result", "succeeded", command.output_hash, scope=scope),
        NodeCompletionEvidence("delegate", call_receipt_key("root", "delegate", text_hash("once")), "workflow", "unavailable" if unavailable else "succeeded",
                               None if unavailable else "d" * 64, "root",
                               replace(scope, activity_key="delegate", invocation_hash=text_hash("once")), code),
        NodeCompletionEvidence("child", result_receipt_key("delegate"), "result", "failed" if unavailable else "succeeded",
                               None if unavailable else "d" * 64, "delegate", child_scope, code),
        NodeCompletionEvidence("lookup", call_receipt_key("child", "lookup", text_hash("once")), "tool", "failed" if unavailable else "succeeded",
                               None if unavailable else "e" * 64, "child",
                               replace(child_scope, activity_key="lookup", invocation_hash=text_hash("once")), code),
    )
    if not unavailable:
        nodes += (NodeCompletionEvidence("later", call_receipt_key("child", "later", text_hash("once")), "step", "succeeded", "f" * 64, "child",
                                         replace(child_scope, activity_key="later", invocation_hash=text_hash("once"))),)
    evidence = CompletionEvidence(nodes=nodes, workflow_artifacts=(("child", child.artifact),))
    target = CompletionTarget(root.capability_id, "workflow", "1", root.artifact.content_hash, "flow")
    return target, root.artifact, command, evidence


@pytest.mark.parametrize("unavailable", [False, True])
def test_nested_frames_match_published_pin_and_allow_safely_failed_partial_children(unavailable):
    require_completion_evidence(*nested_completion_case(unavailable=unavailable))


@pytest.mark.parametrize("node_id,key", [
    ("root", "result." + text_hash("delegate")),
    ("child", CODE_WORKFLOW_RESULT_KEY),
    ("child", "result." + text_hash("wrong-parent")),
    ("delegate", call_receipt_key("wrong-frame", "delegate", text_hash("once"))),
    ("lookup", call_receipt_key("child", "wrong-activity", text_hash("once"))),
    ("later", call_receipt_key("child", "later", text_hash("wrong-invocation"))),
])
def test_completion_rejects_receipt_keys_not_derived_from_actual_parent_and_scope(node_id, key):
    target, artifact, command, evidence = nested_completion_case()
    assert evidence.nodes[0].key == CODE_WORKFLOW_RESULT_KEY
    assert evidence.nodes[2].key == "result." + text_hash("delegate")
    assert evidence.nodes[3].scope.invocation_hash == evidence.nodes[4].scope.invocation_hash == text_hash("once")
    nodes = tuple(replace(node, key=key) if node.id == node_id else node for node in evidence.nodes)
    with pytest.raises(ValueError):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes))


@pytest.mark.parametrize("failure", [
    "root-key", "root-hash", "root-parent", "child-pin", "child-output", "child-artifact", "artifact-hash",
    "activity-frame", "orphan", "cycle", "kind", "extra-child", "no-child", "active-agent", "active-step", "active-call",
])
def test_nested_completion_rejects_forged_or_unfinished_hierarchy(failure):
    target, artifact, command, evidence = nested_completion_case()
    nodes = list(evidence.nodes)
    if failure in {"root-key", "root-hash", "child-pin", "activity-frame"}:
        index, changes = {
            "root-key": (0, {"workflow_key": "wrong"}), "root-hash": (0, {"content_hash": "0" * 64}),
            "child-pin": (2, {"version": "2"}), "activity-frame": (3, {"workflow_key": "flow"}),
        }[failure]
        nodes[index] = replace(nodes[index], scope=replace(nodes[index].scope, **changes))
    elif failure in {"root-parent", "orphan", "cycle"}:
        index, parent = {"root-parent": (0, "delegate"), "orphan": (3, "absent"), "cycle": (3, "lookup")}[failure]
        nodes[index] = replace(nodes[index], parent_node_id=parent)
    elif failure == "child-output":
        nodes[2] = replace(nodes[2], output_hash="0" * 64)
    elif failure == "kind":
        nodes[3] = replace(nodes[3], kind="step")
    elif failure == "extra-child":
        nodes.append(replace(nodes[2], id="extra-child"))
    elif failure == "no-child":
        nodes = nodes[:2]
    elif failure == "child-artifact":
        evidence = replace(evidence, workflow_artifacts=())
    elif failure == "artifact-hash":
        evidence.workflow_artifacts[0][1].definition["implementation"]["digest"] = "0" * 64
    else:
        field = {"active-agent": "running_agent", "active-step": "running_agent_step", "active-call": "running_tool_call"}[failure]
        evidence = replace(evidence, **{field: True})
    with pytest.raises(ValueError):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=tuple(nodes)))


@pytest.mark.parametrize("error", ["EXECUTION_NOT_AUTHORIZED", "TASK_LEASE_LOST", "MODEL_ACCOUNTING_RESULT_UNKNOWN",
                                   "CODE_WORKFLOW_CHECKPOINT_RESULT_UNKNOWN", "TOOL_BINDING_UNAVAILABLE"])
def test_failed_child_frames_cannot_hide_security_control_or_unknown_outcomes(error):
    target, artifact, command, evidence = nested_completion_case(unavailable=True)
    nodes = tuple(replace(node, error_code=error) if node.id in {"delegate", "child", "lookup"} else node
                  for node in evidence.nodes)
    with pytest.raises(ValueError):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes))


def test_failed_partial_frame_accepts_only_group_cancelled_siblings_and_no_active_work():
    target, artifact, command, evidence = nested_completion_case(unavailable=True)
    child_scope = evidence.nodes[2].scope
    sibling = NodeCompletionEvidence("later", call_receipt_key("child", "later", text_hash("once")), "step", "cancelled", None, "child",
                                     replace(child_scope, activity_key="later", invocation_hash=text_hash("once")),
                                     "CODE_ACTIVITY_GROUP_CANCELLED")
    require_completion_evidence(target, artifact, command, replace(evidence, nodes=(*evidence.nodes, sibling)))
    for changes in ({"error_code": "TASK_CANCELLED"}, {"status": "running"}):
        with pytest.raises(ValueError):
            require_completion_evidence(target, artifact, command,
                                        replace(evidence, nodes=(*evidence.nodes, replace(sibling, **changes))))


@pytest.mark.parametrize("state", ["cancelled", "completed", "operational-failure"])
def test_failed_partial_frame_requires_cancelled_agent_siblings_to_be_safely_terminal(state):
    target, artifact, command, evidence = nested_completion_case(unavailable=True)
    child = evidence.workflow_artifacts[0][1].model_copy(deep=True)
    child.definition["activities"][1] = {"key": "later", "kind": "agent", "capability": pin("reviewer")}
    child.dependencies.append(CapabilityDependency(**pin("reviewer")))
    child.content_hash = child.fingerprint()
    nodes = tuple(replace(node, scope=replace(node.scope, content_hash=child.content_hash))
                  if node.scope.workflow_key == "child" else node for node in evidence.nodes)
    sibling = NodeCompletionEvidence(
        "later", call_receipt_key("child", "later", text_hash("once")), "agent", "cancelled", None, "child",
        replace(nodes[2].scope, activity_key="later", invocation_hash=text_hash("once")),
        "CODE_ACTIVITY_GROUP_CANCELLED",
    )
    agent = AgentCompletionEvidence("reviewer", "reviewer", "1", "a" * 64, None, "later",
                                    status="failed", error_code="AGENT_EXECUTION_CANCELLED")
    if state == "completed":
        agent = replace(agent, status="succeeded", error_code=None, output_hash="b" * 64)
    elif state == "operational-failure":
        agent = replace(agent, error_code="AGENT_MODEL_UPSTREAM_UNAVAILABLE")
    evidence = replace(evidence, nodes=(*nodes, sibling), agents=(agent,), workflow_artifacts=(("child", child),))
    require_completion_evidence(target, artifact, command, evidence)
    for error in ("AGENT_MODEL_ACCOUNTING_RESULT_UNKNOWN", "AGENT_MODEL_ACCESS_DENIED", "AGENT_TASK_LEASE_LOST"):
        with pytest.raises(ValueError, match="safely terminal"):
            require_completion_evidence(target, artifact, command, replace(evidence, agents=(
                replace(agent, status="failed", output_hash=None, error_code=error),
            )))


def test_unavailable_agent_requires_matching_safe_terminal_evidence_when_child_exists():
    target, artifact, command, evidence = completion_case()
    artifact = artifact.model_copy(deep=True)
    artifact.definition["activities"][2]["on_unavailable"] = "continue"
    artifact.content_hash = artifact.fingerprint()
    target = replace(target, content_hash=artifact.content_hash)
    nodes = tuple(replace(node, scope=replace(node.scope, content_hash=artifact.content_hash),
                          **({"status": "unavailable", "output_hash": None, "error_code": "MODEL_UPSTREAM_TIMEOUT"}
                             if node.kind == "agent" else {})) for node in evidence.nodes)
    agent = replace(evidence.agents[0], status="failed", output_hash=None, error_code="MODEL_UPSTREAM_TIMEOUT")
    evidence = replace(evidence, nodes=nodes, agents=(agent,), unfinished_agent=True, unfinished_agent_step=True)
    require_completion_evidence(target, artifact, command, evidence)
    require_completion_evidence(target, artifact, command, replace(evidence, agents=()))
    for changes in ({"status": "running"}, {"error_code": "EXECUTION_NOT_AUTHORIZED"}, {"key": "wrong"},
                    {"output_hash": "a" * 64}):
        with pytest.raises(ValueError):
            require_completion_evidence(target, artifact, command, replace(evidence, agents=(replace(agent, **changes),)))


@pytest.mark.parametrize("error", ["MODEL_UPSTREAM_TIMEOUT", "TOOL_UPSTREAM_UNAVAILABLE"])
def test_failed_embedded_agent_accepts_exactly_one_storage_prefix_on_safe_operational_errors(error):
    target, artifact, command, evidence = completion_case()
    artifact = artifact.model_copy(deep=True)
    artifact.definition["activities"][2]["on_unavailable"] = "continue"
    artifact.content_hash = artifact.fingerprint()
    target = replace(target, content_hash=artifact.content_hash)
    nodes = tuple(replace(node, scope=replace(node.scope, content_hash=artifact.content_hash),
                          **({"status": "unavailable", "output_hash": None, "error_code": error}
                             if node.kind == "agent" else {})) for node in evidence.nodes)
    agent = replace(evidence.agents[0], status="failed", output_hash=None, error_code="AGENT_" + error)
    evidence = replace(evidence, nodes=nodes, agents=(agent,))
    require_completion_evidence(target, artifact, command, evidence)
    for rejected in (
        "AGENT_AGENT_" + error, "AGENT_MODEL_ACCOUNTING_RESULT_UNKNOWN", "AGENT_MODEL_NOT_AUTHORIZED",
        "AGENT_TOOL_NOT_AUTHORIZED", "AGENT_EXECUTION_NOT_AUTHORIZED", "AGENT_TASK_LEASE_LOST",
        "AGENT_MODEL_UPSTREAM_FUTURE_ERROR",
        "AGENT_" + ("TOOL_OUTPUT_INVALID" if error.startswith("MODEL_") else "MODEL_OUTPUT_INVALID"),
    ):
        with pytest.raises(ValueError, match="safely failed terminal child"):
            require_completion_evidence(target, artifact, command,
                                        replace(evidence, agents=(replace(agent, error_code=rejected),)))


def test_business_step_cannot_degrade_a_thrown_operational_error_to_unavailable():
    target, artifact, command, evidence = completion_case()
    artifact = artifact.model_copy(deep=True)
    artifact.definition["activities"][0]["on_unavailable"] = "continue"
    artifact.content_hash = artifact.fingerprint()
    target = replace(target, content_hash=artifact.content_hash)
    nodes = tuple(replace(node, scope=replace(node.scope, content_hash=artifact.content_hash),
                          **({"status": "unavailable", "output_hash": None, "error_code": "MODEL_UPSTREAM_TIMEOUT"}
                             if node.kind == "step" else {})) for node in evidence.nodes)
    with pytest.raises(ValueError, match="unavailable"):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=nodes))


def test_completion_limits_are_closed_and_global_node_bound_is_enforced():
    assert MAX_CODE_FRAME_DEPTH == 8 and MAX_CODE_NODES == 1000
    target, artifact, command, evidence = completion_case()
    with pytest.raises(ValueError, match="node bounds"):
        require_completion_evidence(target, artifact, command, replace(evidence, nodes=evidence.nodes * 201))


@pytest.mark.parametrize("depth,allowed", [(8, True), (9, False)])
def test_completion_bounds_actual_workflow_frame_depth(depth, allowed):
    artifacts = []
    for index in range(depth):
        activities = ([{"key": "delegate", "kind": "workflow", "capability": pin(f"flow{index + 1}")}]
                      if index < depth - 1 else [{"key": "prepare", "kind": "step"}])
        artifacts.append(published(f"flow{index}", "workflow", code_definition(activities),
                                   dependencies=[pin(f"flow{index + 1}")] if index < depth - 1 else []))
    command = CompletionCommand.prepare(status="succeeded", result={"done": True})
    nodes = []
    for index, item in enumerate(artifacts):
        scope = CodeNodeScope(item.key, "1", item.artifact.content_hash)
        frame_id = f"frame-{index}"
        parent_node_id = None if index == 0 else f"call-{index - 1}"
        nodes.append(NodeCompletionEvidence(frame_id, result_receipt_key(parent_node_id), "result", "succeeded",
                                            command.output_hash if index == 0 else "a" * 64,
                                            parent_node_id, scope))
        activity = item.artifact.definition["activities"][0]
        nodes.append(NodeCompletionEvidence(f"call-{index}", call_receipt_key(frame_id, activity["key"], text_hash("once")), activity["kind"], "succeeded",
                                            "a" * 64, frame_id,
                                            replace(scope, activity_key=activity["key"], invocation_hash=text_hash("once"))))
    root = artifacts[0]
    target = CompletionTarget(root.capability_id, "workflow", "1", root.artifact.content_hash, root.key)
    evidence = CompletionEvidence(nodes=tuple(nodes), workflow_artifacts=tuple(
        (item.key, item.artifact) for item in artifacts[1:]
    ))
    if allowed:
        require_completion_evidence(target, root.artifact, command, evidence)
    else:
        with pytest.raises(ValueError, match="depth"):
            require_completion_evidence(target, root.artifact, command, evidence)
