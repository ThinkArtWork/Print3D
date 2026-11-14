"""MCP server wiring: transports, auth, and the M0 tool surface.

The server is intentionally thin.  Tool implementations live in
:mod:`xprint.tools` and receive a :class:`Runtime`, so they can be unit tested
without speaking MCP.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from . import __version__
from .assets import AssetStore, validate_asset_id, validate_name
from .blender import run_blender
from .blender_policy import check_blender_script
from .cad import run_script
from .capabilities import build_capabilities, build_system_status
from .cloud import build_provider, download_model, to_stl
from .config import (
    ImageProviderSettings,
    LoadedConfig,
    PrinterSettings,
    Secrets,
    load_all,
    remove_printer,
    save_printer,
)
from .errors import (
    AdapterUnavailable,
    ConfigError,
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    XPrintError,
    error_payload,
)
from .images import MAX_IMAGE_BYTES, GeneratedImage, ReferenceStore, sniff_image_mime
from .jobs import Job, JobStore, sha256_file
from .local_mesh import DEFAULT_TIMEOUT_S as LOCAL_GENERATION_TIMEOUT
from .local_mesh import run_local
from .mesh import stats_for_stl
from .preview import DEFAULT_VIEWS, render_views
from .printers import PRINTER_ADAPTERS, build_printer, validate_printer_id
from .registry import Registry, builtin_registry
from .runner import JobRunner
from .script_policy import check_script, summarise
from .slicers import (
    PreparedProfiles,
    SliceStore,
    build_command,
    find_slicer,
    new_slice_id,
    parse_machine_bed,
    probe_slicer,
    resolve_profiles,
    slice_stl,
    validate_slice_id,
)
from .workspace import Workspace, atomic_write_bytes

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

INSTRUCTIONS = """\
XPrint is a self-hosted bridge between MCP agents and the 3D-printing tools on
this machine.

Safety contract:
- Read-only tools (capabilities, system_status, job_status, model_show,
  asset_list) run without confirmation.
- Every tool that writes files or talks to a paid service requires the exact
  `confirm` string documented in its schema.  A wrong string is rejected with
  code `confirmation_mismatch`, nothing is written.
- XPrint never sends arbitrary G-code to a printer; slicing output is inspected
  and confirmed first.

Start with `capabilities` to learn which providers, CAD backends, image
providers, slicers and printers are available on this host.

Two modelling paths, both stored as versioned assets:

1. Text -> concept image -> model:
   `image_generate` turns the idea into a concept image and stores it as a
   reference.  Write a build123d script informed by that image, call
   `model_write` with `reference=<image_path>`, then `model_build` to execute it
   in a sandbox and read real geometry checks.  `model_preview` renders the
   built STL back to you as an image (next to the concept image), so you can
   compare intent with the result and revise.

2. Text -> model (no image step):
   write the script directly, call `model_write`, `model_build`, then
   `model_preview` to look at the result.  Repeat until the part is watertight
   and matches the request.

`model_show` returns a stored revision's script and provenance.  Built artifacts
(model.stl, model.step) live in the workspace; previews are returned inline as
PNG content.

Two extra engines reuse the same asset + preview loop:

- `blender_run` executes a bpy scene script with Blender in background mode,
  exports model.stl/model.glb and returns the same geometry checks.  Use it for
  text-to-3D when Blender's modelling tools fit better than build123d; for
  image-to-3D, look at the attached image yourself and write the scene.
- `generate_local` runs a user-installed local generator (configured under
  providers.local.<id>) as a background job; poll `job_status`, then preview the
  produced asset.  XPrint downloads no weights and calls no cloud API.

Both accept `image=` for image-driven work; assets remember the reference.

Once the model is accepted, slice it for a configured target: `slice_plan` is a
read-only dry run that prints the exact confirm string; `slice_run` produces
G-code asynchronously (poll `job_status`, then `slice_inspect`).  To print,
`send` uploads the G-code to a configured printer and starts it by default;
`printer_status` and `printer_cancel` cover the live state, while
`printer_list`/`printer_save`/`printer_remove` manage the printer list.
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
    runner: JobRunner | None = None

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
        store = JobStore(ws)
        return cls(
            workspace=ws,
            loaded=loaded,
            secrets=secrets,
            store=store,
            registry=registry or builtin_registry(),
            runner=JobRunner(store),
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


def _image_result(
    payload: dict[str, Any], images: list[tuple[bytes, str, str | None]]
) -> CallToolResult:
    """Return structured JSON and image blocks in a single tool result."""

    content: list[Any] = [
        TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))
    ]
    for data, mime, label in images:
        if label:
            content.append(TextContent(type="text", text=label))
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType=mime,
            )
        )
    return CallToolResult(content=content, structuredContent=payload)


def _image_settings(rt: Runtime, provider: str) -> tuple[str, ImageProviderSettings]:
    configured = rt.loaded.config.images
    if provider:
        settings = configured.get(provider)
        if settings is None:
            known = ", ".join(sorted(configured)) or "(none)"
            raise InvalidArgument(
                f"Unknown image provider: {provider}",
                hint=f"Configured image providers: {known}",
            )
        provider_id = provider
    else:
        enabled = [(name, item) for name, item in sorted(configured.items()) if item.enabled]
        if not enabled:
            raise AdapterUnavailable(
                "No image provider is enabled",
                hint="Enable one under images.<id> in config.json, or add a key.",
            )
        provider_id, settings = enabled[0]
    if not settings.enabled:
        raise AdapterUnavailable(
            f"Image provider {provider_id!r} is disabled in config",
            hint=f"Set config.images.{provider_id}.enabled = true.",
        )
    return provider_id, settings


def _reference_meta(rt: Runtime, path: Path) -> dict[str, Any]:
    """Read the meta.json of a stored reference, when the path is one."""
    meta_path = path.parent / "meta.json"
    try:
        path.relative_to(rt.workspace.references_dir)
    except ValueError:
        return {"source_path": rt.workspace.rel(path)}
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            return data
    return {"source_path": rt.workspace.rel(path)}


