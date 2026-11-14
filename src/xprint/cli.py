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


def cmd_cad(args: argparse.Namespace) -> int:
    from .cad import run_script

    _, loaded, _ = _load(args)
    script = Path(args.script).expanduser()
    if not script.is_absolute():
        script = Path.cwd() / script
    workdir = Path(args.workdir).expanduser() if args.workdir else script.parent
    timeout = args.timeout or loaded.config.cad.timeout_s
    result = run_script(
        script,
        workdir=workdir,
        timeout_s=timeout,
        python=args.python or loaded.config.cad.python,
    )
    _print_json(result.to_dict())
    if not result.ok:
        print(
            result.hint or "the model did not build; see checks/stdout above",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def cmd_image(args: argparse.Namespace) -> int:
    from .images import ReferenceStore
    from .registry import builtin_registry

    ws, loaded, secrets = _load(args)
    configured = loaded.config.images
    provider_id = args.provider
    if not provider_id:
        provider_id = next(
            (name for name, item in sorted(configured.items()) if item.enabled), None
        )
    if not provider_id or provider_id not in configured:
        known = ", ".join(sorted(configured)) or "(none)"
        raise XPrintError(
            f"Unknown image provider: {provider_id or '(none)'}",
            hint=f"Configured image providers: {known}",
        )
    settings = configured[provider_id]
    key = secrets.provider_key(provider_id, api_key_env=settings.api_key_env)
    generator = builtin_registry().create(
        "image",
        settings.adapter,
        settings=settings.model_dump(),
        api_key=key,
        provider_id=provider_id,
    )
    image = generator.generate(args.prompt, size=args.size or settings.size)
    store = ReferenceStore(ws)
    meta = store.create(image, prompt=args.prompt, provider=provider_id, model=settings.model)
    image_path = store.directory(meta["reference_id"]) / meta["file"]
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image.data)
    _print_json(
        {
            "ok": True,
            "reference_id": meta["reference_id"],
            "image_path": ws.rel(image_path),
            "provider": provider_id,
            "model": meta["model"],
            "size": meta["size"],
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
        }
    )
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from .assets import AssetStore
    from .preview import DEFAULT_VIEWS, render_views

    ws, _, _ = _load(args)
    assets = AssetStore(ws)
    manifest = assets.load(args.asset_id)
    target = args.revision or assets.latest_revision(manifest)
    if not target:
        raise XPrintError(f"Asset {args.asset_id} has no revisions", hint="Call model_write first.")
    stl = assets.revision_dir(args.asset_id, target) / "model.stl"
    if not stl.is_file():
        raise XPrintError(
            f"Revision {target} has no built model",
            hint="Call model_build (or `xprint cad run`) first.",
        )
    views = tuple(item.strip().lower() for item in (args.views or "").split(",") if item.strip())
    png = render_views(
        stl,
        views=views or DEFAULT_VIEWS,
        width=args.width,
        height=args.height,
        caption=f"{manifest.get('name') or args.asset_id} {target}",
    )
    out = (
        Path(args.out).expanduser()
        if args.out
        else ws.root / "cache" / (f"previews/{args.asset_id}_{target}.png")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    _print_json(
        {
            "ok": True,
            "asset_id": args.asset_id,
            "revision": target,
            "image_path": str(out),
            "bytes": len(png),
        }
    )
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    import webbrowser

    from .assets import AssetStore
    from .viewer import (
        VIEWER_FILE,
        collection_html,
        entry_from_stl,
        write_legacy_redirect,
    )

    ws, _, _ = _load(args)
    assets = AssetStore(ws)
    manifest = assets.load(args.asset_id)
    target = args.revision or assets.latest_revision(manifest)
    if not target:
        raise XPrintError(f"Asset {args.asset_id} has no revisions", hint="Call model_write first.")
    stl = assets.revision_dir(args.asset_id, target) / "model.stl"
    if not stl.is_file():
        raise XPrintError(
            f"Revision {target} has no built model",
            hint="Call model_build (or `xprint cad run`) first.",
        )
    name = str(manifest.get("name") or args.asset_id)
    entries = []
    for item in manifest.get("revisions", []):
        revision = str(item.get("revision") or "")
        model = assets.revision_dir(args.asset_id, revision) / "model.stl"
        if not model.is_file():
            continue
        checks = (item.get("build") or {}).get("checks") or {}
        entries.append(
            entry_from_stl(
                model,
                revision=revision,
                created_at=str(item.get("created_at") or ""),
                volume_mm3=checks.get("volume_mm3"),
            )
        )
    if not entries:
        raise XPrintError(
            f"Asset {args.asset_id} has no built revisions",
            hint="Call model_build (or `xprint cad run`) first.",
        )
    path = (
        Path(args.out).expanduser() if args.out else assets.asset_dir(args.asset_id) / VIEWER_FILE
    )
    if path.parent.resolve() == assets.asset_dir(args.asset_id).resolve():
        for entry in entries:
            entry["stl"] = f"{entry['revision']}/model.stl"
    html = collection_html(
        entries,
        title=name,
        subtitle=f"{len(entries)} revisions · interactive 3D",
        name=name,
        selected=target,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    path.chmod(0o644)
    for entry in entries:
        legacy_dir = assets.revision_dir(args.asset_id, entry["revision"])
        if (legacy_dir / VIEWER_FILE).is_file():
            write_legacy_redirect(legacy_dir, title=name)
    result = {
        "path": path,
        "bytes": len(html.encode("utf-8")),
        "triangles": sum(entry["triangles"] for entry in entries),
        "revisions": len(entries),
    }
    path = path.resolve()
    if args.open:
        webbrowser.open(path.as_uri())
    _print_json(
        {
            "ok": True,
            "asset_id": args.asset_id,
            "revision": target,
            "viewer_path": str(path),
            "bytes": result["bytes"],
            "triangles": result["triangles"],
            "revisions": result["revisions"],
            "opened": bool(args.open),
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

    p = sub.add_parser("cad", parents=[common], help="run CAD scripts locally")
    csub = p.add_subparsers(dest="cad_command", required=True)
    cp = csub.add_parser("run", help="execute a build123d model.py in the sandbox")
    cp.add_argument("script", help="path to model.py")
    cp.add_argument("--workdir", default=None, help="output directory (default: script's dir)")
    cp.add_argument("--timeout", type=float, default=None, help="wall-clock seconds")
    cp.add_argument("--python", default=None, help="CAD interpreter (default: config.cad.python)")
    cp.set_defaults(func=cmd_cad)

    p = sub.add_parser("image", parents=[common], help="generate a concept image (text -> image)")
    p.add_argument("prompt", help="what the image should show")
    p.add_argument("--name", required=True, help="asset-style name, e.g. cable-clip")
    p.add_argument("--provider", default=None, help="image provider id (default: first enabled)")
    p.add_argument("--size", default=None, help="e.g. 1024x1024")
    p.add_argument("--out", default=None, help="copy the PNG to this path")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("preview", parents=[common], help="render a built model to PNG")
    p.add_argument("asset_id")
    p.add_argument("--revision", default="")
    p.add_argument("--views", default="", help="comma list: iso,front,top,right")
    p.add_argument("--width", type=int, default=900)
    p.add_argument("--height", type=int, default=700)
    p.add_argument("--out", default=None, help="output PNG (default: cache/previews/)")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("view", parents=[common], help="write an interactive 3D HTML viewer")
    p.add_argument("asset_id")
    p.add_argument("--revision", default="")
    p.add_argument("--out", default=None, help="output HTML (default: next to model.stl)")
    p.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    p.set_defaults(func=cmd_view)

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
