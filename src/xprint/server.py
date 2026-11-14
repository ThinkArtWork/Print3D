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

import numpy as np
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from . import __version__
from .assets import INPUT_STEP, AssetStore, validate_asset_id, validate_name
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
from .drawing import DEFAULT_VIEWS as DRAWING_VIEWS
from .drawing import run_drawing
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
from .mesh_fit import DEFAULT_MAX_TRIANGLES, SUPPORTED_SUFFIXES, fit_script, target_spec
from .preview import DEFAULT_VIEWS, render_views
from .printability import (
    THIN_WALL_MM,
    candidate_orientations,
    evaluate_orientation,
    mesh_from_stl,
    printability_report,
)
from .printers import (
    PRINTER_ADAPTERS,
    build_printer,
    normalise_flow_calibration,
    normalise_slots,
    validate_http_config,
    validate_printer_id,
)
from .registry import Registry, builtin_registry
from .runner import JobRunner
from .script_policy import check_script, summarise
from .slicers import (
    BUILD_PLATES,
    PreparedProfiles,
    SliceOptions,
    SliceStore,
    build_command,
    find_slicer,
    new_slice_id,
    normalise_build_plate,
    parse_machine_bed,
    probe_slicer,
    resolve_profiles,
    slice_stl,
    validate_slice_id,
)
from .viewer import (
    entry_from_stl,
    viewer_alias_path,
    write_collection_viewer,
    write_legacy_redirect,
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

Every successful build also writes a self-contained interactive viewer
(`viewer.html`) next to `model.stl` and returns `viewer_file` — `model_build`,
`blender_run`, and the `generate_local` / `generate_cloud` jobs all do this.
As soon as a model exists, call `present` with that `viewer_file` so the user
gets a one-click 3D preview in the conversation; do not wait to be asked.
`model_preview` also echoes `viewer_file` when it is already there, and
`model_view` can regenerate it for older revisions.

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


def _bed_for_target(rt: Runtime, target: str) -> tuple[float, float] | None:
    """Resolve a target's bed size, or None when no target was given."""
    if not target:
        return None
    configured = rt.loaded.config.target(target)
    machine, _process, _filaments = resolve_profiles(configured)
    return parse_machine_bed(machine)


GCODE_CAPABLE_ADAPTERS = {"moonraker", "octoprint"}


def _printer_capabilities(entry: Any, *, adapter: Any = None) -> dict[str, Any]:
    """What this printer/adapter pair can do beyond the basic upload/start contract."""
    calibration = dict(entry.flow_calibration or {})
    return {
        "build_plates": list(entry.build_plates or BUILD_PLATES),
        "build_plate_default": entry.build_plate or None,
        "build_plate_detection": bool(entry.plate_keys) or bool(getattr(adapter, "plate_keys", [])),
        "slots": len(entry.slots or []),
        "flow_calibration": {
            "supported": bool(calibration.get("enable_gcode"))
            and entry.adapter.lower() in GCODE_CAPABLE_ADAPTERS,
            "factors_configured": bool(calibration.get("factors_url")),
            "trim_unused_tools": bool(calibration.get("trim_unused_tools")),
            "tool_macros": list(calibration.get("tool_macros") or []),
        },
    }


def _apply_slot(
    rt: Runtime,
    options: SliceOptions | None,
    *,
    printer_id: str,
    slot: int,
    build_plate: str = "",
) -> tuple[SliceOptions | None, dict[str, Any] | None]:
    """Resolve a printer's filament slot and build plate for one slice."""
    entry = _printer_entry(rt, printer_id) if printer_id else None
    plate = normalise_build_plate(build_plate)
    if not plate and entry is not None:
        plate = normalise_build_plate(entry.build_plate)
    slot = int(slot) if slot is not None else -1
    if plate and entry is not None and entry.build_plates and plate not in entry.build_plates:
        raise InvalidArgument(
            f"printer {printer_id} does not list build plate {plate!r}",
            details={"supported": list(entry.build_plates)},
            hint="Pick one of the supported plates or update printer_save(build_plates=[...]).",
        )
    if slot < 0 and not plate:
        return options, None
    if slot > 15:
        raise InvalidArgument("slot must be between 0 and 15")
    info: dict[str, Any] = {
        "slot": slot if slot >= 0 else None,
        "printer": printer_id or None,
        "build_plate": plate or None,
    }
    material = ""
    if entry is not None and slot >= 0:
        table = list(entry.slots or [])
        if table and slot >= len(table):
            raise InvalidArgument(
                f"printer {printer_id} has {len(table)} configured slot(s), not {slot + 1}",
                hint="Update the slot table with printer_save(slots=[...]).",
            )
        meta = table[slot] if slot < len(table) else {}
        material = str(meta.get("material") or "").upper()
        info.update(
            {
                "material": material or None,
                "color": meta.get("color"),
                "name": meta.get("name"),
                "configured": bool(meta),
            }
        )
    requested = (options.material if options is not None else "").strip().upper()
    if material and requested and material != requested:
        raise InvalidArgument(
            f"slot {slot} on {printer_id} holds {material}, but material={requested} was requested",
            details={"slot_material": material, "requested": requested},
            hint="Pick the slot with the requested material, or update printer_save(slots=[...]).",
        )
    chosen = material or requested
    if options is None:
        options = SliceOptions()
    if slot >= 0:
        options.toolhead = slot
    if chosen:
        options.material = chosen
    if plate:
        options.build_plate = plate
    if chosen:
        info["material"] = chosen
    return options, info


def _slice_options(
    *,
    material: str = "",
    layer_height: float = 0,
    infill: str = "",
    infill_density: float = -1,
    walls: int = 0,
    top_shells: int = 0,
    bottom_shells: int = 0,
    strength: str = "",
    bed_temp: int = 0,
    nozzle_temp: int = 0,
    build_plate: str = "",
    flow_ratio: float = 0,
    pressure_advance: float = -1,
    line_width: float = 0,
    outer_wall_line_width: float = 0,
    inner_wall_line_width: float = 0,
    top_surface_line_width: float = 0,
    arc_fitting: bool = False,
    fan_speed: int = -1,
    aux_fan_speed: int = -1,
    fan_first_layers: int = -1,
) -> SliceOptions | None:
    """Build SliceOptions from MCP arguments (0 / -1 mean "unset")."""

    def positive(value: float | int) -> float | None:
        return float(value) if value and value > 0 else None

    def count(value: int) -> int | None:
        return int(value) if value and value > 0 else None

    density = float(infill_density) if infill_density is not None and infill_density >= 0 else None
    flow = float(flow_ratio) if flow_ratio and flow_ratio > 0 else None
    advance = (
        float(pressure_advance) if pressure_advance is not None and pressure_advance >= 0 else None
    )
    fan = int(fan_speed) if fan_speed is not None and fan_speed >= 0 else None
    aux_fan = int(aux_fan_speed) if aux_fan_speed is not None and aux_fan_speed >= 0 else None
    first_layers = (
        int(fan_first_layers) if fan_first_layers is not None and fan_first_layers >= 0 else None
    )
    options = SliceOptions(
        material=(material or "").strip(),
        layer_height=positive(layer_height),
        infill=(infill or "").strip(),
        infill_density=density,
        walls=count(walls),
        top_shells=count(top_shells),
        bottom_shells=count(bottom_shells),
        strength=(strength or "").strip(),
        bed_temp=count(bed_temp),
        nozzle_temp=count(nozzle_temp),
        flow_ratio=flow,
        pressure_advance=advance,
        line_width=positive(line_width),
        outer_wall_line_width=positive(outer_wall_line_width),
        inner_wall_line_width=positive(inner_wall_line_width),
        top_surface_line_width=positive(top_surface_line_width),
        arc_fitting=bool(arc_fitting),
        fan_speed=fan,
        aux_fan_speed=aux_fan,
        fan_first_layers=first_layers,
    )
    if options.resolved() == SliceOptions().resolved():
        return None
    return options


def _asset_viewer(
    rt: Runtime, assets: AssetStore, manifest: dict[str, Any], *, selected: str = ""
) -> dict[str, Any]:
    """Best-effort version-switchable viewer for every built revision of an asset."""

    asset_id = manifest["asset_id"]
    entries: list[dict[str, Any]] = []
    for item in manifest.get("revisions", []):
        revision = str(item.get("revision") or "")
        if not revision:
            continue
        stl = assets.revision_dir(asset_id, revision) / "model.stl"
        if not stl.is_file():
            continue
        checks = (item.get("build") or {}).get("checks") or {}
        try:
            entries.append(
                entry_from_stl(
                    stl,
                    revision=revision,
                    created_at=str(item.get("created_at") or ""),
                    volume_mm3=checks.get("volume_mm3"),
                )
            )
        except (OSError, ValueError):
            continue
    if not entries:
        return {}
    name = str(manifest.get("name") or "")
    try:
        info = write_collection_viewer(
            assets.asset_dir(asset_id),
            entries,
            title=name or asset_id,
            subtitle=f"{len(entries)} revision(s) · interactive 3D",
            name=name,
            selected=selected,
        )
    except (OSError, ValueError) as exc:
        return {"viewer_error": f"{type(exc).__name__}: {exc}"}
    for entry in entries:
        legacy_dir = assets.revision_dir(asset_id, entry["revision"])
        if (legacy_dir / "viewer.html").is_file():
            try:
                write_legacy_redirect(legacy_dir, title=name or asset_id)
            except OSError:
                continue
    viewer_file = info.get("alias_path") or info["path"]
    return {
        "viewer_path": rt.workspace.rel(viewer_file),
        "viewer_file": str(viewer_file),
        "viewer_revision": selected or entries[-1]["revision"],
        "viewer_revisions": len(entries),
        "viewer_triangles": info["triangles"],
        "viewer_bytes": info["bytes"],
    }


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
            "model.stl/model.step plus a self-contained interactive viewer.html. "
            "Pass `input` with a .step/.stp path to import an existing part: the "
            "file is copied into the revision as input.step and its absolute path "
            "is exposed to the script as the module global INPUT_STEP, so a script "
            "can do `part = import_step(INPUT_STEP)` and edit or re-validate it "
            "without redrawing geometry. This is the feedback step of the closed "
            "loop: read the checks, fix the script, call model_write again, and call "
            "`present` with viewer_file so the user can open the 3D preview. WRITES "
            "files: confirm must be exactly 'BUILD <asset_id>'."
        ),
    )
    def model_build(
        asset_id: str,
        confirm: str,
        revision: str = "",
        input: str = "",
    ) -> dict[str, Any]:
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

            input_record: dict[str, Any] = {}
            input_step: Path | None = None
            if input:
                input_record = assets.store_input(asset_id, target, input)
                input_step = assets.revision_dir(asset_id, target) / INPUT_STEP

            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/builds/{asset_id}_{target}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_script(
                script_path,
                workdir=workdir,
                input_step=input_step,
                timeout_s=cad.timeout_s,
                python=cad.python,
            )

            stored: dict[str, Path] = {}
            for value in result.artifacts.values():
                path = Path(value)
                if path.is_file():
                    stored[f"model{path.suffix or '.stl'}"] = path
            build = result.to_dict()
            if input_record:
                build["input"] = input_record
                rt.record_write(f"assets/{asset_id}/{target}/{INPUT_STEP}")
            record = assets.record_build(
                asset_id,
                target,
                build=build,
                artifacts=stored,
            )
            rt.record_write(f"assets/{asset_id}/{target}/model.stl")

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(rt, assets, assets.load(asset_id), selected=target)

            hint = (
                "Review the checks, open viewer_file for an interactive look, then slice. "
                "If a check failed, fix the script and call model_write followed by model_build."
            )
            if input_record:
                hint = (
                    "STEP input stored as input.step; the script reads it through the "
                    "INPUT_STEP global. " + hint
                )
            if not result.ok:
                hint = (
                    "Build failed; read error/stderr_tail above, fix model.py and "
                    "call model_write, then model_build again."
                )
            stored_input = assets.stored_input(asset_id, target)
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
                **({"input": stored_input} if stored_input else {}),
                "error": result.error,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
                **viewer,
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_drawing",
        description=(
            "Project a built revision's model.step into an engineering drawing: one "
            "sheet with "
            + ", ".join(DRAWING_VIEWS)
            + " views laid out third-angle, dashed hidden lines (orthographic views) and "
            "view labels, "
            "written as drawing.svg and drawing.dxf inside the revision. Use it "
            "after model_build when a printed sheet is wanted, or to hand the part "
            "to someone without a slicer. Returns the sheet size and per-view edge "
            "counts; export the files with asset_export(artifact='svg' or 'dxf'). "
            "WRITES files: confirm must be exactly 'DRAWING <asset_id>'."
        ),
    )
    def model_drawing(
        asset_id: str,
        confirm: str,
        revision: str = "",
        views: str = "",
        scale: float = 1.0,
        hidden: bool = True,
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"DRAWING {asset_id}"
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
            record = next(
                (item for item in manifest.get("revisions", []) if item.get("revision") == target),
                {},
            )
            if "model.step" not in (record.get("artifacts") or {}):
                raise NotFound(
                    f"Revision {target} has no STEP artifact to draw",
                    hint=(
                        f"Call model_build with asset_id {asset_id!r} first; "
                        "drawings are projected from model.step."
                    ),
                )
            step_path = assets.revision_dir(asset_id, target) / "model.step"
            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/drawings/{asset_id}_{target}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_drawing(
                step_path,
                workdir=workdir,
                views=views,
                scale=scale,
                hidden=hidden,
                timeout_s=cad.timeout_s,
                python=cad.python,
            )
            if not result.ok:
                return {
                    "ok": False,
                    "writes_performed": False,
                    "asset_id": asset_id,
                    "revision": target,
                    "error": result.error,
                    "timed_out": result.timed_out,
                    "stdout_tail": result.stdout[-2000:],
                    "stderr_tail": result.stderr[-2000:],
                    "hint": result.hint
                    or "Fix the geometry, rebuild, then call model_drawing again.",
                }
            files = {key: Path(value) for key, value in result.files.items()}
            record_out = assets.record_output(
                asset_id,
                target,
                key="drawing",
                meta={
                    "views": [item["name"] for item in (result.checks.get("views") or [])],
                    "scale": result.checks.get("scale"),
                    "hidden": result.checks.get("hidden"),
                    "sheet_mm": result.checks.get("sheet_mm"),
                    "warnings": result.warnings,
                },
                files=files,
            )
            for name in sorted(files):
                rt.record_write(f"assets/{asset_id}/{target}/{name}")
            drawing = record_out.get("drawing") or {}
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "duration_s": round(result.duration_s, 2),
                "sheet_mm": result.checks.get("sheet_mm"),
                "views": result.checks.get("views"),
                "edges": result.checks.get("edges"),
                "files": sorted(drawing.get("files") or {}),
                "warnings": result.warnings,
                "hint": (
                    "Open drawing.svg in a browser or drawing.dxf in a CAD tool; "
                    "copy them out with asset_export(artifact='svg') or ('dxf')."
                ),
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
            viewer_file = viewer_alias_path(
                assets.asset_dir(asset_id),
                name=str(manifest.get("name") or ""),
                asset_id=asset_id,
            )
            payload = {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": target,
                "image_path": rt.workspace.rel(cache),
                "viewer_path": rt.workspace.rel(viewer_file) if viewer_file.is_file() else None,
                "viewer_file": str(viewer_file) if viewer_file.is_file() else None,
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
        name="model_view",
        description=(
            "Write one self-contained interactive 3D viewer (HTML, orbit / pan / "
            "zoom, no server or CDN) per asset. The file embeds every built "
            "revision and has a revision dropdown, so only one file is ever handed "
            "to the user. Open it in a browser or pass it to `present`. WRITES one "
            "file in the asset directory: confirm must be exactly 'VIEW <asset_id>'."
        ),
    )
    def model_view(asset_id: str, confirm: str, revision: str = "") -> dict[str, Any]:
        try:
            token = validate_asset_id(asset_id)
            expected = f"VIEW {token}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )

            assets = AssetStore(rt.workspace)
            manifest = assets.load(token)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {token} has no revisions",
                    hint="Call model_write first.",
                )
            directory = assets.revision_dir(token, target)
            stl = directory / "model.stl"
            if not stl.is_file():
                raise InvalidArgument(
                    f"Revision {target} has no built model yet",
                    hint=(
                        f"Call model_build with asset_id {token!r} and "
                        f"confirm 'BUILD {token}' first."
                    ),
                )

            viewer = _asset_viewer(rt, assets, manifest, selected=target)
            if "viewer_error" in viewer:
                raise InvalidArgument(
                    f"Cannot build the 3D viewer: {viewer['viewer_error']}",
                    hint="Reduce the triangle count or inspect the STL.",
                )
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": token,
                "revision": target,
                "viewer_path": viewer["viewer_path"],
                "viewer_file": viewer["viewer_file"],
                "viewer_revision": viewer["viewer_revision"],
                "viewer_revisions": viewer["viewer_revisions"],
                "file": Path(viewer["viewer_file"]).name,
                "triangles": viewer["viewer_triangles"],
                "bytes": viewer["viewer_bytes"],
                "hint": (
                    "Open the HTML in a browser to switch revisions, orbit, pan, "
                    "zoom, spin and save a PNG. Call `present` with viewer_file to "
                    "give the user an open action in DSH."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="blender_run",
        description=(
            "Turn a bpy Python script into a printable mesh by running Blender in "
            "background mode (blender -b --factory-startup --python scene.py), then "
            "exporting every mesh object to model.stl + model.glb, writing an "
            "interactive viewer.html and inspecting it. "
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

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=revision,
                )

            hint = (
                "Review checks, open viewer_file for an interactive look, or call "
                "model_preview. Fix scene.py and call blender_run with asset_id to "
                "add the next revision."
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
                **viewer,
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="mesh_fit",
        description=(
            "Fit an existing STL/GLB/OBJ mesh to a real millimetre size and turn "
            "it into a versioned XPrint asset. This is the finishing step for "
            "image-to-3D: keep the generated shape, repair it, scale it, seat it on "
            "z=0, centre it, decimate to a preview-friendly triangle budget, then "
            "export STL/GLB and a named viewer. Sizing is explicit: pass "
            "size_mm=[x,y,z] for an exact envelope (non-uniform scale), or "
            "max_dimension_mm=<longest side> to scale uniformly and preserve the "
            "image proportions. Pass source= for a file path, or asset_id= (plus "
            "optional revision=) to use an asset's stored model.stl. WRITES files "
            "and runs Blender: confirm must be exactly 'FIT <name>'."
        ),
    )
    def mesh_fit(
        name: str,
        confirm: str,
        size_mm: list[float] | None = None,
        max_dimension_mm: float = 0.0,
        source: str = "",
        asset_id: str = "",
        revision: str = "",
        max_triangles: int = DEFAULT_MAX_TRIANGLES,
        image: str = "",
        blender_path: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"FIT {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if bool(source) == bool(asset_id):
                raise InvalidArgument(
                    "Pass exactly one of source= or asset_id=",
                    hint="source= is a STL/GLB/OBJ path; asset_id= fits a stored asset revision.",
                )
            try:
                exact, uniform = target_spec(size_mm, max_dimension_mm)
            except ValueError as exc:
                raise InvalidArgument(
                    str(exc),
                    hint="Use size_mm=[x,y,z] for an exact envelope, or max_dimension_mm for uniform scaling.",
                ) from None
            if not 1_000 <= int(max_triangles) <= 400_000:
                raise InvalidArgument(
                    "max_triangles must be between 1000 and 400000",
                    details={"max_triangles": max_triangles},
                )

            assets = AssetStore(rt.workspace)
            origin: dict[str, Any]
            if asset_id:
                source_asset = assets.load(validate_asset_id(asset_id))
                chosen = revision or assets.latest_revision(source_asset)
                known = {item.get("revision") for item in source_asset.get("revisions", [])}
                if not chosen or chosen not in known:
                    raise NotFound(
                        f"Revision {revision!r} not found on {source_asset['asset_id']}",
                        details={"available": sorted(v for v in known if v)},
                    )
                source_path = assets.revision_dir(source_asset["asset_id"], chosen) / "model.stl"
                if not source_path.is_file():
                    raise NotFound(
                        f"Stored model.stl not found for {source_asset['asset_id']} {chosen}",
                        hint="Fit the source file directly with source= instead.",
                    )
                origin = {
                    "asset_id": source_asset["asset_id"],
                    "revision": chosen,
                    "name": source_asset.get("name", ""),
                }
            else:
                source_path = Path(source).expanduser().resolve()
                if not source_path.is_file():
                    raise NotFound(
                        f"Mesh not found: {source_path}",
                        hint="Pass an existing .stl, .glb, .gltf or .obj file.",
                    )
                if source_path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise InvalidArgument(
                        f"Unsupported mesh format: {source_path.suffix or '(none)'}",
                        hint="Supported: " + ", ".join(sorted(SUPPORTED_SUFFIXES)),
                    )
                origin = {"path": str(source_path)}

            script = fit_script(
                source_path,
                exact,
                int(max_triangles),
                max_dimension_mm=uniform,
            )
            issues = check_blender_script(script)
            errors = [issue.as_dict() for issue in issues if issue.severity == "error"]
            if errors:
                raise InvalidArgument(
                    "The generated fit script failed safety checks; nothing was executed",
                    details={"issues": errors},
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

            manifest = assets.create(name, kind="blender")
            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt=(
                    f"Fit {source_path.name} to "
                    + (
                        f"{exact[0]:g} × {exact[1]:g} × {exact[2]:g} mm"
                        if exact
                        else f"a {uniform:g} mm longest side (uniform)"
                    )
                    + f" with at most {int(max_triangles)} triangles"
                ),
                model="mesh_fit",
                modeler="mesh_fit",
                checks={"policy": summarise(issues)},
                reference=image_path,
                reference_meta={"source_path": str(image_path)} if image_path else None,
                kind="blender",
            )
            revision = record["revision"]
            render_config = rt.loaded.config.render
            workdir = rt.workspace.safe_path(
                f"cache/mesh_fit/{manifest['asset_id']}_{revision}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_blender(
                script,
                workdir=workdir,
                image=image_path,
                blender_path=blender_path or render_config.blender_path,
                timeout_s=float(timeout_s or render_config.timeout_s),
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

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=revision,
                )

            hint = (
                "Open viewer_file to inspect the fitted mesh, then call model_preview "
                "or printability_check."
                if result.ok
                else "Read stderr_tail, check the source mesh, then call mesh_fit again."
            )
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "revision": revision,
                "engine": "blender",
                "source": origin,
                "requested_size_mm": list(exact) if exact else None,
                "max_dimension_mm": uniform or None,
                "max_triangles": int(max_triangles),
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(stored),
                "error": result.error,
                "stdout_tail": result.stdout[-1500:],
                "stderr_tail": result.stderr[-1500:],
                **viewer,
                "hint": hint,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="generate_cloud",
        description=(
            "Generate a 3D model with a cloud provider (tripo, hunyuan_openai or "
            "hunyuan TC3) in the background. Configure it in providers.cloud.<id> "
            "(adapter, key env, scale, timeout). XPrint submits the task, polls it, "
            "downloads the result, converts it to STL, records real geometry checks "
            "and stores a versioned asset. Pass optional size_mm=[x,y,z] or "
            "max_dimension_mm=<longest side> to run the same mesh fit after "
            "generation; the raw mesh is kept as source.stl. Returns a job_id: poll "
            "job_status until succeeded, then call model_preview. WRITES files and "
            "spends provider credits: confirm must be exactly 'CLOUD <name>'."
        ),
    )
    def generate_cloud(
        provider: str,
        name: str,
        confirm: str,
        prompt: str = "",
        image: str = "",
        timeout_s: int = 0,
        size_mm: list[float] | None = None,
        max_dimension_mm: float = 0.0,
        max_triangles: int = DEFAULT_MAX_TRIANGLES,
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
            try:
                exact, uniform = (
                    target_spec(size_mm, max_dimension_mm)
                    if (size_mm or max_dimension_mm)
                    else (None, 0.0)
                )
            except ValueError as exc:
                raise InvalidArgument(
                    str(exc),
                    hint="Use size_mm=[x,y,z] for an exact envelope, or max_dimension_mm for uniform scaling.",
                ) from None
            if not 1_000 <= int(max_triangles) <= 400_000:
                raise InvalidArgument(
                    "max_triangles must be between 1000 and 400000",
                    details={"max_triangles": max_triangles},
                )
            fit_requested = bool(exact or uniform)

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
            adapter = str(entry.get("adapter") or provider).strip().lower().replace("-", "_")

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
            if adapter == "hunyuan_openai" and not api_key:
                raise AdapterUnavailable(
                    "No API key for the Hunyuan OpenAI-compatible provider",
                    hint=f"Set {entry.get('api_key_env') or 'HUNYUAN3D_API_KEY'}.",
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
                    hint=(
                        "Cloud meshes often use metres: use 1000 for millimetres, "
                        "then set a smaller value if the generated model is oversized."
                    ),
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

                    fit_info: dict[str, Any] = {}
                    if fit_requested:
                        raw_stl = staging / "source.stl"
                        shutil.copyfile(stl_path, raw_stl)
                        raw_bytes = stl_path.stat().st_size
                        fit_dir = staging / "fit"
                        fit_dir.mkdir(parents=True, exist_ok=True)
                        fit_result = run_blender(
                            fit_script(
                                stl_path,
                                exact,
                                int(max_triangles),
                                max_dimension_mm=uniform,
                            ),
                            workdir=fit_dir,
                            image=image_path,
                            blender_path=rt.loaded.config.render.blender_path,
                            timeout_s=float(rt.loaded.config.render.timeout_s),
                        )
                        fitted = fit_result.artifacts.get("model.stl")
                        if not fit_result.ok or not fitted or not Path(fitted).is_file():
                            raise AdapterUnavailable(
                                f"{provider} generated a mesh, but the size fit failed",
                                hint="The raw mesh is retained in the job cache; retry with mesh_fit.",
                                details={"stderr_tail": fit_result.stderr[-1200:]},
                            )
                        shutil.copyfile(fitted, stl_path)
                        artifacts["source.stl"] = raw_stl
                        fit_info = {
                            "mode": "uniform" if uniform else "exact",
                            "requested_size_mm": list(exact) if exact else None,
                            "max_dimension_mm": uniform or None,
                            "max_triangles": int(max_triangles),
                            "source_bytes": raw_bytes,
                            "checks": fit_result.checks,
                        }

                    checks = stats_for_stl(stl_path)
                    checks["engine"] = f"cloud:{provider}"
                    checks["provider_task"] = task_id
                    if fit_info:
                        checks["fit"] = fit_info
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
                    viewer = _asset_viewer(
                        rt,
                        assets,
                        assets.load(manifest["asset_id"]),
                        selected=record["revision"],
                    )
                    return {
                        "asset_id": manifest["asset_id"],
                        "name": name,
                        "revision": record["revision"],
                        "provider": provider,
                        "mode": mode,
                        "task_id": task_id,
                        "checks": checks,
                        "artifacts": sorted(record.get("artifacts", {})),
                        "fit": fit_info or None,
                        **viewer,
                    }
                finally:
                    client.close()

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "cloud.generate",
                work,
                params={
                    "provider": provider,
                    "name": name,
                    "mode": mode,
                    "fit": fit_requested,
                },
                provider=provider,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "provider": provider,
                "adapter": adapter,
                "mode": mode,
                "fit_requested": fit_requested,
                "timeout_s": budget,
                "hint": "Poll job_status(job_id); on success open result.viewer_file or call model_preview(asset_id).",
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
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=record["revision"],
                )
                return {
                    "asset_id": manifest["asset_id"],
                    "name": name,
                    "revision": record["revision"],
                    "provider": provider,
                    "mode": mode,
                    "duration_s": round(result.duration_s, 2),
                    "checks": checks,
                    "artifacts": sorted(record.get("artifacts", {})),
                    **viewer,
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
                "hint": "Poll job_status(job_id); on success open result.viewer_file or call model_preview(asset_id).",
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printability_check",
        description=(
            "Read-only geometric printability report for a built revision: overhang "
            "area, estimated support volume, bed contact, thin-wall estimate, bed fit, "
            "a 0-100 score and actionable issues. Pass target to include its bed size."
        ),
    )
    def printability_check(asset_id: str, target: str = "", revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then printability_check.",
                )
            bed = _bed_for_target(rt, target)
            triangles, normals, areas = mesh_from_stl(stl)
            report = printability_report(triangles, normals, areas, bed=bed)
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target or None,
                "thin_wall_mm": THIN_WALL_MM,
                **report,
                "hint": (
                    "Fix error/warning issues before slicing. If overhang dominates, "
                    "call orientation_plan for ranked build orientations."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)
        except ValueError as exc:
            return tool_error(InvalidArgument(f"Could not inspect the mesh: {exc}"))

    @server.tool(
        name="orientation_plan",
        description=(
            "Read-only build-orientation ranking: evaluates 24 axis-aligned rotations "
            "(6 down axes x 4 spins), compares overhang area, estimated support volume, "
            "height and bed contact, and returns the best rotations with metrics."
        ),
    )
    def orientation_plan(
        asset_id: str, target: str = "", revision: str = "", limit: int = 5
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            if not 1 <= int(limit) <= 24:
                raise InvalidArgument("limit must be between 1 and 24")
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then orientation_plan.",
                )
            bed = _bed_for_target(rt, target)
            triangles, normals, areas = mesh_from_stl(stl)
            current = evaluate_orientation(triangles, normals, areas, np.eye(3), bed=bed)
            rows: list[dict[str, Any]] = []
            for down, angle, rotation in candidate_orientations():
                evaluated = evaluate_orientation(triangles, normals, areas, rotation, bed=bed)
                rows.append(
                    {
                        "score": evaluated["score"],
                        "fits_bed": evaluated["fits_bed"],
                        "rotation": evaluated["rotation"],
                        "down_axis": down,
                        "spin_deg": angle,
                        "metrics": evaluated["metrics"],
                    }
                )
            rows.sort(key=lambda item: (-item["score"], item["metrics"]["bbox_mm"][2]))
            top = rows[: int(limit)]
            for index, row in enumerate(top, start=1):
                row["rank"] = index
            best = top[0]
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target or None,
                "bed_mm": list(bed) if bed else None,
                "current": {
                    "score": current["score"],
                    "fits_bed": current["fits_bed"],
                    "metrics": current["metrics"],
                },
                "recommended": best,
                "candidates": top,
                "hint": (
                    "Rotate the part so the chosen down_axis becomes -Z (rotation is "
                    "applied as vertices @ rotation.T). Re-run printability_check after "
                    "reorienting; supports still need slicer settings."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)
        except ValueError as exc:
            return tool_error(InvalidArgument(f"Could not inspect the mesh: {exc}"))

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
                "build_plates": [
                    {"id": key, "label": value["label"], "bed_adjust_c": value["adjust"]}
                    for key, value in BUILD_PLATES.items()
                ],
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
            "checks, preview process/material options (material, layer_height, infill, "
            "infill_density, walls, top_shells, bottom_shells, strength, bed_temp, "
            "nozzle_temp, build_plate, flow_ratio, pressure_advance, line_width, outer_wall_line_width, inner_wall_line_width, top_surface_line_width, arc_fitting, fan_speed, aux_fan_speed, fan_first_layers), optionally pinned to "
            "a printer filament slot (printer + slot: multi-tool or AMS). Read-only: it "
            "reports the overrides and the slot material that slice_run would use."
        ),
    )
    def slice_plan(
        asset_id: str,
        target: str,
        revision: str = "",
        printer: str = "",
        slot: int = -1,
        build_plate: str = "",
        material: str = "",
        layer_height: float = 0,
        infill: str = "",
        infill_density: float = -1,
        walls: int = 0,
        top_shells: int = 0,
        bottom_shells: int = 0,
        strength: str = "",
        bed_temp: int = 0,
        nozzle_temp: int = 0,
        flow_ratio: float = 0,
        pressure_advance: float = -1,
        line_width: float = 0,
        outer_wall_line_width: float = 0,
        inner_wall_line_width: float = 0,
        top_surface_line_width: float = 0,
        arc_fitting: bool = False,
        fan_speed: int = -1,
        aux_fan_speed: int = -1,
        fan_first_layers: int = -1,
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            options = _slice_options(
                material=material,
                layer_height=layer_height,
                infill=infill,
                infill_density=infill_density,
                walls=walls,
                top_shells=top_shells,
                bottom_shells=bottom_shells,
                strength=strength,
                bed_temp=bed_temp,
                nozzle_temp=nozzle_temp,
                build_plate=build_plate,
                flow_ratio=flow_ratio,
                pressure_advance=pressure_advance,
                line_width=line_width,
                outer_wall_line_width=outer_wall_line_width,
                inner_wall_line_width=inner_wall_line_width,
                top_surface_line_width=top_surface_line_width,
                arc_fitting=arc_fitting,
                fan_speed=fan_speed,
                aux_fan_speed=aux_fan_speed,
                fan_first_layers=fan_first_layers,
            )
            options, slot_info = _apply_slot(
                rt, options, printer_id=printer, slot=slot, build_plate=build_plate
            )
            options_summary = options.resolved() if options is not None else None
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
            if configured.overrides or options is not None:
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
                options=options_summary,
                toolhead=int(options.toolhead) if options is not None else 0,
            )
            profile_overrides = {
                "process": options.process_overrides() if options is not None else {},
                "filament": options.filament_overrides() if options is not None else {},
            }
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
            printer_cfg = _printer_entry(rt, printer) if printer else None
            calibration = dict(printer_cfg.flow_calibration or {}) if printer_cfg else {}
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target,
                "printer_model": configured.printer_model,
                "slicer": info.to_dict() if info is not None else None,
                "profiles": prepared.to_dict(),
                "options": options_summary,
                "slot": slot_info,
                "flow_calibration": {
                    "trim_unused_tools": bool(calibration.get("trim_unused_tools")),
                    "tool_macros": list(calibration.get("tool_macros") or []),
                },
                "profile_overrides": profile_overrides,
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
            "OrcaSlicer CLI, optionally with process/material options (material implies "
            "nozzle/bed temperatures; layer_height, infill, infill_density, walls, "
            "top_shells, bottom_shells, strength override the process preset; flow_ratio and "
            "pressure_advance override the filament flow/PA; line_width / outer_wall_line_width / "
            "inner_wall_line_width / top_surface_line_width override extrusion widths; "
            "arc_fitting emits G2/G3 arcs for rounder holes; fan_speed / aux_fan_speed / "
            "fan_first_layers control part and auxiliary cooling; build_plate picks the plate; "
            "printer + slot pin the G-code to a filament slot and take its material/colour). "
            "Runs as an async job (poll job_status). confirm must be exactly "
            "'SLICE <asset_id> <target>'."
        ),
    )
    def slice_run(
        asset_id: str,
        target: str,
        confirm: str,
        revision: str = "",
        printer: str = "",
        slot: int = -1,
        build_plate: str = "",
        timeout_s: float = 0,
        material: str = "",
        layer_height: float = 0,
        infill: str = "",
        infill_density: float = -1,
        walls: int = 0,
        top_shells: int = 0,
        bottom_shells: int = 0,
        strength: str = "",
        bed_temp: int = 0,
        nozzle_temp: int = 0,
        flow_ratio: float = 0,
        pressure_advance: float = -1,
        line_width: float = 0,
        outer_wall_line_width: float = 0,
        inner_wall_line_width: float = 0,
        top_surface_line_width: float = 0,
        arc_fitting: bool = False,
        fan_speed: int = -1,
        aux_fan_speed: int = -1,
        fan_first_layers: int = -1,
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"SLICE {asset_id} {target}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            options = _slice_options(
                material=material,
                layer_height=layer_height,
                infill=infill,
                infill_density=infill_density,
                walls=walls,
                top_shells=top_shells,
                bottom_shells=bottom_shells,
                strength=strength,
                bed_temp=bed_temp,
                nozzle_temp=nozzle_temp,
                build_plate=build_plate,
                flow_ratio=flow_ratio,
                pressure_advance=pressure_advance,
                line_width=line_width,
                outer_wall_line_width=outer_wall_line_width,
                inner_wall_line_width=inner_wall_line_width,
                top_surface_line_width=top_surface_line_width,
                arc_fitting=arc_fitting,
                fan_speed=fan_speed,
                aux_fan_speed=aux_fan_speed,
                fan_first_layers=fan_first_layers,
            )
            options, slot_info = _apply_slot(
                rt, options, printer_id=printer, slot=slot, build_plate=build_plate
            )
            options_summary = options.resolved() if options is not None else None
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
            printer_cfg = _printer_entry(rt, printer) if printer else None

            def work(job_id: str) -> dict[str, Any]:
                rt.store.set_progress(job_id, 0.1)
                result = slice_stl(
                    stl,
                    configured,
                    slice_dir,
                    timeout_s=budget,
                    slicer_path=configured.slicer_path,
                    options=options,
                    flow_calibration=printer_cfg.flow_calibration if printer_cfg else None,
                    slots=printer_cfg.slots if printer_cfg else None,
                    toolhead_count=printer_cfg.toolheads if printer_cfg else None,
                )
                record = SliceStore(rt.workspace).save_result(
                    slice_id=slice_id,
                    asset_id=asset_id,
                    revision=rev,
                    target_name=target,
                    target=configured,
                    result=result,
                    slicer=info,
                    options=options_summary,
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
                params={
                    "asset_id": asset_id,
                    "revision": rev,
                    "target": target,
                    "printer": printer or None,
                    "slot": slot_info,
                    "options": options_summary,
                },
                provider=target,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "slice_id": slice_id,
                "target": target,
                "printer_model": configured.printer_model,
                "options": options_summary,
                "slot": slot_info,
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
        description=(
            "Read a stored slice record, its statistics, process/material options and "
            "G-code path. Read-only."
        ),
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
            "List configured printers with adapter, model, address, toolheads, filament "
            "slots (material/colour) and whether an API key is available. Read-only (no "
            "network probe)."
        ),
    )
    def printer_list() -> dict[str, Any]:
        try:
            entries = []
            for entry in rt.loaded.config.printers:
                key_env = entry.password_env or entry.access_code_env or entry.api_key_env
                key = rt.secrets.provider_key(entry.id, api_key_env=key_env)
                entries.append(
                    {
                        "id": entry.id,
                        "adapter": entry.adapter,
                        "adapter_known": entry.adapter.lower() in PRINTER_ADAPTERS,
                        "model": entry.model,
                        "base_url": entry.base_url,
                        "serial": entry.serial,
                        "storage": entry.storage,
                        "toolheads": entry.toolheads,
                        "slots": list(entry.slots or []),
                        "build_plate": entry.build_plate or None,
                        "build_plates": list(entry.build_plates or []),
                        "plate_keys": list(entry.plate_keys or []),
                        "flow_calibration_configured": bool(
                            (entry.flow_calibration or {}).get("enable_gcode")
                        ),
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
            "Query one printer's live state (state, filename, progress, temperatures), "
            "configured build plate and any printer-stored flow calibration factors, "
            "through its adapter. Read-only."
        ),
    )
    def printer_status(printer_id: str) -> dict[str, Any]:
        try:
            validate_printer_id(printer_id)
            entry = _printer_entry(rt, printer_id)
            adapter = build_printer(entry, secrets=rt.secrets)
            state = adapter.status()
            calibration = None
            reader = getattr(adapter, "flow_calibration", None)
            if callable(reader):
                try:
                    calibration = reader()
                except XPrintError as exc:
                    calibration = {
                        "source": None,
                        "factors": {},
                        "error": error_payload(exc),
                    }
            return {
                "ok": True,
                "writes_performed": False,
                "printer": {
                    "id": entry.id,
                    "adapter": entry.adapter,
                    "model": entry.model,
                    "base_url": entry.base_url,
                    "toolheads": entry.toolheads,
                    "slots": list(entry.slots or []),
                    "build_plate": entry.build_plate or None,
                    "build_plates": list(entry.build_plates or []),
                },
                "capabilities": _printer_capabilities(entry, adapter=adapter),
                "state": state.to_dict(),
                "build_plate": entry.build_plate or state.build_plate,
                "build_plate_source": (
                    "config" if entry.build_plate else ("firmware" if state.build_plate else None)
                ),
                "flow_calibration": calibration,
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="printer_save",
        description=(
            "Add or update a printer in config.json (upsert). slots is a list of "
            "{material, color, name?, toolhead?} entries in G-code tool order (T0 first); "
            "single-nozzle AMS machines use one slot per AMS slot. build_plates restricts "
            "the plate ids this printer accepts; plate_keys lists JSON fields to auto-detect "
            "the plate; flow_calibration configures vendor calibration G-code / factors URL. "
            "WRITES the config: "
            "confirm must be exactly 'SAVE PRINTER <printer_id>'. Adapters: moonraker, "
            "octoprint, bambu_lan, prusalink, http."
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
        username: str = "",
        password_env: str = "",
        storage: str = "local",
        http: dict[str, Any] | None = None,
        slots: list[dict[str, Any]] | None = None,
        build_plate: str = "",
        build_plates: list[str] | None = None,
        plate_keys: list[str] | None = None,
        flow_calibration: dict[str, Any] | None = None,
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
            if chosen == "http":
                validate_http_config(http or {})
            slot_table = normalise_slots(slots, toolheads=int(toolheads))
            plate = normalise_build_plate(build_plate)
            existing = next(
                (item for item in rt.loaded.config.printers if item.id == printer_id),
                None,
            )
            if not build_plates and existing is not None:
                build_plates = list(existing.build_plates or [])
            if not plate_keys and existing is not None:
                plate_keys = list(existing.plate_keys or [])
            if flow_calibration is None and existing is not None:
                flow_calibration = dict(existing.flow_calibration or {})
            supported_plates = [normalise_build_plate(str(item)) for item in (build_plates or [])]
            plate_key_list = [str(item).strip() for item in (plate_keys or []) if str(item).strip()]
            calibration = normalise_flow_calibration(flow_calibration)
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
                username=username.strip() or None,
                password_env=password_env.strip() or None,
                storage=storage.strip() or "local",
                http=http or {},
                slots=slot_table,
                build_plate=plate,
                build_plates=supported_plates,
                plate_keys=plate_key_list,
                flow_calibration=calibration,
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
            "target. flow_calibrate=True asks a Moonraker printer to run its own flow/"
            "pressure-advance calibration at print start (SET_PRINT_PREFERENCES "
            "FLOW_CALIBRATE=1). confirm must be exactly 'PRINT <printer_id> <slice_id>'. "
            "Runs as an async job (poll job_status)."
        ),
    )
    def send(
        slice_id: str,
        printer_id: str,
        confirm: str,
        start: bool = True,
        flow_calibrate: bool = False,
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
            material = str((record.get("options") or {}).get("material") or "").upper()
            slots = list(entry.slots or [])
            slot_checks: list[dict[str, Any]] = []
            material_warnings: list[str] = []
            if slots:
                if used and max(used) >= len(slots):
                    raise InvalidArgument(
                        f"G-code uses tool T{max(used)} but printer {printer_id} has "
                        f"{len(slots)} configured filament slot(s)",
                        details={"toolheads_used": used, "slots": len(slots)},
                        hint="Add the slot with printer_save(slots=[...]).",
                    )
                for tool in used:
                    meta = slots[tool]
                    slot_material = str(meta.get("material") or "").upper()
                    if slot_material and material and slot_material != material:
                        raise InvalidArgument(
                            f"G-code asks for {material} on T{tool}, but printer "
                            f"{printer_id} slot {tool} holds {slot_material}",
                            details={
                                "toolhead": tool,
                                "slot_material": slot_material,
                                "gcode_material": material,
                                "color": meta.get("color"),
                            },
                            hint=(
                                "Load the requested material, or re-slice with "
                                f"printer={printer_id!r}, slot=<the slot holding {material}>."
                            ),
                        )
                    slot_checks.append(
                        {
                            "toolhead": tool,
                            "material": slot_material or None,
                            "color": meta.get("color"),
                            "matches": not (slot_material and material)
                            or slot_material == material,
                        }
                    )
            else:
                if used and max(used) >= entry.toolheads:
                    raise InvalidArgument(
                        f"G-code uses tool T{max(used)} but printer {printer_id} declares "
                        f"{entry.toolheads} toolhead(s)",
                        details={"toolheads_used": used, "configured": entry.toolheads},
                        hint=(
                            "Set the toolhead count with printer_save(toolheads=...), or "
                            "declare material slots with printer_save(slots=[...])."
                        ),
                    )
                material_warnings.append(
                    "printer has no filament-slot metadata; material/colour cannot be verified"
                )
            adapter = build_printer(entry, secrets=rt.secrets)
            state = adapter.status()
            if state.busy:
                raise InvalidArgument(
                    f"Printer {printer_id} is busy ({state.state})",
                    hint="Wait for it to finish, or call printer_cancel first.",
                )
            if flow_calibrate:
                configured = bool((entry.flow_calibration or {}).get("enable_gcode")) and hasattr(
                    adapter, "set_flow_calibrate"
                )
                if not configured:
                    raise InvalidArgument(
                        f"printer {printer_id} has no flow-calibration command configured",
                        hint=(
                            "Set printers[].flow_calibration.enable_gcode to your firmware "
                            "macro (for Snapmaker U1: "
                            "'SET_PRINT_PREFERENCES FLOW_CALIBRATE=1'), then printer_save."
                        ),
                    )
            remote_name = f"{slice_id}.gcode"
            runner = rt.runner
            if runner is None:  # pragma: no cover - Runtime always builds one
                raise ConfigError("No job runner is available")

            def work(job_id: str) -> dict[str, Any]:
                if flow_calibrate:
                    rt.store.set_progress(job_id, 0.1, message="enabling flow calibration")
                    adapter.set_flow_calibrate(True)
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
                    "flow_calibrate": bool(flow_calibrate),
                    "printer_state_before": state.to_dict(),
                    "slot_checks": slot_checks,
                    "material_warnings": material_warnings,
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
                    "flow_calibrate": bool(flow_calibrate),
                    "slot_checks": slot_checks,
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
                "flow_calibrate": bool(flow_calibrate),
                "printer_state": state.to_dict(),
                "slot_checks": slot_checks,
                "material_warnings": material_warnings,
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
            "(for example ~/Desktop/bracket.stl). artifact is 'stl', 'step', "
            "'script', 'svg' or 'dxf' (the last two come from model_drawing). "
            "Defaults to the workspace exports/ directory. WRITES the destination "
            "file: confirm must be exactly 'EXPORT <asset_id>'."
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
            elif artifact in {"svg", "dxf"}:
                stored = f"drawing.{artifact}"
                if stored not in ((record.get("drawing") or {}).get("files") or {}):
                    raise NotFound(
                        f"Revision {target} has no {stored}",
                        hint=f"Call model_drawing with asset_id {asset_id!r} first.",
                    )
                source = directory / stored
                suffix = f".{artifact}"
            else:
                raise InvalidArgument(
                    "artifact must be 'stl', 'step', 'script', 'svg' or 'dxf'",
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
                "hint": (
                    "Open the sheet in a browser (svg) or a CAD/drawing tool (dxf)."
                    if artifact in {"svg", "dxf"}
                    else "Open the file in a slicer, or import it into the printer's web UI."
                ),
            }
        except XPrintError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_show",
        description=(
            "Return one asset's manifest, its revision history (prompt, model, "
            "geometry summary, STEP input, drawing files, viewer availability) and "
            "the source of a stored "
            "revision (latest by default), so a client model can read, explain, "
            "compare or repair it. Read-only."
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
            viewer_path = viewer_alias_path(
                assets.asset_dir(asset_id),
                name=str(manifest.get("name") or ""),
                asset_id=asset_id,
            )
            viewer_rel = rt.workspace.rel(viewer_path) if viewer_path.is_file() else None
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "name": manifest.get("name"),
                "revision": target,
                "viewer": viewer_rel,
                "revisions": [
                    {
                        "revision": item.get("revision"),
                        "created_at": item.get("created_at"),
                        "checks": item.get("checks", {}),
                        "build_ok": (item.get("build") or {}).get("ok"),
                        "built": bool((item.get("build") or {}).get("ok")),
                        "bbox_mm": ((item.get("build") or {}).get("checks") or {}).get("bbox_mm"),
                        "volume_mm3": ((item.get("build") or {}).get("checks") or {}).get(
                            "volume_mm3"
                        ),
                        "volume_cm3": ((item.get("build") or {}).get("checks") or {}).get(
                            "volume_cm3"
                        ),
                        "artifacts": sorted(item.get("artifacts", {})),
                        "drawing": sorted((item.get("drawing") or {}).get("files") or {}),
                        "input": (item.get("build") or {}).get("input"),
                        "reference": (item.get("reference") or {}).get("file"),
                        "prompt": item.get("prompt"),
                        "model": item.get("model"),
                        "modeler": item.get("modeler"),
                        "viewer": viewer_rel,
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
