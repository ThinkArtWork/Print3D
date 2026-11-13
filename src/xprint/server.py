"""MCP server wiring: transports, auth, and the M0 tool surface.

The server is intentionally thin.  Tool implementations live in
:mod:`xprint.tools` and receive a :class:`Runtime`, so they can be unit tested
without speaking MCP.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .agent import ModelingAgent
from .assets import AssetStore, validate_asset_id, validate_name
from .cad import run_script
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

Two modelling paths, both versioned as assets:

1. Bring your own model (no LLM inside XPrint): you write a build123d script,
   call `model_write` to store it, `model_build` to execute it and read the real
   geometry checks, then revise with another `model_write`/`model_build` pair
   until the part is watertight.  `model_show` returns the stored script.
2. Let XPrint drive a modeler: `model_generate` runs a closed loop with the
   configured modeler (Kimi by default), which needs an API key.
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
            "Ask an LLM modeler (Kimi K3.1 Pro by default) to design a parametric "
            "build123d part from a text prompt and/or a reference photo. The modeler "
            "runs a closed loop: it writes a script, XPrint executes it in a sandbox, "
            "returns real geometry checks, and the modeler revises until printable. "
            "The new asset revision stores model.py, STL/STEP artifacts, build checks "
            "and the full agent_trace.json. WRITES files, executes generated code in a "
            "limited subprocess and calls a paid API: confirm must be exactly "
            "'MODEL <name> <modeler>'."
        ),
    )
    def model_generate(
        prompt: str,
        name: str,
        confirm: str,
        image: str | None = None,
        modeler: str = "kimi",
        rounds: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"MODEL {name} {modeler}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not isinstance(prompt, str) or not prompt.strip():
                raise InvalidArgument(
                    "prompt must not be empty",
                    hint="Describe the part, its purpose and any dimensions in millimetres.",
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

            max_rounds = int(rounds or settings.max_rounds)
            if not 1 <= max_rounds <= 10:
                raise InvalidArgument(
                    "rounds must be between 1 and 10",
                    hint="Each round is one model completion plus any tool calls.",
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

            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/agents/agent_{secrets.token_hex(6)}", create_parents=True
            )
            modeler_loop = ModelingAgent(
                adapter,
                workdir,
                max_rounds=max_rounds,
                cad_timeout_s=cad.timeout_s,
                python=cad.python,
            )
            result = modeler_loop.run(prompt, image=image_path)

            if result.script is None:
                raise InvalidArgument(
                    "The modeler finished without writing a script",
                    hint="Retry with a more concrete prompt; inspect details.steps.",
                    details={
                        "usage": result.usage,
                        "stopped_reason": result.stopped_reason,
                        "steps": result.trace[-6:],
                    },
                )

            checks = summarise(check_script(result.script))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The modeler returned a script that failed XPrint's safety checks",
                    hint="Retry; nothing was stored. See details.issues for line numbers.",
                    details={
                        "model": settings.model,
                        "usage": result.usage,
                        "issues": checks["errors"],
                    },
                )

            artifact_files: dict[str, Path] = {}
            if result.build:
                for value in result.build.artifacts.values():
                    path = Path(value)
                    if path.is_file():
                        artifact_files[f"model{path.suffix or '.stl'}"] = path

            assets = AssetStore(rt.workspace)
            manifest = assets.create(name)
            build_data = result.build.to_dict() if result.build else None
            record = assets.add_revision(
                manifest["asset_id"],
                result.script,
                prompt=prompt.strip(),
                model=settings.model,
                modeler=modeler,
                usage=result.usage,
                checks=checks,
                build=build_data,
                agent={
                    "rounds": result.rounds_used,
                    "finished": result.finished,
                    "stopped_reason": result.stopped_reason,
                    "summary": result.summary,
                    "elapsed_s": round(result.elapsed_s, 2),
                    "trace": result.trace,
                },
                artifacts=artifact_files,
            )
            rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.py")

            built = bool(result.build and result.build.ok)
            hint = (
                "Review model.stl in the revision directory, then slice it."
                if built
                else (
                    "The script was stored but did not build cleanly; inspect "
                    "build.error in params.json, or retry model_generate with more rounds."
                )
            )
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
                "model": settings.model,
                "modeler": modeler,
                "usage": result.usage,
                "checks": checks,
                "build": {
                    "ok": built,
                    "rounds": result.rounds_used,
                    "finished": result.finished,
                    "stopped_reason": result.stopped_reason,
                    "checks": result.build.checks if result.build else {},
                    "error": result.build.error if result.build else None,
                    "timed_out": result.build.timed_out if result.build else False,
                },
                "artifacts": sorted(record["artifacts"]),
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_write",
        description=(
            "Store a build123d script you wrote yourself as a new asset revision, "
            "without calling any LLM. The script must follow XPrint's contract "
            "(from build123d import *, a build_part() returning one solid, PARAMS in "
            "mm, base on z=0) and passes the same static safety checks as generated "
            "scripts; nothing is executed here. Use asset_id to append a revision to "
            "an existing asset, or name to create a new one. WRITES files: confirm "
            "must be exactly 'WRITE <name>' for a new asset or 'WRITE <asset_id>' to "
            "append. Then call model_build."
        ),
    )
    def model_write(
        script: str,
        confirm: str,
        name: str = "",
        asset_id: str = "",
        prompt: str = "",
    ) -> dict[str, Any]:
        try:
            target = asset_id or name
            expected = f"WRITE {target}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not isinstance(script, str) or not script.strip():
                raise InvalidArgument(
                    "script must not be empty",
                    hint="Send the complete Python source of model.py.",
                )

            assets = AssetStore(rt.workspace)
            if asset_id:
                validate_asset_id(asset_id)
                manifest = assets.load(asset_id)
            else:
                name = validate_name(name)
                manifest = None

            checks = summarise(check_script(script))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The script failed XPrint's safety checks; nothing was stored",
                    hint="See details.issues for line numbers, then call model_write again.",
                    details={"issues": checks["errors"]},
                )
            if manifest is None:
                manifest = assets.create(name)

            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt=prompt.strip() or "(written by an MCP client)",
                model="external",
                modeler="mcp-client",
                checks=checks,
            )
            rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.py")
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "name": manifest["name"],
                "revision": record["revision"],
                "script_path": rt.workspace.rel(
                    assets.revision_dir(manifest["asset_id"], record["revision"]) / "model.py"
                ),
                "sha256": record["sha256"],
                "checks": checks,
                "hint": (
                    f"Call model_build with asset_id {manifest['asset_id']!r} and "
                    f"confirm 'BUILD {manifest['asset_id']}'."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_build",
        description=(
            "Execute a stored asset revision in XPrint's sandboxed build123d "
            "interpreter (CPU/time/file limits, no API keys in the environment) and "
            "attach the result to that revision. Returns real geometry checks "
            "(watertight, boundary/non-manifold edges, bbox, volume) and stores "
            "model.stl/model.step. This is the feedback step of the closed loop: read "
            "the checks, fix the script, call model_write again. WRITES files: confirm "
            "must be exactly 'BUILD <asset_id>'."
        ),
    )
    def model_build(asset_id: str, confirm: str, revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"BUILD {asset_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {asset_id} has no revisions",
                    hint="Call model_write first.",
                )
            script_path, script = assets.read_script(asset_id, target)
            checks = summarise(check_script(script))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The stored script failed safety checks; refusing to execute it",
                    details={"issues": checks["errors"]},
                )

            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/builds/{asset_id}_{target}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_script(
                script_path,
                workdir=workdir,
                timeout_s=cad.timeout_s,
                python=cad.python,
            )

            stored: dict[str, Path] = {}
            for value in result.artifacts.values():
                path = Path(value)
                if path.is_file():
                    stored[f"model{path.suffix or '.stl'}"] = path
            record = assets.record_build(
                asset_id,
                target,
                build=result.to_dict(),
                artifacts=stored,
            )
            rt.record_write(f"assets/{asset_id}/{target}/model.stl")

            hint = (
                "Printable so far: review the checks, then slice. If a check failed, "
                "fix the script and call model_write followed by model_build."
            )
            if not result.ok:
                hint = (
                    "Build failed; read error/stderr_tail above, fix model.py and "
                    "call model_write, then model_build again."
                )
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(record["artifacts"]),
                "error": result.error,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_show",
        description=(
            "Return one asset's manifest and the source of a stored revision "
            "(latest by default), so a client model can read, explain or repair it. "
            "Read-only."
        ),
    )
    def model_show(asset_id: str, revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            script = ""
            params: dict[str, Any] = {}
            if target:
                _, script = assets.read_script(asset_id, target)
                params_path = assets.revision_dir(asset_id, target) / "params.json"
                if params_path.is_file():
                    params = json.loads(params_path.read_text(encoding="utf-8"))
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "name": manifest.get("name"),
                "revision": target,
                "revisions": [
                    {
                        "revision": item.get("revision"),
                        "created_at": item.get("created_at"),
                        "checks": item.get("checks", {}),
                        "build_ok": (item.get("build") or {}).get("ok"),
                        "artifacts": sorted(item.get("artifacts", {})),
                    }
                    for item in manifest.get("revisions", [])
                ],
                "script": script,
                "params": params,
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
