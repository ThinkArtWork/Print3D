"""MCP tools: slicing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..assets import AssetStore, validate_asset_id
from ..errors import (
    AdapterUnavailable,
    ConfirmationMismatch,
    NotFound,
    Print3DError,
)
from ..mesh import stats_for_stl
from ..runner import JobRunner
from ..runtime import Runtime, tool_error
from ..slicers import (
    BUILD_PLATES,
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
from .common import (
    _apply_slot,
    _printer_entry,
    _slice_options,
)


def register(server: FastMCP, rt: Runtime) -> None:
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
                except Print3DError as exc:
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
                except Print3DError as exc:
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
        except Print3DError as exc:
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
            except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
            return tool_error(exc)
