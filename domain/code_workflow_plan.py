"""Declared code Workflow activities, explicitly not a static control-flow graph."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.business_code import (
    CODE_WORKFLOW_CHECKPOINT_PROFILE,
    CODE_WORKFLOW_INTERPRETER_PROFILE,
    CODE_WORKFLOW_RESULT_KEY,
    CodeActivity,
    CodeReference,
)
from domain.capability import Key, Version


class CodeWorkflowPlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_key: Key
    capability_key: Key
    version: Version
    content_hash: str
    compiler_profile: Literal["enterprise-ai.workflow.code.plan.v1"] = "enterprise-ai.workflow.code.plan.v1"
    interpreter_profile: Literal[CODE_WORKFLOW_INTERPRETER_PROFILE] = CODE_WORKFLOW_INTERPRETER_PROFILE
    checkpoint_profile: Literal[CODE_WORKFLOW_CHECKPOINT_PROFILE] = CODE_WORKFLOW_CHECKPOINT_PROFILE
    implementation: CodeReference
    control_flow: Literal["code"] = "code"
    required_adapters: list[Literal["agent", "tool", "llm"]]
    activities: tuple[CodeActivity, ...] = Field(min_length=1, max_length=99)
    max_parallelism: int = Field(default=4, strict=True, ge=1, le=16)
    result_receipt_key: Literal[CODE_WORKFLOW_RESULT_KEY] = CODE_WORKFLOW_RESULT_KEY
    requires_runtime_validation: Literal[True] = True
    supports_resume: Literal[False] = False
