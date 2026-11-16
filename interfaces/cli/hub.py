"""Explicit business bundle inspection/publication; never installs executable code."""

import json
import os

from domain.capability import CapabilityDependency
from domain.governance import AccessDenied
from infra.schema_validation import IsolatedJSONValidator
from orchestration.hub_service import HubPublicationService


def _catalog():
    # Business packages are imported only by trusted process composition.
    from capability_hub.catalog import builtin_catalog

    return builtin_catalog()


def prepare_bundle(args):
    return _catalog().build(args.bundle, policy=CapabilityDependency(key=args.policy_key, version=args.policy_version))


def validate_selection(keys):
    return _catalog().select(keys)


def inspect_hub(args):
    result = _catalog().describe() if args.command == "hub-list" else prepare_bundle(args).export()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def publish_hub(args, services, bundle):
    auth = services.control_plane_auth
    principal = auth.authenticate(os.environ.get(args.api_key_env or "PLATFORM_API_KEY"))
    if principal is None or not principal.has_scope("admin"):
        raise AccessDenied("Hub publication requires an authenticated administrator")
    auth.allowed_resources(principal, args.project, "capability:write", "*")
    auth.allowed_resources(principal, args.project, "capability:publish", "*")
    result = HubPublicationService(services.store.capabilities, IsolatedJSONValidator()).publish(
        args.project, bundle, principal.name,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
