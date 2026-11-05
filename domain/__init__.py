"""Domain exports; individual models are loaded only when requested."""

from importlib import import_module

_MODULES = {
    "api_key_config": ("ApiKeyConfig",),
    "ids": ("new_event_id", "new_trace_id", "utc_now"),
    "log_event": ("LogEvent",),
}
_EXPORTS = {name: module for module, names in _MODULES.items() for name in names}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{module}"), name)
