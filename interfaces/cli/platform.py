"""Command line for the new platform, independent of historical launchers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import uvicorn

from infra.database import build_engine
from infra.mcp_catalog import catalog_metadata, load_mcp_catalog
from infra.platform_migrations.runner import upgrade_platform
from infra.release_checks import check_installed_assets
from interfaces.http.platform_server import create_platform_app
from interfaces.platform_settings import load_settings
from interfaces.worker.callbacks import run_callback_worker
from interfaces.worker.platform import run_platform_worker, run_workflow_worker
from interfaces.worker.recovery import run_recovery_worker


def main(argv=None):
    parser = argparse.ArgumentParser(prog="enterprise-ai")
    parser.add_argument("command", choices=["serve", "export-openapi", "check-config", "check-release", "health", "check-worker", "migrate", "worker", "callback-worker", "recovery-worker", "check-mcp-catalog"])
    parser.add_argument("--config-dir", help="Explicit private configuration directory")
    parser.add_argument("--apply", action="store_true", help="Apply incremental migrations (migrate only)")
    parser.add_argument("--once", action="store_true", help="Claim at most one selected Task (worker only)")
    parser.add_argument("--worker-id", help="Trusted Worker identity, generated when omitted (worker only)")
    parser.add_argument("--mcp-catalog", help="Explicit operator-reviewed JSON catalog (worker/check-mcp-catalog only)")
    parser.add_argument("--executors", choices=["workflow", "agent", "workflow,agent"],
                        help="Explicit execution roles; default is workflow (worker only)")
    parser.add_argument("--workflow-agents", action="store_true", help="Explicitly install Agent nodes in Workflow (worker only)")
    parser.add_argument("--pi", action="store_true", help="Check local Node/Pi handshake without models (check-release only)")
    parser.add_argument("--project", help="Project health summary (health only)")
    parser.add_argument("--reference-console", action="store_true", help="Mount the optional reference UI (serve only)")
    args = parser.parse_args(argv)
    if args.once and args.command not in {"worker", "callback-worker", "recovery-worker"}:
        parser.error("--once requires worker, callback-worker or recovery-worker")
    if args.worker_id is not None and args.command not in {"worker", "callback-worker", "recovery-worker", "check-worker"}:
        parser.error("--worker-id requires worker, callback-worker, recovery-worker or check-worker")
    if args.pi and args.command != "check-release":
        parser.error("--pi requires check-release")
    if args.reference_console and args.command != "serve":
        parser.error("--reference-console is only valid with serve")
    if args.project is not None and args.command != "health":
        parser.error("--project requires health")
    if args.command == "health" and not args.project:
        parser.error("health requires --project")
    if args.command == "check-worker" and not args.worker_id:
        parser.error("check-worker requires --worker-id")
    if args.executors is not None and args.command != "worker":
        parser.error("--executors is only valid with worker")
    if args.workflow_agents and (args.command != "worker" or args.executors == "agent"):
        parser.error("--workflow-agents requires worker with the Workflow role")
    if args.apply and args.command != "migrate":
        parser.error("--apply is only valid with migrate")
    if args.mcp_catalog is not None and args.command not in {"worker", "check-mcp-catalog"}:
        parser.error("--mcp-catalog is only valid with worker or check-mcp-catalog")
    if args.command == "check-mcp-catalog" and args.mcp_catalog is None:
        parser.error("check-mcp-catalog requires --mcp-catalog")
    if args.command == "export-openapi" and args.config_dir is not None:
        parser.error("export-openapi does not read deployment configuration")
    if args.config_dir is None and args.command not in {"check-release", "check-mcp-catalog", "export-openapi"}:
        parser.error("--config-dir is required; no default deployment environment is selected")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
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
        app = (create_platform_app(args.config_dir, include_reference_console=True) if args.reference_console else
               create_platform_app(args.config_dir))
    except Exception as exc:
        print(f"Platform initialization failed ({type(exc).__name__}); no configuration data displayed")
        raise SystemExit(1) from None
    if args.command == "check-config":
        print(json.dumps({"status": "ok", "mode": app.state.platform_runtime.mode,
                          "schema_and_auth": "verified", "rate_limits": "process-local; shared edge required in production"}))
        app.state.store.close()
        return
    if args.command in {"health", "check-worker"}:
        try:
            runtime = app.state.platform_runtime
            if args.command == "health":
                result = app.state.store.worker_health.summary(args.project,
                    stale_seconds=runtime.worker_stale_seconds, queue_warning_seconds=runtime.queue_warning_seconds)
                healthy = result["status"] == "ok"
            else:
                workers = app.state.store.worker_health.live(stale_seconds=runtime.worker_stale_seconds, worker_id=args.worker_id)
                healthy = bool(workers)
                result = {"status": "ok" if healthy else "missing", "worker_id": args.worker_id,
                          "heartbeat_is_advisory": True}
            print(json.dumps(result, default=str))
        except Exception as exc:
            print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__}))
            raise SystemExit(2) from None
        finally:
            app.state.store.close()
        if not healthy:
            raise SystemExit(1)
        return
    if args.command == "recovery-worker":
        try:
            result = asyncio.run(run_recovery_worker(app.state.store, once=args.once, worker_id=args.worker_id))
            if args.once:
                print(json.dumps(result))
        except Exception as exc:
            print(f"Recovery worker stopped ({type(exc).__name__}); no task data displayed")
            raise SystemExit(1) from None
        finally:
            app.state.store.close()
        return
    if args.command == "callback-worker":
        try:
            delivered = asyncio.run(run_callback_worker(app.state.store, once=args.once, worker_id=args.worker_id))
            if args.once:
                print(json.dumps({"claimed": delivered, "executor": "callback-delivery"}))
        except Exception as exc:
            print(f"Callback worker stopped ({type(exc).__name__}); no delivery data displayed")
            raise SystemExit(1) from None
        finally:
            app.state.store.close()
        return
    if args.command == "worker":
        try:
            kinds = tuple((args.executors or "workflow").split(","))
            if kinds == ("workflow",) and not args.workflow_agents:
                claimed = asyncio.run(run_workflow_worker(app.state.store, worker_id=args.worker_id, once=args.once,
                                                          mcp_catalog=catalog))
            else:
                claimed = asyncio.run(run_platform_worker(app.state.store, worker_id=args.worker_id, once=args.once,
                                                          mcp_catalog=catalog, execution_kinds=kinds,
                                                          workflow_agents=args.workflow_agents))
            if args.once:
                print(json.dumps({"claimed": claimed, "executor": "workflow-rules-llm-tools" if kinds == ("workflow",)
                                 else ",".join(kinds), "workflow_agents": args.workflow_agents}))
        except Exception as exc:
            print(f"Platform worker stopped ({type(exc).__name__}); no execution data displayed")
            raise SystemExit(1) from None
        finally:
            app.state.store.close()
        return
    settings = app.state.bootstrap.get("server", {})
    uvicorn.run(app, host=settings.get("host", "127.0.0.1"), port=int(settings.get("port", 50020)), access_log=False)


if __name__ == "__main__":
    main()
