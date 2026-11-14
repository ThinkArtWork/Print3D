"""MCP tools: printers."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import (
    PrinterSettings,
    remove_printer,
    save_printer,
)
from ..errors import (
    ConfigError,
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    Print3DError,
    error_payload,
)
from ..jobs import sha256_file
from ..printers import (
    PRINTER_ADAPTERS,
    build_printer,
    normalise_flow_calibration,
    normalise_slots,
    validate_http_config,
    validate_printer_id,
)
from ..runtime import Runtime, tool_error
from ..slicers import (
    SliceStore,
    normalise_build_plate,
    validate_slice_id,
)
from .common import (
    _printer_capabilities,
    _printer_entry,
)


def register(server: FastMCP, rt: Runtime) -> None:
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
        except Print3DError as exc:
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
                except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
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
        except Print3DError as exc:
            return tool_error(exc)
