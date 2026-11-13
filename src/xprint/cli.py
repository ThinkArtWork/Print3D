"""Command line interface.

``xprint`` is the operator-facing surface: it starts the MCP server, prints
capabilities, and manages the workspace.  Every command prints JSON unless it
is a human-only convenience (``init``, ``serve``), so scripts can pipe it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .capabilities import build_capabilities, build_system_status
from .config import Secrets, load_all, write_config
from .errors import XPrintError, error_payload
from .jobs import JobStore
from .registry import builtin_registry
from .workspace import Workspace

DEFAULT_WORKSPACE = "~/.xprint"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace(args.workspace or DEFAULT_WORKSPACE)


def _load(args: argparse.Namespace) -> tuple[Workspace, Any, Secrets]:
    ws = _workspace(args)
    loaded, secrets = load_all(path=args.config, workspace=ws.root)
    return ws, loaded, secrets


# --------------------------------------------------------------------------- #
# commands


def cmd_init(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    target = Path(args.config).expanduser() if args.config else ws.config_path
    if target.exists() and not args.force:
        print(f"config already exists: {target} (use --force to overwrite)", file=sys.stderr)
        return 1
    loaded, _ = load_all(path=None, workspace=ws.root)
    loaded.config.workspace = str(ws.root)
    loaded.path = target
    write_config(loaded, force=args.force)
    print(
        json.dumps(
            {
                "ok": True,
                "workspace": str(ws.root),
                "config": str(target),
                "created": True,
            }
        )
    )
    print(
        "next: add API keys to secrets.json (see README) and run `xprint capabilities`",
        file=sys.stderr,
    )
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    _, loaded, secrets = _load(args)
    _print_json(build_capabilities(loaded, secrets, builtin_registry()))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws, loaded, secrets = _load(args)
    store = JobStore(ws)
    _print_json(build_system_status(loaded, secrets, ws, store, builtin_registry()))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    import asyncio

    from .server import Runtime, build_server

    runtime = Runtime.create(workspace=_workspace(args), config_path=args.config)
    server = build_server(runtime)
    tools = sorted(asyncio.run(server.list_tools()), key=lambda t: t.name)
    _print_json(
        {
            "ok": True,
            "count": len(tools),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
                for t in tools
            ],
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import Runtime, serve

    runtime = Runtime.create(workspace=_workspace(args), config_path=args.config)
    transport = "stdio" if args.stdio or not args.http else "http"
    serve(runtime, transport=transport, host=args.host, port=args.port)
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    ws, _, _ = _load(args)
    store = JobStore(ws)
    if args.job_command == "list":
        _print_json(
            {
                "ok": True,
                "jobs": [j.to_dict() for j in store.list(limit=args.limit)],
            }
        )
        return 0
    job = store.load(args.job_id)
    _print_json({"ok": True, "job": job.to_dict()})
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    _, loaded, secrets = _load(args)
    # config.json holds env-var *names*, never key material; no redaction needed.
    _print_json(
        {
            "ok": True,
            "path": str(loaded.path) if loaded.path else None,
            "exists": loaded.exists,
            "config": loaded.config.model_dump(mode="json"),
            "warnings": loaded.warnings,
            "secrets": {
                "path": str(secrets.path) if secrets.path else None,
                "present": bool(secrets.path and secrets.path.exists()),
                "warnings": secrets.warnings,
            },
        }
    )
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    _print_json({"ok": True, "name": "xprint", "version": __version__})
    return 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xprint",
        description="Self-hosted MCP bridge for 3D-printing tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace", default=os.environ.get("XPRINT_WORKSPACE", DEFAULT_WORKSPACE)
    )
    common.add_argument("--config", default=None, help="explicit config.json path")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="create workspace and default config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("capabilities", parents=[common], help="print capability report (JSON)")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("status", parents=[common], help="print system status (JSON)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("config", parents=[common], help="print resolved config (keys redacted)")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("tools", parents=[common], help="print registered MCP tools (JSON)")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("serve", parents=[common], help="run the MCP server")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="stdio transport (default)")
    mode.add_argument("--http", action="store_true", help="streamable HTTP transport")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("job", parents=[common], help="inspect jobs")
    jsub = p.add_subparsers(dest="job_command", required=True)
    jp = jsub.add_parser("list", help="list recent jobs")
    jp.add_argument("--limit", type=int, default=20)
    jp.set_defaults(func=cmd_job)
    jp = jsub.add_parser("show", help="show one job")
    jp.add_argument("job_id")
    jp.set_defaults(func=cmd_job)

    p = sub.add_parser("version", parents=[common], help="print version")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except XPrintError as exc:
        _print_json({"ok": False, "error": error_payload(exc)})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
