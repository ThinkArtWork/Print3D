"""Shared helpers used by more than one tool module."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from ..assets import AssetStore
from ..config import (
    ImageProviderSettings,
)
from ..errors import (
    AdapterUnavailable,
    InvalidArgument,
    NotFound,
)
from ..runtime import Runtime
from ..slicers import (
    BUILD_PLATES,
    SliceOptions,
    normalise_build_plate,
    parse_machine_bed,
    resolve_profiles,
)
from ..viewer import (
    entry_from_stl,
    write_collection_viewer,
    write_legacy_redirect,
)

GCODE_CAPABLE_ADAPTERS = {"moonraker", "octoprint"}


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
