"""MCP server wiring: transports, auth, and the M0 tool surface.

The server is intentionally thin.  Tool implementations live in
:mod:`xprint.tools` and receive a :class:`Runtime`, so they can be unit tested
without speaking MCP.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .assets import AssetStore, validate_name
from .capabilities import build_capabilities, build_system_status
from .config import LoadedConfig, Secrets, load_all
from .errors import (
    AdapterUnavailable,
    ConfigError,
    ConfirmationMismatch,
    InvalidArgument,
    XPrintError,
    error_payload,
)
from .jobs import Job, JobStore
from .registry import Registry, builtin_registry
from .script_policy import check_script, summarise
from .workspace import Workspace

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

INSTRUCTIONS = """\
XPrint is a self-hosted bridge between MCP agents and the 3D-printing tools on
this machine.

Safety contract:
- Read-only tools (capabilities, system_status, job_status) run without
  confirmation.
- Every tool that writes files or talks to paid services requires the exact
  `confirm` string documented in its schema.  A wrong string is rejected with
  code `confirmation_mismatch`, nothing is written.
- XPrint never sends arbitrary G-code to a printer; slicing output is inspected
  and confirmed first.

Start with `capabilities` to learn which providers, CAD backends, slicers and
printers are available on this host.
"""


@dataclass
class Runtime:
    """Everything a tool handler needs, resolved once per process."""

    workspace: Workspace
    loaded: LoadedConfig
    secrets: Secrets
    store: JobStore
    registry: Registry
    writes: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        workspace: Workspace | None = None,
        config_path: str | os.PathLike[str] | None = None,
        registry: Registry | None = None,
    ) -> Runtime:
        ws = workspace or Workspace()
        loaded, secrets = load_all(path=config_path, workspace=ws.root)
        return cls(
            workspace=ws,
            loaded=loaded,
            secrets=secrets,
            store=JobStore(ws),
            registry=registry or builtin_registry(),
        )

    def record_write(self, what: str) -> None:
        self.writes.append(what)


def tool_error(exc: BaseException) -> dict[str, Any]:
    """Convert an exception into the unified error envelope."""

    if isinstance(exc, XPrintError):
        return {"ok": False, "writes_performed": False, "error": error_payload(exc)}
    return {
        "ok": False,
        "writes_performed": False,
        "error": error_payload(
            XPrintError(str(exc) or exc.__class__.__name__, code="internal_error")
        ),
    }


def job_payload(job: Job) -> dict[str, Any]:
    data = job.to_dict()
    # Keep the contract stable: durations are computed, not stored.
    data["duration_seconds"] = job.duration_seconds
    return data


def build_server(runtime: Runtime | None = None, *, name: str = "xprint") -> FastMCP:
    """Create the FastMCP application with every M0 tool registered."""

    rt = runtime or Runtime.create()
    host = rt.loaded.config.server.host
    port = rt.loaded.config.server.port

    server = FastMCP(
        name=name,
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
    )
    # FastMCP otherwise advertises the ``mcp`` package version; hosts show this
    # string in their server list, so report XPrint's own version instead.
    if hasattr(server, "_mcp_server"):
        server._mcp_server.version = __version__

    @server.tool(
        name="capabilities",
        description=(
            "Report what this XPrint installation can do right now: cloud image-to-3D "
            "providers (key present or not), CAD/render backends, slicer profiles and "
            "printers, plus the adapters compiled in. Never returns API keys and never "
            "writes files."
        ),
    )
    def capabilities() -> dict[str, Any]:
        return build_capabilities(rt.loaded, rt.secrets, rt.registry)

    @server.tool(
        name="system_status",
        description=(
            "Report XPrint version, Python/platform, workspace paths, config/secrets "
            "presence and job counters. Read-only: writes_performed is always false."
        ),
    )
    def system_status() -> dict[str, Any]:
        return build_system_status(rt.loaded, rt.secrets, rt.workspace, rt.store, rt.registry)

    @server.tool(
        name="model_generate",
        description=(
            "Ask an LLM modeler (Kimi K3.1 Pro by default) to write a parametric "
            "build123d script from a text prompt and/or a reference photo, validate it "
            "against XPrint's script policy, and store it as a new asset revision. "
            "WRITES files and calls a paid API: confirm must be exactly "
            "'MODEL <name> <modeler>'. The script is not executed here; build it with "
            "the CAD backend or review it first."
        ),
    )
    def model_generate(
        prompt: str,
        name: str,
        confirm: str,
        image: str | None = None,
        modeler: str = "kimi",
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"MODEL {name} {modeler}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )

            settings = rt.loaded.config.modelers.get(modeler)
            if settings is None:
                known = ", ".join(sorted(rt.loaded.config.modelers)) or "(none)"
                raise InvalidArgument(
                    f"Unknown modeler: {modeler}",
                    hint=f"Configured modelers: {known}",
                )
            if not settings.enabled:
                raise AdapterUnavailable(
                    f"Modeler {modeler!r} is disabled in config",
                    hint=f"Set config.modelers.{modeler}.enabled = true.",
                )

            key = rt.secrets.provider_key(modeler, api_key_env=settings.api_key_env)
            adapter = rt.registry.create(
                "modeler",
                modeler,
                settings=settings.model_dump(),
                api_key=key,
            )

            image_path = None
            if image:
                image_path = rt.workspace.safe_path(image, must_exist=True)

            generated = adapter.generate(prompt, image=image_path)
            checks = summarise(check_script(generated.code))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The modeler returned a script that failed XPrint's safety checks",
                    hint=(
                        "Rephrase the prompt or retry; nothing was stored. "
                        "See details.issues for the rejected line numbers."
                    ),
                    details={
                        "model": generated.model,
                        "usage": generated.usage,
                        "issues": checks["errors"],
                    },
                )

            assets = AssetStore(rt.workspace)
            manifest = assets.create(name)
            record = assets.add_revision(
                manifest["asset_id"],
                generated.code,
                prompt=prompt.strip(),
                model=generated.model,
                modeler=modeler,
                usage=generated.usage,
                checks=checks,
            )
            rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.py")
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "name": name,
                "revision": record["revision"],
                "script_path": rt.workspace.rel(
                    assets.revision_dir(manifest["asset_id"], record["revision"]) / "model.py"
                ),
                "sha256": record["sha256"],
                "model": generated.model,
                "modeler": modeler,
                "usage": generated.usage,
                "checks": checks,
                "hint": (
                    "Review the script, then build it with the CAD backend "
                    "(`cad run model.py`) once text-to-cad is configured."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="asset_list",
        description="List generated model assets with their revision counts. Read-only.",
    )
    def asset_list(limit: int = 20) -> dict[str, Any]:
        try:
            if not 1 <= int(limit) <= 200:
                raise InvalidArgument("limit must be between 1 and 200")
            return {
                "ok": True,
                "writes_performed": False,
                "assets": AssetStore(rt.workspace).list(limit=int(limit)),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="job_status",
        description=(
            "Look up one asynchronous job by id (j_<timestamp>_<hex>). Returns state, "
            "progress, timestamps, result and artifacts. Read-only."
        ),
    )
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            if not isinstance(job_id, str) or not job_id.strip():
                raise InvalidArgument(
                    "job_id must be a non-empty string",
                    hint="Use the id returned when the job was created.",
                )
            job = rt.store.load(job_id.strip())
            return {
                "ok": True,
                "writes_performed": False,
                "job": job_payload(job),
            }
        except XPrintError as exc:
            return tool_error(exc)

    return server


def _bearer_token(loaded: LoadedConfig, secrets: Secrets) -> str | None:
    env_name = loaded.config.server.token_env or "XPRINT_TOKEN"
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    token = str(secrets.section("server").get("token") or "").strip()
    return token or None


class BearerAuth:
    """Minimal ASGI middleware: require ``Authorization: Bearer <token>``."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        supplied = headers.get("authorization", "")
        if supplied != f"Bearer {self.token}":
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "missing or invalid bearer token",
                        "hint": "Send Authorization: Bearer <token> using the value from XPRINT_TOKEN or secrets.json server.token.",
                        "details": {},
                    },
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="xprint"'),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def serve(
    runtime: Runtime, *, transport: str = "stdio", host: str | None = None, port: int | None = None
) -> None:
    """Run the server over stdio or streamable HTTP.

    Non-loopback HTTP binds require a bearer token, otherwise we refuse to start:
    an unauthenticated CAD/printer bridge on the LAN is exactly the failure mode
    the design's security section calls out.
    """

    server = build_server(runtime)
    if transport == "stdio":
        server.run("stdio")
        return

    if transport not in {"http", "streamable-http"}:
        raise ConfigError(
            f"unknown transport {transport!r}",
            hint="Use `xprint serve --stdio` or `xprint serve --http`.",
        )

    cfg = runtime.loaded.config.server
    bind_host = host or cfg.host
    bind_port = port or cfg.port
    token = _bearer_token(runtime.loaded, runtime.secrets)

    if bind_host not in LOOPBACK_HOSTS and not token:
        raise ConfigError(
            f"refusing to bind {bind_host} without a bearer token",
            hint=(
                "Set XPRINT_TOKEN (or server.token in secrets.json) before exposing "
                "XPrint beyond loopback."
            ),
        )

    app = server.streamable_http_app()
    if token:
        app.add_middleware(BearerAuth, token=token)

    import uvicorn

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - convenience
    from .cli import main as cli_main

    return cli_main(argv if argv is not None else sys.argv[1:])


__all__ = [
    "Runtime",
    "build_server",
    "serve",
    "tool_error",
    "job_payload",
]
