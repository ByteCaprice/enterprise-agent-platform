"""Select a closed Workflow interpreter without changing Task execution ownership."""

from domain.business_code import CodeWorkflowDefinition
from domain.capability_definitions import interpret_definition
from orchestration.code_workflow import CodeWorkflowExecutor
from orchestration.workflow_executor import WorkflowExecutor


class WorkflowDispatcher:
    def __init__(self, declarative: WorkflowExecutor, code: CodeWorkflowExecutor | None = None, *, tool_implementations=()):
        self._declarative = declarative
        self._code = code
        self._tools = tuple(tool_implementations)

    @property
    def interpreter_profile(self):
        return self._declarative.interpreter_profile

    @property
    def checkpoint_profile(self):
        return self._declarative.checkpoint_profile

    @property
    def adapter_descriptors(self):
        return self._declarative.adapter_descriptors

    @property
    def additional_profiles(self):
        return self._code.additional_profiles if self._code is not None else ()

    @property
    def implementation_references(self):
        return tuple(dict.fromkeys((*self.tool_implementation_references, *self.workflow_implementation_references)))

    @property
    def workflow_implementation_references(self):
        return self._code.workflow_implementation_references if self._code is not None else ()

    @property
    def tool_implementation_references(self):
        return self._tools

    async def __call__(self, claim):
        from domain.task_execution import ExecutionFailure

        definition = interpret_definition("workflow", claim.artifact).materialize()
        if isinstance(definition, CodeWorkflowDefinition):
            if self._code is None:
                raise ExecutionFailure("WORKFLOW_CODE_UNAVAILABLE")
            return await self._code(claim)
        return await self._declarative(claim)
