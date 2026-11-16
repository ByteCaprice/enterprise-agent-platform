"""Command line for the new platform, independent of historical launchers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re

from infra.database import build_engine
from infra.mcp_catalog import catalog_metadata, load_mcp_catalog
from infra.platform_migrations.runner import upgrade_platform
from infra.release_checks import check_installed_assets
from interfaces.platform_settings import load_settings
from interfaces.runtime import RuntimeServices, create_runtime
from interfaces.worker.callbacks import run_callback_worker
from interfaces.worker.platform import run_platform_worker, run_workflow_worker
from interfaces.worker.recovery import run_recovery_worker


def main(argv=None):
    parser = argparse.ArgumentParser(prog="enterprise-ai")
    parser.add_argument("command", choices=["serve", "export-openapi", "check-config", "check-release", "health", "check-worker", "migrate", "worker", "callback-worker", "recovery-worker", "check-mcp-catalog", "hub-list", "hub-export", "hub-publish", "skill-export", "skill-publish"])
    parser.add_argument("--config-dir", help="Explicit private configuration directory")
    parser.add_argument("--apply", action="store_true", help="Apply incremental migrations (migrate only)")
    parser.add_argument("--once", action="store_true", help="Claim at most one selected Task (worker only)")
    parser.add_argument("--worker-id", help="Trusted Worker identity, generated when omitted (worker only)")
    parser.add_argument("--mcp-catalog", help="Explicit operator-reviewed JSON catalog (worker/check-mcp-catalog only)")
    parser.add_argument("--executors", choices=["workflow", "agent", "workflow,agent"],
                        help="Explicit execution roles; default is workflow (worker only)")
    parser.add_argument("--workflow-agents", action="store_true", help="Explicitly install Agent nodes in Workflow (worker only)")
    parser.add_argument("--pi", action="store_true", help="Check local Node/Pi handshake without models (check-release only)")
    parser.add_argument("--project", help="Project key for health or capability publication")
    parser.add_argument("--reference-console", action="store_true", help="Mount the optional reference UI (serve only)")
    parser.add_argument("--hub-bundles", help="Explicit comma-separated reviewed business bundles to install (worker only)")
    parser.add_argument("--bundle", help="Reviewed business bundle key (hub-export/hub-publish only)")
    parser.add_argument("--policy-key", help="Existing published agent-turn model-policy key (hub-export/hub-publish only)")
    parser.add_argument("--policy-version", help="Fixed model-policy version (hub-export/hub-publish only)")
    parser.add_argument("--api-key-env", help="Environment variable holding an administrator Key; default PLATFORM_API_KEY (hub-publish/skill-publish)")
    parser.add_argument("--skill-file", help="Explicit SKILL.md file or its directory (skill-export/skill-publish)")
    parser.add_argument("--skill-key", help="Override the Skill frontmatter name as capability Key (skill-export/skill-publish)")
    parser.add_argument("--skill-version", help="Fixed Skill version; defaults to 1 (skill-export/skill-publish)")
    args = parser.parse_args(argv)
    if args.once and args.command not in {"worker", "callback-worker", "recovery-worker"}:
        parser.error("--once requires worker, callback-worker or recovery-worker")
    if args.worker_id is not None and args.command not in {"worker", "callback-worker", "recovery-worker", "check-worker"}:
        parser.error("--worker-id requires worker, callback-worker, recovery-worker or check-worker")
    if args.pi and args.command != "check-release":
        parser.error("--pi requires check-release")
    if args.reference_console and args.command != "serve":
        parser.error("--reference-console is only valid with serve")
    if args.project is not None and args.command not in {"health", "hub-publish", "skill-publish"}:
        parser.error("--project requires health, hub-publish or skill-publish")
    if args.command in {"health", "hub-publish", "skill-publish"} and not args.project:
        parser.error("This command requires --project")
    if args.command == "check-worker" and not args.worker_id:
        parser.error("check-worker requires --worker-id")
    if args.executors is not None and args.command != "worker":
        parser.error("--executors is only valid with worker")
    if args.workflow_agents and (args.command != "worker" or args.executors == "agent"):
        parser.error("--workflow-agents requires worker with the Workflow role")
    if args.hub_bundles is not None and args.command != "worker":
        parser.error("--hub-bundles is only valid with worker")
    if any(value is not None for value in (args.bundle, args.policy_key, args.policy_version)) and args.command not in {"hub-export", "hub-publish"}:
        parser.error("--bundle and model policy options require hub-export or hub-publish")
    if args.command in {"hub-export", "hub-publish"} and not all((args.bundle, args.policy_key, args.policy_version)):
        parser.error("Hub export/publication requires --bundle, --policy-key and --policy-version")
    if args.api_key_env is not None and (args.command not in {"hub-publish", "skill-publish"} or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", args.api_key_env) is None):
        parser.error("--api-key-env requires a publication command and a valid environment variable name")
    if any(value is not None for value in (args.skill_file, args.skill_key, args.skill_version)) and args.command not in {"skill-export", "skill-publish"}:
        parser.error("Skill options require skill-export or skill-publish")
    if args.command in {"skill-export", "skill-publish"} and not args.skill_file:
        parser.error("Skill import requires --skill-file")
    if args.apply and args.command != "migrate":
        parser.error("--apply is only valid with migrate")
    if args.mcp_catalog is not None and args.command not in {"worker", "check-mcp-catalog"}:
        parser.error("--mcp-catalog is only valid with worker or check-mcp-catalog")
    if args.command == "check-mcp-catalog" and args.mcp_catalog is None:
        parser.error("check-mcp-catalog requires --mcp-catalog")
    if args.command in {"export-openapi", "hub-list", "hub-export", "skill-export"} and args.config_dir is not None:
        parser.error("This offline command does not read deployment configuration")
    if args.config_dir is None and args.command not in {"check-release", "check-mcp-catalog", "export-openapi", "hub-list", "hub-export", "skill-export"}:
        parser.error("--config-dir is required; no default deployment environment is selected")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command in {"hub-list", "hub-export"}:
        from interfaces.cli.hub import inspect_hub

        try:
            inspect_hub(args)
        except Exception as exc:
            print(f"Hub inspection failed ({type(exc).__name__}); no configuration data displayed")
            raise SystemExit(1) from None
        return
    hub_bundle = None
    if args.command in {"skill-export", "skill-publish"}:
        from infra.skill_files import load_skill_bundle

        try:
            hub_bundle = load_skill_bundle(args.skill_file, key=args.skill_key,
                                           version="1" if args.skill_version is None else args.skill_version)
        except ValueError as exc:
            print(f"Skill import failed ({type(exc).__name__}); no configuration or Skill content displayed")
            raise SystemExit(1) from None
        if args.command == "skill-export":
            print(json.dumps(hub_bundle.export(), ensure_ascii=False, indent=2))
            return
    if args.command == "hub-publish" or args.hub_bundles is not None:
        from interfaces.cli.hub import prepare_bundle, validate_selection

        try:
            if args.command == "hub-publish":
                hub_bundle = prepare_bundle(args)
            else:
                validate_selection(tuple(args.hub_bundles.split(",")))
        except Exception as exc:
            print(f"Hub selection failed ({type(exc).__name__}); no deployment configuration was read")
            raise SystemExit(1) from None
    if args.command == "export-openapi":
        from interfaces.http.api_contract import export_openapi

        print(json.dumps(export_openapi(), ensure_ascii=False, indent=2))
        return
    if args.command == "check-release":
        try:
            result = check_installed_assets()
            if args.pi:
                from framework.agent_runtime.pi.adapter import PiRuntime

                asyncio.run(PiRuntime().check_ready())
                result["pi_handshake"] = "ok"
        except Exception as exc:
            print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
            raise SystemExit(1) from None
        print(json.dumps(result))
        return
    catalog = None
    if args.mcp_catalog is not None:
        try:
            catalog = load_mcp_catalog(args.mcp_catalog)
        except ValueError:
            print("Invalid MCP deployment catalog; no configuration data displayed")
            raise SystemExit(1) from None
    if args.command == "check-mcp-catalog":
        assert catalog is not None
        print(json.dumps({"tools": catalog_metadata(catalog)}))
        return
    if args.command == "migrate":
        database = load_settings(args.config_dir).get("database", {})
        engine = build_engine(database)
        try:
            result = upgrade_platform(engine, database.get("schema"), apply=args.apply)
        except Exception as exc:
            # Driver errors can contain database credentials or customer input.
            print(f"Platform upgrade failed ({type(exc).__name__}); no credentials displayed")
            raise SystemExit(1) from None
        finally:
            engine.dispose()
        print(json.dumps(result))
        return
    try:
        runtime = create_runtime(args.config_dir)
    except Exception as exc:
        print(f"Platform initialization failed ({type(exc).__name__}); no configuration data displayed")
        raise SystemExit(1) from None
    try:
        _run_runtime_command(args, runtime, catalog, hub_bundle=hub_bundle)
    finally:
        runtime.close()


def _run_runtime_command(args, services: RuntimeServices, catalog, *, hub_bundle=None):
    if args.command in {"hub-publish", "skill-publish"}:
        from interfaces.cli.hub import publish_hub

        try:
            publish_hub(args, services, hub_bundle)
        except Exception as exc:
            print(f"Capability publication failed ({type(exc).__name__}); no configuration or business data displayed")
            raise SystemExit(1) from None
        return
    if args.command == "check-config":
        print(json.dumps({"status": "ok", "mode": services.runtime_settings.mode,
                          "schema_and_auth": "verified", "rate_limits": "process-local; shared edge required in production"}))
        return
    if args.command in {"health", "check-worker"}:
        try:
            runtime = services.runtime_settings
            if args.command == "health":
                result = services.store.worker_health.summary(args.project,
                    stale_seconds=runtime.worker_stale_seconds, queue_warning_seconds=runtime.queue_warning_seconds)
                healthy = result["status"] == "ok"
            else:
                workers = services.store.worker_health.live(stale_seconds=runtime.worker_stale_seconds, worker_id=args.worker_id)
                healthy = bool(workers)
                result = {"status": "ok" if healthy else "missing", "worker_id": args.worker_id,
                          "heartbeat_is_advisory": True}
            print(json.dumps(result, default=str))
        except Exception as exc:
            print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__}))
            raise SystemExit(2) from None
        if not healthy:
            raise SystemExit(1)
        return
    if args.command == "recovery-worker":
        try:
            result = asyncio.run(run_recovery_worker(services.store, once=args.once, worker_id=args.worker_id))
            if args.once:
                print(json.dumps(result))
        except Exception as exc:
            print(f"Recovery worker stopped ({type(exc).__name__}); no task data displayed")
            raise SystemExit(1) from None
        return
    if args.command == "callback-worker":
        try:
            delivered = asyncio.run(run_callback_worker(services.store, once=args.once, worker_id=args.worker_id))
            if args.once:
                print(json.dumps({"claimed": delivered, "executor": "callback-delivery"}))
        except Exception as exc:
            print(f"Callback worker stopped ({type(exc).__name__}); no delivery data displayed")
            raise SystemExit(1) from None
        return
    if args.command == "worker":
        try:
            kinds = tuple((args.executors or "workflow").split(","))
            hub_bundles = tuple(args.hub_bundles.split(",")) if args.hub_bundles is not None else ()
            hub_options = {"hub_bundles": hub_bundles} if args.hub_bundles is not None else {}
            if kinds == ("workflow",) and not args.workflow_agents:
                claimed = asyncio.run(run_workflow_worker(services.store, worker_id=args.worker_id, once=args.once,
                                                          mcp_catalog=catalog, **hub_options))
            else:
                claimed = asyncio.run(run_platform_worker(services.store, worker_id=args.worker_id, once=args.once,
                                                          mcp_catalog=catalog, execution_kinds=kinds,
                                                          workflow_agents=args.workflow_agents, **hub_options))
            if args.once:
                print(json.dumps({"claimed": claimed, "executor": "workflow-rules-llm-tools" if kinds == ("workflow",)
                                 else ",".join(kinds), "workflow_agents": args.workflow_agents}))
        except Exception as exc:
            print(f"Platform worker stopped ({type(exc).__name__}); no execution data displayed")
            raise SystemExit(1) from None
        return
    import uvicorn

    from interfaces.http.platform_server import create_platform_app

    try:
        app = create_platform_app(runtime=services, include_reference_console=args.reference_console)
    except Exception as exc:
        print(f"Platform initialization failed ({type(exc).__name__}); no configuration data displayed")
        raise SystemExit(1) from None
    settings = services.bootstrap.get("server", {})
    uvicorn.run(app, host=settings.get("host", "127.0.0.1"), port=int(settings.get("port", 50020)), access_log=False)


if __name__ == "__main__":
    main()
