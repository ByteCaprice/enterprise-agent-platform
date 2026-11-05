"""ORM exports, loaded by owning metadata rather than importing every model."""

from importlib import import_module

from infra.store.model.base import AUTO_PK, JSON_VARIANT, PlatformBase

_PLATFORM = {
    "ai_api_key_config": ("AiApiKeyConfig",),
    "ai_capability": ("AiCapability", "AiCapabilityVersion"),
    "ai_governance": ("AiProject", "AiResourceGrant", "AiServiceAccessKey", "AiServiceIdentity"),
    "ai_gateway_request": ("AiGatewayRequest",),
    "ai_model_provider": ("AiPlatformModelProvider",),
    "ai_platform_agent": ("AiAgentExecution", "AiAgentStep"),
    "ai_platform_model_call": ("AiPlatformModelCall",),
    "ai_platform_tool": ("AiPlatformToolBinding", "AiPlatformToolCall"),
    "ai_run_log": ("AiRunLog",),
    "ai_task": ("AiTask",),
    "ai_task_attempt": ("AiTaskAttempt",),
    "ai_task_event": ("AiTaskEvent",),
    "ai_workflow_node": ("AiWorkflowNode",),
    "ai_operations": (),
    "ai_worker_health": (),
}
_EXPORTS = {name: module for module, names in _PLATFORM.items() for name in names}
__all__ = ["AUTO_PK", "JSON_VARIANT", "PlatformBase", "platform_metadata", *_EXPORTS]


def __getattr__(name: str):
    if name == "platform_metadata":
        for module in _PLATFORM:
            import_module(f"{__name__}.{module}")
        return PlatformBase.metadata
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{module}"), name)
