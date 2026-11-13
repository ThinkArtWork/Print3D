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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from . import __version__
from .assets import AssetStore, validate_asset_id, validate_name
from .cad import run_script
from .capabilities import build_capabilities, build_system_status
from .config import ImageProviderSettings, LoadedConfig, Secrets, load_all
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
from .preview import DEFAULT_VIEWS, render_views
from .registry import Registry, builtin_registry
from .script_policy import check_script, summarise
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