def _printer_entry(rt: Runtime, printer_id: str):
    for entry in rt.loaded.config.printers:
        if entry.id == printer_id:
            return entry
    known = ", ".join(entry.id for entry in rt.loaded.config.printers) or "(none)"
    raise NotFound(
        f"Unknown printer: {printer_id}",
        hint=f"Configured printers: {known}. Add one with printer_save.",
    )


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
        name="reference_import",
        description=(
            "Import an image the user already has (a sketch, a photo, a screenshot) "
            "into the workspace as a concept reference, so it can be attached to a "
            "model revision with model_write(reference=...). This is the image-input "
            "half of the text/image -> model path: the client model looks at the "
            "image and writes the script; XPrint keeps the image as provenance. "
            "WRITES files: confirm must be exactly 'IMPORT <name>'."
        ),
    )
    def reference_import(path: str, name: str, confirm: str) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"IMPORT {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            source = Path(path).expanduser()
            if not source.is_absolute():
                source = Path.cwd() / source
            if not source.is_file():
                raise NotFound(
                    f"Image not found: {source}",
                    hint="Pass an absolute path, or a path relative to the server's cwd.",
                )
            data = source.read_bytes()
            if not data:
                raise InvalidArgument("The image file is empty")
            if len(data) > MAX_IMAGE_BYTES:
                raise InvalidArgument(
                    f"The image is too large ({len(data)} bytes, limit {MAX_IMAGE_BYTES})",
                    hint="Resize it first.",
                )
            mime = sniff_image_mime(data[:32])
            if mime is None:
                raise InvalidArgument(
                    "The file is not a PNG, JPEG or WebP image",
                    details={"path": str(source)},
                )

            image = GeneratedImage(
                data=data,
                provider="local",
                model="upload",
                size="",
                prompt=f"imported from {source.name}",
            )
            store = ReferenceStore(rt.workspace)
            meta = store.create(
                image,
                prompt=f"imported from {source.name}",
                provider="local",
                model="upload",
            )
            relative = rt.workspace.rel(store.directory(meta["reference_id"]) / meta["file"])
            rt.record_write(relative)
            return {
                "ok": True,
                "writes_performed": True,
                "reference_id": meta["reference_id"],
                "image_path": relative,
                "name": name,
                "source_path": str(source),
                "mime": mime,
                "bytes": meta["bytes"],
                "sha256": meta["sha256"],
                "hint": (
                    f"Now call model_write with reference={relative!r} and confirm='WRITE {name}'."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="image_generate",
        description=(
            "Generate a concept image from a text prompt and store it as a reference "
            "in the workspace (references/<id>/). This is step one of the "
            "text -> image -> model path: pass the returned image_path to model_write "
            "as `reference`, then build and preview the model. Any OpenAI-compatible "
            "images endpoint works (config.images.<id>). WRITES files and calls a paid "
            "service: confirm must be exactly 'IMAGE <name>'. The image is also "
            "returned inline so you can see it."
        ),
    )
    def image_generate(
        prompt: str,
        name: str,
        confirm: str,
        provider: str = "",
        size: str = "",
    ) -> Any:
        try:
            name = validate_name(name)
            expected = f"IMAGE {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not isinstance(prompt, str) or not prompt.strip():
                raise InvalidArgument(
                    "prompt must not be empty",
                    hint="Describe the product, style, material and the key features.",
                )

            provider_id, settings = _image_settings(rt, provider)
            key = rt.secrets.provider_key(provider_id, api_key_env=settings.api_key_env)
            if not key:
                raise AdapterUnavailable(
                    f"No API key for image provider {provider_id!r}",
                    hint=(
                        f"Set {settings.api_key_env} (or images.{provider_id}.api_key_env), "
                        "or put the key in secrets.json under providers."
                    ),
                    details={"provider": provider_id, "env": settings.api_key_env},
                )

            adapter = settings.adapter or provider_id
            generator = rt.registry.create(
                "image",
                adapter,
                settings=settings.model_dump(),
                api_key=key,
                provider_id=provider_id,
            )
            image = generator.generate(prompt, size=size or settings.size)

            store = ReferenceStore(rt.workspace)
            meta = store.create(
                image,
                prompt=prompt.strip(),
                provider=provider_id,
                model=settings.model,
            )
            image_path = store.directory(meta["reference_id"]) / meta["file"]
            relative = rt.workspace.rel(image_path)
            rt.record_write(relative)

            payload = {
                "ok": True,
                "writes_performed": True,
                "reference_id": meta["reference_id"],
                "image_path": relative,
                "name": name,
                "provider": provider_id,
                "model": meta["model"],
                "size": meta["size"],
                "bytes": meta["bytes"],
                "sha256": meta["sha256"],
                "elapsed_s": meta["elapsed_s"],
                "prompt": meta["prompt"],
                "hint": (
                    f"Now write the build123d script and call model_write with "
                    f"reference={relative!r} and confirm='WRITE {name}'."
                ),
            }
            return _image_result(payload, [(image.data, image.mime, None)])
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_write",
        description=(
            "Store a build123d script as a new asset revision, without calling any "
            "LLM. The script must follow XPrint's contract (from build123d import *, "
            "a build_part() returning one solid, PARAMS in mm, base on z=0) and passes "
            "the static safety checks; nothing is executed here. Use asset_id to "
            "append to an existing asset, or name to create one. Optional `reference` "
            "attaches a concept image (the image_path returned by image_generate) to "
            "this revision so the model has a visual record of the request. WRITES "
            "files: confirm must be exactly 'WRITE <name>' for a new asset or "
            "'WRITE <asset_id>' to append. Then call model_build."
        ),
    )
    def model_write(
        script: str,
        confirm: str,
        name: str = "",
        asset_id: str = "",
        prompt: str = "",
        reference: str = "",
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

            reference_path: Path | None = None
            reference_meta: dict[str, Any] = {}
            if reference:
                reference_path = rt.workspace.safe_path(reference, must_exist=True)
                if reference_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise InvalidArgument(
                        "reference must be a PNG, JPEG or WebP image",
                        hint="Use the image_path returned by image_generate.",
                        details={"path": reference},
                    )
                head = reference_path.read_bytes()[:32]
                if sniff_image_mime(head) is None:
                    raise InvalidArgument(
                        "reference is not a valid image file",
                        hint="Use the image_path returned by image_generate.",
                        details={"path": reference},
                    )
                reference_meta = _reference_meta(rt, reference_path)
                if not prompt.strip():
                    prompt = str(reference_meta.get("prompt") or "")

            if manifest is None:
                manifest = assets.create(name)

            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt=prompt.strip() or "(written by an MCP client)",
                model="external",
                modeler="mcp-client",
                checks=checks,
                reference=reference_path,
                reference_meta=reference_meta,
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
                "reference": record["reference"],
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
            if manifest.get("kind", "build123d") != "build123d":
                raise InvalidArgument(
                    f"Asset {asset_id} was produced by {manifest.get('kind')} and has no build123d script",
                    hint="Use model_preview or asset_export for this asset, then blender_run/generate_local to add a revision.",
                )
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
        name="model_preview",
        description=(
            "Render a built revision's model.stl to a PNG with labelled orthographic "
            "views (iso/front/top/right by default) and return it inline, together "
            "with the concept reference when the revision has one. Use this after "
            "model_build to see the real geometry and compare it with the request; "
            "then fix the script and call model_write/model_build again. Read-only "
            "for assets: the PNG is cached under cache/previews/ and nothing in the "
            "revision changes."
        ),
    )
    def model_preview(
        asset_id: str,
        revision: str = "",
        views: str = "",
        width: int = 900,
        height: int = 700,
    ) -> Any:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {asset_id} has no revisions",
                    hint="Call model_write first.",
                )
            directory = assets.revision_dir(asset_id, target)
            stl = directory / "model.stl"
            if not stl.is_file():
                raise InvalidArgument(
                    f"Revision {target} has no built model yet",
                    hint=(
                        f"Call model_build with asset_id {asset_id!r} and "
                        f"confirm 'BUILD {asset_id}' first."
                    ),
                )
            if not 320 <= int(width) <= 1600:
                raise InvalidArgument("width must be between 320 and 1600 pixels")
            if not 240 <= int(height) <= 1200:
                raise InvalidArgument("height must be between 240 and 1200 pixels")

            wanted = (
                tuple(item.strip().lower() for item in (views or "").split(",") if item.strip())
                or DEFAULT_VIEWS
            )
            digest = sha256_file(stl)[:8]
            cache = rt.workspace.safe_path(
                f"cache/previews/{asset_id}_{target}_{digest}_{int(width)}x{int(height)}.png",
                create_parents=True,
            )
            cached = cache.is_file()
            if cached:
                png = cache.read_bytes()
            else:
                caption = f"{manifest.get('name') or asset_id} {target}"
                try:
                    png = render_views(
                        stl,
                        views=wanted,
                        width=int(width),
                        height=int(height),
                        caption=caption,
                    )
                except ValueError as exc:
                    raise InvalidArgument(
                        f"Cannot render the model: {exc}",
                        hint="Check the views names or rebuild the model.",
                        details={"views": list(wanted)},
                    ) from None
                atomic_write_bytes(cache, png, mode=0o644)

            images: list[tuple[bytes, str, str | None]] = []
            reference_file = ""
            params_path = directory / "params.json"
            if params_path.is_file():
                try:
                    params = json.loads(params_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    params = {}
                reference_file = str((params.get("reference") or {}).get("file") or "")
            if not reference_file:
                candidates = sorted(directory.glob("reference.*"))
                reference_file = candidates[0].name if candidates else ""
            if reference_file and (directory / reference_file).is_file():
                ref_path = directory / reference_file
                ref_mime = sniff_image_mime(ref_path.read_bytes()[:32]) or "image/png"
                images.append(
                    (ref_path.read_bytes(), ref_mime, "Concept reference (what was asked for):")
                )

            images.append(
                (
                    png,
                    "image/png",
                    f"Rendered model, revision {target} (views: {', '.join(wanted)}):",
                )
            )
            payload = {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": target,
                "image_path": rt.workspace.rel(cache),
                "cached": cached,
                "views": list(wanted),
                "width": int(width),
                "height": int(height),
                "bytes": len(png),
                "reference": rt.workspace.rel(directory / reference_file)
                if reference_file
                else None,
                "hint": (
                    "Compare the render with the request and the concept reference. "
                    "If proportions or features are wrong, edit model.py and call "
                    "model_write (new revision) then model_build again."
                ),
            }
            return _image_result(payload, images)
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="blender_run",
        description=(
            "Turn a bpy Python script into a printable mesh by running Blender in "
            "background mode (blender -b --factory-startup --python scene.py), then "
            "exporting every mesh object to model.stl + model.glb and inspecting it. "
            "This is the Blender path for text-to-3D: the client model writes the "
            "scene (primitive_*, bmesh, solidify, remesh, ...); for image-to-3D it "
            "looks at the attached image itself and the script can read the same "
            "file from sys.argv after '--'. Pass asset_id to append a new revision "
            "to an existing Blender asset. WRITES files and executes code in "
            "Blender: confirm must be exactly 'BLENDER <name>' or 'BLENDER <asset_id>'."
        ),
    )
    def blender_run(
        script: str,
        confirm: str,
        name: str = "",
        asset_id: str = "",
        image: str = "",
        blender_path: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            token = validate_asset_id(asset_id) if asset_id else validate_name(name)
            expected = f"BLENDER {token}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )

            issues = check_blender_script(script)
            errors = [issue.as_dict() for issue in issues if issue.severity == "error"]
            if errors:
                raise InvalidArgument(
                    "The bpy script failed safety checks; nothing was executed",
                    details={"issues": errors},
                )

            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id) if asset_id else assets.create(name, kind="blender")
            if manifest.get("kind") != "blender":
                raise InvalidArgument(
                    f"Asset {manifest['asset_id']} was built with {manifest.get('kind') or 'build123d'}",
                    hint="Use model_write/model_build for build123d assets, or pass name= to create a new Blender asset.",
                )

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Pass a PNG/JPEG/WebP path or omit image=.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )

            checks = summarise(issues)
            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt="",
                model="blender",
                modeler="blender",
                checks={"policy": checks},
                reference=image_path,
                reference_meta={"source_path": str(image_path)} if image_path else None,
                kind="blender",
            )
            revision = record["revision"]
            render = rt.loaded.config.render
            workdir = rt.workspace.safe_path(
                f"cache/blender/{manifest['asset_id']}_{revision}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_blender(
                script,
                workdir=workdir,
                image=image_path,
                blender_path=blender_path or render.blender_path,
                timeout_s=float(timeout_s or render.timeout_s),
            )

            stored: dict[str, Path] = {}
            for stored_name, value in result.artifacts.items():
                path = Path(value)
                if path.is_file():
                    stored[Path(stored_name).name] = path
            if stored:
                assets.record_build(
                    manifest["asset_id"],
                    revision,
                    build=result.to_dict(),
                    artifacts=stored,
                )
                rt.record_write(f"assets/{manifest['asset_id']}/{revision}/model.stl")

            hint = (
                "Review checks and call model_preview; fix scene.py and call "
                "blender_run with asset_id to add the next revision."
                if result.ok
                else "Read stderr_tail, fix scene.py, then call blender_run again with asset_id."
            )
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "revision": revision,
                "engine": "blender",
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(stored),
                "error": result.error,
                "stdout_tail": result.stdout[-1500:],
                "stderr_tail": result.stderr[-1500:],
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="generate_cloud",
        description=(
            "Generate a 3D model with a cloud provider (tripo or hunyuan) in the "
            "background. Configure it in providers.cloud.<id> (adapter, key env, "
            "scale, timeout). XPrint submits the task, polls it, downloads the "
            "result, converts it to STL, records real geometry checks and stores "
            "a versioned asset. Returns a job_id: poll job_status until succeeded, "
            "then call model_preview. WRITES files and spends provider credits: "
            "confirm must be exactly 'CLOUD <name>'."
        ),
    )
    def generate_cloud(
        provider: str,
        name: str,
        confirm: str,
        prompt: str = "",
        image: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"CLOUD {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not prompt.strip() and not image:
                raise InvalidArgument(
                    "Provide prompt, image, or both",
                    hint="Cloud providers accept a text prompt, a reference image, or both.",
                )

            entry = rt.loaded.config.providers.cloud.get(provider)
            if entry is None:
                raise InvalidArgument(
                    f"Unknown cloud provider: {provider}",
                    hint="Add providers.cloud.<id> to config.json; `xprint config` shows the file.",
                    details={"configured": sorted(rt.loaded.config.providers.cloud)},
                )
            if not entry.get("enabled", True):
                raise AdapterUnavailable(
                    f"Cloud provider {provider!r} is disabled",
                    hint="Set providers.cloud.<id>.enabled to true.",
                )
            adapter = str(entry.get("adapter") or provider).lower()

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Call reference_import first or pass a valid path.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )
                if image_path.stat().st_size > MAX_IMAGE_BYTES:
                    raise InvalidArgument("The reference image is larger than 12 MB")

            api_key = rt.secrets.provider_key(provider, api_key_env=entry.get("api_key_env"))
            secret_id = os.environ.get(str(entry.get("secret_id_env") or "")) or (
                rt.secrets.provider_field(provider, "secret_id")
            )
            secret_key = os.environ.get(str(entry.get("secret_key_env") or "")) or (
                rt.secrets.provider_field(provider, "secret_key")
            )
            if adapter == "tripo" and not api_key:
                raise AdapterUnavailable(
                    "No Tripo API key",
                    hint=f"Set {entry.get('api_key_env') or 'TRIPO_API_KEY'}.",
                )
            if adapter == "hunyuan" and not (secret_id and secret_key):
                raise AdapterUnavailable(
                    "No Tencent Cloud credentials for Hunyuan 3D",
                    hint="Set TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY.",
                )

            scale = float(entry.get("scale") or 1.0)
            if scale <= 0:
                raise ConfigError(
                    f"providers.cloud.{provider}.scale must be positive",
                    hint="Tripo GLB output is in meters: use 1000 for millimeters.",
                )
            budget = float(timeout_s or entry.get("timeout_s") or 600)
            poll_interval = float(entry.get("poll_interval_s") or 3.0)
            mode = "both" if prompt.strip() and image else ("image" if image else "text")

            def work(job_id: str) -> dict[str, Any]:
                client = build_provider(
                    provider,
                    entry,
                    api_key=api_key,
                    secret_id=secret_id,
                    secret_key=secret_key,
                )
                try:
                    task_id = client.submit(
                        prompt=(prompt.strip() or None),
                        image=(image_path.read_bytes() if image_path else None),
                    )
                    rt.store.set_progress(job_id, 0.05, message=f"{provider} task {task_id}")
                    deadline = time.monotonic() + budget
                    task = None
                    while True:
                        task = client.poll(task_id)
                        progress = max(5.0, min(float(task.progress or 5), 95.0)) / 100.0
                        rt.store.set_progress(
                            job_id, progress, message=f"{provider}: {task.status}"
                        )
                        if task.status == "success":
                            break
                        if task.status in {"failed", "cancelled"}:
                            raise AdapterUnavailable(
                                f"{provider} task {task.status}: {task.error or 'no detail'}",
                                hint="Check the provider dashboard and your credits.",
                                details={"task_id": task_id},
                            )
                        if time.monotonic() >= deadline:
                            raise AdapterUnavailable(
                                f"{provider} task did not finish within {int(budget)} s",
                                hint="Raise providers.cloud.<id>.timeout_s and retry.",
                                details={"task_id": task_id},
                            )
                        time.sleep(max(0.2, poll_interval))
                    if not task.model_url:
                        raise AdapterUnavailable(
                            f"{provider} finished without a model URL",
                            details={"task_id": task_id},
                        )
                    raw, suffix = download_model(task.model_url)
                    stl = to_stl(raw, suffix, scale=scale)
                    staging = rt.workspace.safe_path(f"cache/cloud/{job_id}", create_parents=True)
                    stl_path = staging / "model.stl"
                    atomic_write_bytes(stl_path, stl, mode=0o644)
                    artifacts = {"model.stl": stl_path}
                    if suffix != ".stl":
                        original = staging / f"model{suffix}"
                        atomic_write_bytes(original, raw, mode=0o644)
                        artifacts[f"model{suffix}"] = original

                    checks = stats_for_stl(stl_path)
                    checks["engine"] = f"cloud:{provider}"
                    checks["provider_task"] = task_id
                    assets = AssetStore(rt.workspace)
                    manifest = assets.create(name, kind="cloud")
                    record = assets.add_revision(
                        manifest["asset_id"],
                        "# cloud generator: no script is produced\n",
                        prompt=prompt,
                        model=f"cloud:{provider}",
                        modeler="cloud",
                        usage={
                            "provider": provider,
                            "task_id": task_id,
                            "credits": task.credits,
                        },
                        checks=checks,
                        reference=image_path,
                        reference_meta=(
                            {"source_path": str(image_path), "mode": mode} if image_path else None
                        ),
                        artifacts=artifacts,
                        kind="cloud",
                    )
                    rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.stl")
                    return {
                        "asset_id": manifest["asset_id"],
                        "name": name,
                        "revision": record["revision"],
                        "provider": provider,
                        "mode": mode,
                        "task_id": task_id,
                        "checks": checks,
                        "artifacts": sorted(record.get("artifacts", {})),
                    }
                finally:
                    client.close()

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "cloud.generate",
                work,
                params={"provider": provider, "name": name, "mode": mode},
                provider=provider,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "provider": provider,
                "adapter": adapter,
                "mode": mode,
                "timeout_s": budget,
                "hint": "Poll job_status(job_id); on success call model_preview(asset_id).",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="generate_local",
        description=(
            "Run a user-installed local 3D generator in the background. The provider "
            "is configured in providers.local.<id> as a command + args template with "
            "{prompt}, {image}, {outdir}, {name} placeholders; it must write "
            '<outdir>/mesh.stl (or print {"mesh": path}). XPrint creates the asset '
            "from the produced STL, records real geometry checks and returns a "
            "job_id: poll job_status until succeeded, then call model_preview. "
            "No weights are downloaded and no cloud API is called. WRITES files and "
            "runs the provider command: confirm must be exactly 'LOCAL <name>'."
        ),
    )
    def generate_local(
        provider: str,
        name: str,
        confirm: str,
        prompt: str = "",
        image: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"LOCAL {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not prompt.strip() and not image:
                raise InvalidArgument(
                    "Provide prompt, image, or both",
                    hint="Local providers are text-to-3D, image-to-3D, or both.",
                )

            entry = rt.loaded.config.providers.local.get(provider)
            if entry is None:
                raise InvalidArgument(
                    f"Unknown local provider: {provider}",
                    hint=(
                        "Add providers.local.<id> to config.json with command/args; "
                        "run `xprint config` to see the current file."
                    ),
                    details={"configured": sorted(rt.loaded.config.providers.local)},
                )
            if not entry.get("enabled", True):
                raise AdapterUnavailable(
                    f"Local provider {provider!r} is disabled",
                    hint="Set providers.local.<id>.enabled to true.",
                )

            mode = "both" if prompt.strip() and image else ("image" if image else "text")
            declared = str(entry.get("mode") or "both")
            if declared not in {"text", "image", "both"}:
                raise ConfigError(
                    f"providers.local.{provider}.mode must be text, image or both",
                    hint="Fix config.json and retry.",
                )
            if declared != "both" and declared != mode:
                raise InvalidArgument(
                    f"Provider {provider!r} only supports {declared}-to-3D",
                    hint=f"Pass {'prompt' if declared == 'text' else 'image'} instead.",
                )

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Import the picture first (reference_import) or pass a valid path.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )

            budget = float(timeout_s or entry.get("timeout_s") or LOCAL_GENERATION_TIMEOUT)
            if budget <= 0:
                raise InvalidArgument("timeout must be positive")

            def work(job_id: str) -> dict[str, Any]:
                outdir = rt.workspace.safe_path(f"cache/local/{job_id}", create_parents=True)
                result = run_local(
                    provider,
                    entry,
                    prompt=prompt,
                    image=image_path,
                    outdir=outdir,
                    name=name,
                    mode=mode,
                    timeout_s=budget,
                )
                if not result.ok or not result.mesh:
                    raise AdapterUnavailable(
                        result.error or f"Local provider {provider!r} failed",
                        hint=result.hint,
                        details={
                            "provider": provider,
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                            "stdout_tail": result.stdout[-1500:],
                            "stderr_tail": result.stderr[-1500:],
                        },
                    )
                checks = stats_for_stl(result.mesh)
                checks["engine"] = f"local:{provider}"
                assets = AssetStore(rt.workspace)
                manifest = assets.create(name, kind="local")
                record = assets.add_revision(
                    manifest["asset_id"],
                    "# local generator: no script is produced\n",
                    prompt=prompt,
                    model=f"local:{provider}",
                    modeler="local",
                    usage={"command": result.command, "duration_s": round(result.duration_s, 2)},
                    checks=checks,
                    reference=image_path,
                    reference_meta=(
                        {"source_path": str(image_path), "mode": mode} if image_path else None
                    ),
                    artifacts={"model.stl": Path(result.mesh)},
                    kind="local",
                )
                rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.stl")
                return {
                    "asset_id": manifest["asset_id"],
                    "name": name,
                    "revision": record["revision"],
                    "provider": provider,
                    "mode": mode,
                    "duration_s": round(result.duration_s, 2),
                    "checks": checks,
                    "artifacts": sorted(record.get("artifacts", {})),
                }

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "local.generate",
                work,
                params={"provider": provider, "name": name, "mode": mode},
                provider=provider,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "provider": provider,
                "mode": mode,
                "timeout_s": budget,
                "hint": "Poll job_status(job_id); on success call model_preview(asset_id).",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="slice_capabilities",
        description=(
            "Report the configured print targets, whether an OrcaSlicer-compatible "
            "binary is installed, and whether each target's machine/process/filament "
            "presets resolve. Read-only."
        ),
    )
    def slice_capabilities() -> dict[str, Any]:
        try:
            targets: list[dict[str, Any]] = []
            default_binary = find_slicer()
            default_info = probe_slicer(default_binary)
            for name, configured in rt.loaded.config.targets.items():
                problems: list[str] = []
                try:
                    binary = find_slicer(configured.slicer_path)
                except XPrintError as exc:
                    binary = None
                    problems.append(exc.message)
                info = probe_slicer(binary) if binary is not None else None
                if info is None:
                    problems.append("no slicer binary found")
                else:
                    problems.extend(info.problems)
                resolved: dict[str, Any] = {}
                bed = None
                try:
                    machine, process, filaments = resolve_profiles(configured)
                    resolved = {
                        "machine": str(machine),
                        "process": str(process),
                        "filament": [str(item) for item in filaments],
                    }
                    bed = parse_machine_bed(machine)
                except XPrintError as exc:
                    problems.append(exc.message)
                targets.append(
                    {
                        "name": name,
                        "printer_model": configured.printer_model,
                        "slicer": configured.slicer,
                        "binary": str(binary) if binary is not None else None,
                        "version": info.version if info is not None else None,
                        "profiles": resolved,
                        "bed_mm": list(bed) if bed else None,
                        "overrides": dict(configured.overrides or {}),
                        "timeout_s": configured.timeout_s,
                        "ready": not problems,
                        "problems": problems,
                    }
                )
            return {
                "ok": True,
                "writes_performed": False,
                "slicer": default_info.to_dict(),
                "targets": targets,
                "configured": len(targets),
                "ready": sum(1 for item in targets if item["ready"]),
                "hint": (
                    "Add targets.<name> in config.json with machine/process/filament "
                    "preset paths; see README."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="slice_plan",
        description=(
            "Dry-run a slice: resolve the target presets, report bed size and model "
            "checks, and print the exact command slice_run would execute. Read-only."
        ),
    )
    def slice_plan(asset_id: str, target: str, revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            configured = rt.loaded.config.target(target)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            matches = [
                item for item in manifest.get("revisions", []) if item.get("revision") == rev
            ]
            if not matches:
                raise NotFound(
                    f"Revision {rev} not found on {asset_id}",
                    hint="Call model_show to list revisions.",
                )
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then slice_plan.",
                )
            problems: list[str] = []
            binary = None
            try:
                binary = find_slicer(configured.slicer_path)
            except XPrintError as exc:
                problems.append(exc.message)
            info = probe_slicer(binary) if binary is not None else None
            if binary is None:
                problems.append("no slicer binary found")
            elif info is not None:
                problems.extend(info.problems)
            machine, process, filaments = resolve_profiles(configured)
            bed = parse_machine_bed(machine)
            checks = stats_for_stl(stl)
            warnings: list[str] = []
            if configured.overrides:
                warnings.append(
                    "overrides are applied to a private copy of the vendor presets at slice time"
                )
            if bed is None:
                warnings.append(
                    "bed size is not in the machine preset (it may be inherited); "
                    "slice_run reads it from the produced G-code instead"
                )
            if bed and checks.get("bbox_mm"):
                width, depth = bed
                bbox = checks["bbox_mm"]
                if bbox[0] > width or bbox[1] > depth:
                    warnings.append(
                        f"model footprint {bbox[0]:.1f}x{bbox[1]:.1f} mm exceeds the "
                        f"{width:.0f}x{depth:.0f} mm bed"
                    )
            plan_workdir = rt.workspace.slices_dir / "plan"
            prepared = PreparedProfiles(
                machine=machine,
                process=process,
                filament=filaments,
                datadir=Path(configured.datadir).expanduser() if configured.datadir else None,
            )
            command = (
                build_command(
                    binary,
                    prepared,
                    stl=stl,
                    outdir=plan_workdir / "output",
                    supports_statistics=bool(info and info.supports_statistics),
                )
                if binary is not None
                else []
            )
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target,
                "printer_model": configured.printer_model,
                "slicer": info.to_dict() if info is not None else None,
                "profiles": prepared.to_dict(),
                "bed_mm": list(bed) if bed else None,
                "checks": checks,
                "command": command,
                "ready": not problems,
                "problems": problems,
                "warnings": warnings,
                "confirm": f"SLICE {asset_id} {target}",
                "hint": "Run slice_run with the confirm string above; the job is async.",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="slice_run",
        description=(
            "Slice a built revision for a configured target with the external "
            "OrcaSlicer CLI. Runs as an async job (poll job_status). confirm must be "
            "exactly 'SLICE <asset_id> <target>'."
        ),
    )
    def slice_run(
        asset_id: str,
        target: str,
        confirm: str,
        revision: str = "",
        timeout_s: float = 0,
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"SLICE {asset_id} {target}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            configured = rt.loaded.config.target(target)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then slice_run.",
                )
            binary = find_slicer(configured.slicer_path)
            if binary is None:
                raise AdapterUnavailable(
                    "No slicer found",
                    hint=(
                        "Install OrcaSlicer into /Applications, or set targets.<name>.slicer_path."
                    ),
                )
            info = probe_slicer(binary)
            if not info.found:
                raise AdapterUnavailable(
                    f"Slicer did not start: {binary}",
                    hint="; ".join(info.problems) or "Run it once from the GUI to finish setup.",
                )
            budget = float(timeout_s or configured.timeout_s)
            slice_id = new_slice_id()
            slice_dir = rt.workspace.slices_dir / slice_id

            def work(job_id: str) -> dict[str, Any]:
                rt.store.set_progress(job_id, 0.1)
                result = slice_stl(
                    stl,
                    configured,
                    slice_dir,
                    timeout_s=budget,
                    slicer_path=configured.slicer_path,
                )
                record = SliceStore(rt.workspace).save_result(
                    slice_id=slice_id,
                    asset_id=asset_id,
                    revision=rev,
                    target_name=target,
                    target=configured,
                    result=result,
                    slicer=info,
                )
                rt.record_write(f"slices/{slice_id}/slice.json")
                if not result.ok:
                    raise AdapterUnavailable(
                        result.error or "Slicing failed",
                        hint=result.hint,
                        details={
                            "slice_id": slice_id,
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                        },
                    )
                return {
                    "slice_id": slice_id,
                    "statistics": result.statistics,
                    "metadata": result.metadata,
                    "checks": result.checks,
                    "gcode": record.get("gcode"),
                    "duration_s": round(result.duration_s, 2),
                    "warnings": result.warnings,
                }

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "slice.run",
                work,
                params={"asset_id": asset_id, "revision": rev, "target": target},
                provider=target,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "slice_id": slice_id,
                "target": target,
                "printer_model": configured.printer_model,
                "timeout_s": budget,
                "hint": (
                    f"Poll job_status({job.id}); on success call "
                    f"slice_inspect({slice_id}) for the G-code metadata."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="slice_inspect",
        description="Read a stored slice record, its statistics and G-code path. Read-only.",
    )
    def slice_inspect(slice_id: str) -> dict[str, Any]:
        try:
            validate_slice_id(slice_id)
            store = SliceStore(rt.workspace)
            record = store.load(slice_id)
            directory = store.directory(slice_id)
            gcode_name = (record.get("gcode") or {}).get("file")
            gcode = directory / gcode_name if gcode_name else None
            gcode_payload = dict(record.get("gcode") or {})
            if gcode is not None:
                gcode_payload["path"] = str(gcode)
                gcode_payload["present"] = gcode.is_file()
            return {
                "ok": True,
                "writes_performed": False,
                "slice": record,
                "directory": str(directory),
                "gcode": gcode_payload or None,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_list",
        description=(
            "List configured printers with adapter, model, address, toolheads and "
            "whether an API key is available. Read-only (no network probe)."
        ),
    )
    def printer_list() -> dict[str, Any]:
        try:
            entries = []
            for entry in rt.loaded.config.printers:
                key_env = entry.access_code_env or entry.api_key_env
                key = rt.secrets.provider_key(entry.id, api_key_env=key_env)
                entries.append(
                    {
                        "id": entry.id,
                        "adapter": entry.adapter,
                        "adapter_known": entry.adapter.lower() in PRINTER_ADAPTERS,
                        "model": entry.model,
                        "base_url": entry.base_url,
                        "serial": entry.serial,
                        "toolheads": entry.toolheads,
                        "enabled": entry.enabled,
                        "has_key": bool(key),
                        "key_source": rt.secrets.key_source(entry.id, api_key_env=key_env),
                    }
                )
            return {
                "ok": True,
                "writes_performed": False,
                "printers": entries,
                "count": len(entries),
                "config_path": str(rt.loaded.path or rt.workspace.config_path),
                "hint": (
                    "Call printer_status(id) to test a connection, or printer_save to "
                    "add or update one."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_status",
        description=(
            "Query one printer's live state (state, filename, progress, temperatures) "
            "through its adapter. Read-only."
        ),
    )
    def printer_status(printer_id: str) -> dict[str, Any]:
        try:
            validate_printer_id(printer_id)
            entry = _printer_entry(rt, printer_id)
            adapter = build_printer(entry, secrets=rt.secrets)
            state = adapter.status()
            return {
                "ok": True,
                "writes_performed": False,
                "printer": {
                    "id": entry.id,
                    "adapter": entry.adapter,
                    "model": entry.model,
                    "base_url": entry.base_url,
                },
                "state": state.to_dict(),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_save",
        description=(
            "Add or update a printer in config.json (upsert). WRITES the config: "
            "confirm must be exactly 'SAVE PRINTER <printer_id>'. Supported adapters: "
            "moonraker (Klipper, default port 7125) and octoprint (port 80/443)."
        ),
    )
    def printer_save(
        printer_id: str,
        confirm: str,
        adapter: str = "moonraker",
        base_url: str = "",
        model: str = "",
        toolheads: int = 1,
        serial: str = "",
        api_key_env: str = "",
        access_code_env: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        try:
            validate_printer_id(printer_id)
            expected = f"SAVE PRINTER {printer_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            chosen = (adapter or "").lower()
            if chosen not in PRINTER_ADAPTERS:
                raise InvalidArgument(
                    f"Unknown printer adapter: {adapter}",
                    hint=f"Supported adapters: {', '.join(PRINTER_ADAPTERS)}",
                )
            if not base_url.strip():
                raise InvalidArgument(
                    "base_url is required",
                    hint=("Moonraker: http://printer.local:7125 ; OctoPrint: http://printer.local"),
                )
            if not 1 <= int(toolheads) <= 9:
                raise InvalidArgument(
                    "toolheads must be between 1 and 9",
                    hint="Snapmaker U1 has 4 toolheads; a Bambu X3D-style machine can have 9.",
                )
            if chosen == "bambu_lan" and not serial.strip():
                raise InvalidArgument(
                    "bambu_lan printers need a serial",
                    hint="Find the serial under Settings > Device on the printer (or in Bambu Studio).",
                )
            entry = PrinterSettings(
                id=printer_id,
                adapter=chosen,
                base_url=base_url.strip(),
                model=model.strip() or None,
                toolheads=int(toolheads),
                serial=serial.strip() or None,
                enabled=bool(enabled),
                api_key_env=api_key_env.strip() or None,
                access_code_env=access_code_env.strip() or None,
            )
            path = save_printer(rt.loaded, entry)
            return {
                "ok": True,
                "writes_performed": True,
                "printer": entry.model_dump(mode="json", exclude_none=True),
                "config_path": str(path),
                "hint": f"Call printer_status({printer_id!r}) to test the connection.",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_remove",
        description=(
            "Remove a printer from config.json. WRITES the config: confirm must be "
            "exactly 'REMOVE PRINTER <printer_id>'."
        ),
    )
    def printer_remove(printer_id: str, confirm: str) -> dict[str, Any]:
        try:
            validate_printer_id(printer_id)
            expected = f"REMOVE PRINTER {printer_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            _printer_entry(rt, printer_id)
            path = remove_printer(rt.loaded, printer_id)
            return {
                "ok": True,
                "writes_performed": True,
                "printer_id": printer_id,
                "config_path": str(path),
                "hint": "printer_list shows the remaining printers.",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="send",
        description=(
            "Upload a successful slice's G-code to a printer and, by default, start "
            "the print. The printer must be idle and its model must match the slice "
            "target. confirm must be exactly 'PRINT <printer_id> <slice_id>'. Runs as "
            "an async job (poll job_status)."
        ),
    )
    def send(
        slice_id: str,
        printer_id: str,
        confirm: str,
        start: bool = True,
    ) -> dict[str, Any]:
        try:
            validate_slice_id(slice_id)
            validate_printer_id(printer_id)
            expected = f"PRINT {printer_id} {slice_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            store = SliceStore(rt.workspace)
            record = store.load(slice_id)
            if not record.get("ok"):
                raise InvalidArgument(
                    f"Slice {slice_id} did not succeed; nothing to send",
                    details={"error": record.get("error")},
                    hint="Fix the model or target and call slice_run again.",
                )
            gcode_name = (record.get("gcode") or {}).get("file")
            if not gcode_name:
                raise NotFound(
                    f"Slice {slice_id} has no G-code recorded",
                    hint="Slice again with slice_run.",
                )
            gcode = store.directory(slice_id) / gcode_name
            if not gcode.is_file():
                raise NotFound(
                    f"G-code file is missing: {gcode}",
                    hint="Slice again; the file may have been removed.",
                )
            entry = _printer_entry(rt, printer_id)
            sliced_for = record.get("printer_model")
            if entry.model and sliced_for and entry.model != sliced_for:
                raise InvalidArgument(
                    f"Slice was made for {sliced_for!r} but printer {printer_id} is "
                    f"{entry.model!r}",
                    hint="Slice with a target whose printer_model matches this printer.",
                )
            used = [int(item) for item in (record.get("metadata") or {}).get("toolheads", [])]
            if used and max(used) >= entry.toolheads:
                raise InvalidArgument(
                    f"G-code uses tool T{max(used)} but printer {printer_id} declares "
                    f"{entry.toolheads} toolhead(s)",
                    details={"toolheads_used": used, "configured": entry.toolheads},
                    hint=(
                        "Add the missing toolheads with printer_save(toolheads=...), "
                        "or slice for a machine with more extruders."
                    ),
                )
            used = [int(item) for item in (record.get("metadata") or {}).get("toolheads", [])]
            if used and max(used) >= entry.toolheads:
                raise InvalidArgument(
                    f"G-code uses tool T{max(used)} but printer {printer_id} declares "
                    f"{entry.toolheads} toolhead(s)",
                    details={"toolheads_used": used, "configured": entry.toolheads},
                    hint=(
                        "Add the missing toolheads with printer_save(toolheads=...), "
                        "or slice for a machine with more extruders."
                    ),
                )
            adapter = build_printer(entry, secrets=rt.secrets)
            state = adapter.status()
            if state.busy:
                raise InvalidArgument(
                    f"Printer {printer_id} is busy ({state.state})",
                    hint="Wait for it to finish, or call printer_cancel first.",
                )
            remote_name = f"{slice_id}.gcode"
            runner = rt.runner
            if runner is None:  # pragma: no cover - Runtime always builds one
                raise ConfigError("No job runner is available")

            def work(job_id: str) -> dict[str, Any]:
                rt.store.set_progress(job_id, 0.2, message="uploading G-code")
                uploaded = adapter.upload(gcode, name=remote_name)
                rt.store.set_progress(job_id, 0.7, message="starting print")
                if start:
                    adapter.start(uploaded)
                return {
                    "printer_id": printer_id,
                    "slice_id": slice_id,
                    "filename": uploaded,
                    "started": bool(start),
                    "printer_state_before": state.to_dict(),
                    "bytes": gcode.stat().st_size,
                    "sha256": sha256_file(gcode),
                }

            job = runner.submit(
                "print.send",
                work,
                params={
                    "printer_id": printer_id,
                    "slice_id": slice_id,
                    "start": bool(start),
                },
                provider=printer_id,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "printer_id": printer_id,
                "slice_id": slice_id,
                "filename": remote_name,
                "started": bool(start),
                "printer_state": state.to_dict(),
                "hint": f"Poll job_status({job.id!r}) for the upload/start result.",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_cancel",
        description=(
            "Cancel the current print on a printer. WRITES to the printer: confirm "
            "must be exactly 'CANCEL PRINT <printer_id>'."
        ),
    )
    def printer_cancel(printer_id: str, confirm: str) -> dict[str, Any]:
        try:
            validate_printer_id(printer_id)
            expected = f"CANCEL PRINT {printer_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            entry = _printer_entry(rt, printer_id)
            adapter = build_printer(entry, secrets=rt.secrets)
            adapter.cancel()
            return {
                "ok": True,
                "writes_performed": True,
                "printer_id": printer_id,
                "cancelled": True,
                "hint": f"Call printer_status({printer_id!r}) to confirm it is idle.",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="asset_export",
        description=(
            "Copy a built artifact out of the workspace to a path the user can open "
            "(for example ~/Desktop/bracket.stl). artifact is 'stl', 'step' or "
            "'script'. Defaults to the workspace exports/ directory. WRITES the "
            "destination file: confirm must be exactly 'EXPORT <asset_id>'."
        ),
    )
    def asset_export(
        asset_id: str,
        confirm: str,
        revision: str = "",
        artifact: str = "stl",
        out_path: str = "",
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"EXPORT {asset_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(f"Asset {asset_id} has no revisions")
            record = next(
                (item for item in manifest.get("revisions", []) if item.get("revision") == target),
                {},
            )
            directory = assets.revision_dir(asset_id, target)
            name = str(manifest.get("name") or asset_id)

            if artifact == "script":
                source = directory / "model.py"
                suffix = ".py"
            elif artifact in {"stl", "step"}:
                stored = f"model.{artifact}"
                if stored not in (record.get("artifacts") or {}):
                    raise NotFound(
                        f"Revision {target} has no {artifact} artifact",
                        hint=f"Call model_build with asset_id {asset_id!r} first.",
                    )
                source = directory / stored
                suffix = f".{artifact}"
            else:
                raise InvalidArgument(
                    "artifact must be 'stl', 'step' or 'script'",
                    details={"artifact": artifact},
                )
            if not source.is_file():
                raise NotFound(f"Artifact missing on disk: {source}")

            if out_path:
                destination = Path(out_path).expanduser()
            else:
                destination = rt.workspace.root / "exports" / f"{name}-{target}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "artifact": artifact,
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
                "hint": "Open the file in a slicer, or import it into the printer's web UI.",
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
                        "reference": (item.get("reference") or {}).get("file"),
                    }
                    for item in manifest.get("revisions", [])
                ],
                "script": script,
                "params": params,
                "reference": (params.get("reference") or {}).get("file"),
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
