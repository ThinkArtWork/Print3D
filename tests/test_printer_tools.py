"""Printer CRUD, status, send and cancel MCP tools."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from xprint.errors import InvalidArgument
from xprint.printers import PrinterState
from xprint.server import Runtime, _apply_slot, build_server
from xprint.slicers import SliceOptions, SliceStore, new_slice_id
from xprint.workspace import Workspace


def _call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = asyncio.run(scenario())
    return json.loads(result.content[0].text)


def _runtime(tmp_path: Path, printers: list[dict] | None = None) -> Runtime:
    ws = Workspace(tmp_path / "ws")
    payload = {"printers": printers or []}
    ws.config_path.write_text(json.dumps(payload), encoding="utf-8")
    return Runtime.create(workspace=ws)


def _fake_slice(
    workspace: Workspace,
    *,
    printer_model: str = "Fake A1",
    ok: bool = True,
    toolheads: list[int] | None = None,
    material: str = "",
) -> str:
    store = SliceStore(workspace)
    slice_id = new_slice_id()
    directory = store.directory(slice_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plate_1.gcode").write_text("; gcode\nG28\n", encoding="utf-8")
    record = {
        "slice_id": slice_id,
        "ok": ok,
        "printer_model": printer_model,
        "gcode": {"file": "plate_1.gcode", "bytes": 12, "sha256": "deadbeef"},
        "metadata": {"total_layers": 50, "toolheads": toolheads or []},
        "statistics": {},
        "options": {"material": material} if material else {},
    }
    (directory / "slice.json").write_text(json.dumps(record), encoding="utf-8")
    return slice_id


class FakePrinter:
    adapter = "moonraker"

    def __init__(self, state: str = "standby", *, fail_upload: bool = False) -> None:
        self.state = state
        self.fail_upload = fail_upload
        self.uploaded: list[tuple[str, bytes]] = []
        self.started: list[str] = []
        self.cancelled = 0
        self.flow_calibrate: list[bool] = []

    def status(self) -> PrinterState:
        return PrinterState(online=True, state=self.state, bed_temp=20.0)

    def upload(self, path: Path, *, name: str) -> str:
        if self.fail_upload:
            raise RuntimeError("upload exploded")
        self.uploaded.append((name, path.read_bytes()))
        return name

    def set_flow_calibrate(self, enabled: bool) -> None:
        self.flow_calibrate.append(bool(enabled))

    def start(self, filename: str) -> None:
        self.started.append(filename)

    def cancel(self) -> None:
        self.cancelled += 1


def _wait(runtime, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = _call(runtime, "job_status", {"job_id": job_id})
        states = payload.get("job", {}).get("state")
        if states in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_printer_list_reports_configuration(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Snapmaker U1",
                "toolheads": 4,
            }
        ],
    )
    payload = _call(runtime, "printer_list", {})
    assert payload["ok"] is True
    assert payload["count"] == 1
    entry = payload["printers"][0]
    assert entry["id"] == "u1"
    assert entry["adapter_known"] is True
    assert entry["has_key"] is False
    assert entry["toolheads"] == 4


def test_printer_save_writes_config_then_updates(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    saved = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "adapter": "moonraker",
            "base_url": "http://printer.local:7125",
            "model": "Snapmaker U1",
            "toolheads": 4,
        },
    )
    assert saved["ok"] is True
    assert saved["writes_performed"] is True
    on_disk = json.loads(runtime.workspace.config_path.read_text(encoding="utf-8"))
    assert on_disk["printers"][0]["id"] == "u1"
    assert on_disk["printers"][0]["toolheads"] == 4

    updated = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "adapter": "moonraker",
            "base_url": "http://printer.local:7125",
            "model": "Snapmaker U1",
            "toolheads": 1,
        },
    )
    assert updated["ok"] is True
    listing = _call(runtime, "printer_list", {})
    assert listing["count"] == 1
    assert listing["printers"][0]["toolheads"] == 1


def test_printer_save_rejects_bad_confirm_and_adapter(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    bad = _call(
        runtime,
        "printer_save",
        {"printer_id": "u1", "confirm": "save printer u1", "base_url": "http://x"},
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "confirmation_mismatch"

    unknown = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "adapter": "bambu",
            "base_url": "http://x",
        },
    )
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "invalid_argument"


def test_printer_remove(tmp_path) -> None:
    runtime = _runtime(
        tmp_path, [{"id": "u1", "adapter": "moonraker", "base_url": "http://printer.local:7125"}]
    )
    removed = _call(runtime, "printer_remove", {"printer_id": "u1", "confirm": "REMOVE PRINTER u1"})
    assert removed["ok"] is True
    assert _call(runtime, "printer_list", {})["count"] == 0
    missing = _call(runtime, "printer_remove", {"printer_id": "u1", "confirm": "REMOVE PRINTER u1"})
    assert missing["ok"] is False


def test_printer_status_uses_adapter(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path, [{"id": "u1", "adapter": "moonraker", "base_url": "http://printer.local:7125"}]
    )
    monkeypatch.setattr(
        "xprint.server.build_printer", lambda *a, **k: FakePrinter(state="operational")
    )
    payload = _call(runtime, "printer_status", {"printer_id": "u1"})
    assert payload["ok"] is True
    assert payload["state"]["state"] == "operational"
    assert payload["state"]["busy"] is False


def test_send_uploads_and_starts(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Fake A1",
            }
        ],
    )
    fake = FakePrinter()
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace)

    sent = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert sent["ok"] is True
    assert sent["writes_performed"] is True
    finished = _wait(runtime, sent["job_id"])
    assert finished["job"]["state"] == "succeeded"
    assert fake.uploaded and fake.uploaded[0][0] == f"{slice_id}.gcode"
    assert fake.uploaded[0][1] == b"; gcode\nG28\n"
    assert fake.started == [f"{slice_id}.gcode"]


def test_send_refuses_busy_printer(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Fake A1",
            }
        ],
    )
    fake = FakePrinter(state="printing")
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace)
    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "busy" in payload["error"]["message"]
    assert fake.uploaded == []


def test_send_refuses_target_mismatch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Snapmaker U1",
            }
        ],
    )
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: FakePrinter())
    slice_id = _fake_slice(runtime.workspace, printer_model="Bambu Lab A1")
    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "Bambu Lab A1" in payload["error"]["message"]


def test_send_requires_exact_confirm(tmp_path) -> None:
    runtime = _runtime(tmp_path, [{"id": "u1", "adapter": "moonraker", "base_url": "http://x"}])
    slice_id = _fake_slice(runtime.workspace)
    payload = _call(runtime, "send", {"slice_id": slice_id, "printer_id": "u1", "confirm": "print"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


def test_send_requires_successful_slice(tmp_path) -> None:
    runtime = _runtime(tmp_path, [{"id": "u1", "adapter": "moonraker", "base_url": "http://x"}])
    slice_id = _fake_slice(runtime.workspace, ok=False)
    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_printer_cancel(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path, [{"id": "u1", "adapter": "moonraker", "base_url": "http://printer.local:7125"}]
    )
    fake = FakePrinter(state="printing")
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    payload = _call(runtime, "printer_cancel", {"printer_id": "u1", "confirm": "CANCEL PRINT u1"})
    assert payload["ok"] is True
    assert fake.cancelled == 1
    bad = _call(runtime, "printer_cancel", {"printer_id": "u1", "confirm": "cancel"})
    assert bad["ok"] is False
    assert bad["error"]["code"] == "confirmation_mismatch"


def test_printer_status_unknown_printer(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    payload = _call(runtime, "printer_status", {"printer_id": "nope"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "printer_save" in (payload["error"].get("hint") or "")


def test_printer_save_accepts_nine_toolheads(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    payload = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "x3d",
            "confirm": "SAVE PRINTER x3d",
            "adapter": "moonraker",
            "base_url": "http://printer.local:7125",
            "model": "Bambu X3D",
            "toolheads": 9,
        },
    )
    assert payload["ok"] is True
    assert payload["printer"]["toolheads"] == 9
    rejected = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "too-many",
            "confirm": "SAVE PRINTER too-many",
            "adapter": "moonraker",
            "base_url": "http://printer.local:7125",
            "toolheads": 10,
        },
    )
    assert rejected["ok"] is False
    assert "between 1 and 9" in rejected["error"]["message"]


def test_send_refuses_more_toolheads_than_configured(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Fake A1",
                "toolheads": 4,
            }
        ],
    )
    fake = FakePrinter()
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace, toolheads=[0, 4])
    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "T4" in payload["error"]["message"]
    assert "toolheads=..." in payload["error"]["hint"]
    assert fake.uploaded == []


def test_printer_save_bambu_lan_requires_serial(tmp_path):
    runtime = _runtime(tmp_path)
    result = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "x1",
            "confirm": "SAVE PRINTER x1",
            "adapter": "bambu_lan",
            "base_url": "http://192.168.1.50",
            "model": "Bambu Lab X1C",
        },
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert "serial" in result["error"]["message"]


def test_printer_save_http_requires_config(tmp_path):
    runtime = _runtime(tmp_path)
    bad = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "vendorp",
            "confirm": "SAVE PRINTER vendorp",
            "adapter": "http",
            "base_url": "http://printer.local",
        },
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "invalid_argument"
    good = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "vendorp",
            "confirm": "SAVE PRINTER vendorp",
            "adapter": "http",
            "base_url": "http://printer.local",
            "http": {
                "status": {"path": "/state", "map": {"state": "state"}},
                "upload": {"path": "/files/{filename}"},
                "start": {"path": "/files/{filename}/start"},
                "cancel": {"path": "/job/stop"},
            },
        },
    )
    assert good["ok"] is True
    assert good["printer"]["adapter"] == "http"


def test_printer_save_stores_material_slots(tmp_path) -> None:
    runtime = _runtime(tmp_path, [])
    payload = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "ams",
            "confirm": "SAVE PRINTER ams",
            "base_url": "http://printer.local:7125",
            "toolheads": 1,
            "slots": [
                {"material": "pla", "color": "#ffffff", "name": "White PLA"},
                {"material": "PETG", "color": "#101010"},
            ],
        },
    )
    assert payload["ok"] is True
    assert payload["printer"]["slots"][0]["material"] == "PLA"
    assert payload["printer"]["slots"][0]["color"] == "#FFFFFF"
    listed = _call(runtime, "printer_list", {})
    assert listed["printers"][0]["slots"][1]["material"] == "PETG"


def test_send_refuses_a_slot_with_the_wrong_material(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Fake A1",
                "toolheads": 2,
                "slots": [{"material": "PLA"}, {"material": "PETG"}],
            }
        ],
    )
    fake = FakePrinter()
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace, toolheads=[1], material="PLA")

    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "u1", "confirm": f"PRINT u1 {slice_id}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "holds PETG" in payload["error"]["message"]
    assert fake.uploaded == []


def test_send_accepts_an_ams_slot_with_the_right_material(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "ams",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "model": "Fake A1",
                "toolheads": 1,
                "slots": [
                    {"material": "PLA", "color": "#FFFFFF"},
                    {"material": "PETG", "color": "#000000"},
                    {"material": "PLA", "color": "#FF0000"},
                ],
            }
        ],
    )
    fake = FakePrinter()
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace, toolheads=[2], material="PLA")

    payload = _call(
        runtime,
        "send",
        {"slice_id": slice_id, "printer_id": "ams", "confirm": f"PRINT ams {slice_id}"},
    )
    assert payload["ok"] is True
    finished = _wait(runtime, payload["job_id"])
    assert finished["job"]["state"] == "succeeded"
    checks = finished["job"]["result"]["slot_checks"]
    assert checks == [{"toolhead": 2, "material": "PLA", "color": "#FF0000", "matches": True}]


def test_printer_save_normalises_the_build_plate(workspace, tmp_path) -> None:
    runtime = _runtime(tmp_path, [])
    saved = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "base_url": "http://printer.local:7125",
            "model": "Snapmaker U1",
            "build_plate": "pei",
        },
    )
    assert saved["ok"] is True
    assert saved["printer"]["build_plate"] == "textured_pei"

    listed = _call(runtime, "printer_list", {})
    assert listed["printers"][0]["build_plate"] == "textured_pei"

    unknown = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "base_url": "http://printer.local:7125",
            "build_plate": "cardboard",
        },
    )
    assert unknown["ok"] is False
    assert "Unknown build plate" in unknown["error"]["message"]


def test_send_enables_printer_flow_calibration(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "u1",
                "adapter": "moonraker",
                "base_url": "http://printer.local:7125",
                "flow_calibration": {"enable_gcode": "SET_PRINT_PREFERENCES FLOW_CALIBRATE=1"},
            }
        ],
    )
    fake = FakePrinter()
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    slice_id = _fake_slice(runtime.workspace)
    sent = _call(
        runtime,
        "send",
        {
            "slice_id": slice_id,
            "printer_id": "u1",
            "confirm": f"PRINT u1 {slice_id}",
            "flow_calibrate": True,
        },
    )
    assert sent["ok"] is True
    finished = _wait(runtime, sent["job_id"])
    assert finished["job"]["state"] == "succeeded"
    assert fake.flow_calibrate == [True]
    assert fake.started == [f"{slice_id}.gcode"]


def test_printer_save_stores_vendor_capabilities(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    saved = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "voron",
            "confirm": "SAVE PRINTER voron",
            "adapter": "moonraker",
            "base_url": "http://voron.local:7125",
            "build_plates": ["textured_pei", "cool_plate"],
            "plate_keys": ["bed_type"],
            "flow_calibration": {
                "enable_gcode": "FLOW_CALIBRATE ENABLE=1",
                "disable_gcode": "FLOW_CALIBRATE ENABLE=0",
                "factors_url": "/server/files/config/calibration.json",
                "factors_key": "pa.factor",
                "extruder_keys": ["extruder", "extruder1"],
            },
        },
    )
    assert saved["ok"] is True
    entry = saved["printer"]
    assert entry["build_plates"] == ["textured_pei", "cool_plate"]
    assert entry["plate_keys"] == ["bed_type"]
    assert entry["flow_calibration"]["enable_gcode"] == "FLOW_CALIBRATE ENABLE=1"
    listed = _call(runtime, "printer_list", {})
    row = next(item for item in listed["printers"] if item["id"] == "voron")
    assert row["flow_calibration_configured"] is True
    assert row["build_plates"] == ["textured_pei", "cool_plate"]


def test_printer_save_rejects_unknown_calibration_keys(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    payload = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "voron",
            "confirm": "SAVE PRINTER voron",
            "adapter": "moonraker",
            "base_url": "http://voron.local:7125",
            "flow_calibration": {"enable_gcode": "X", "bogus": "y"},
        },
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "bogus" in payload["error"]["message"]


def test_slice_rejects_a_plate_the_printer_does_not_list(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "voron",
                "adapter": "moonraker",
                "base_url": "http://voron.local:7125",
                "build_plates": ["textured_pei"],
            }
        ],
    )
    with pytest.raises(InvalidArgument, match="does not list build plate"):
        _apply_slot(
            runtime,
            SliceOptions(),
            printer_id="voron",
            slot=-1,
            build_plate="cool_plate",
        )


def test_printer_status_reports_capabilities(tmp_path, monkeypatch) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "voron",
                "adapter": "moonraker",
                "base_url": "http://voron.local:7125",
                "build_plates": ["textured_pei", "cool_plate"],
                "plate_keys": ["bed_type"],
                "flow_calibration": {
                    "enable_gcode": "FLOW_CALIBRATE ENABLE=1",
                    "factors_url": "/x.json",
                },
            }
        ],
    )
    fake = FakePrinter()
    fake.flow_calibration = lambda: {"source": "/x.json", "factors": {"T0": 0.04}, "env": {}}
    monkeypatch.setattr("xprint.server.build_printer", lambda *a, **k: fake)
    payload = _call(runtime, "printer_status", {"printer_id": "voron"})
    assert payload["ok"] is True
    caps = payload["capabilities"]
    assert caps["build_plates"] == ["textured_pei", "cool_plate"]
    assert caps["build_plate_detection"] is True
    assert caps["flow_calibration"] == {
        "supported": True,
        "factors_configured": True,
        "trim_unused_tools": False,
        "tool_macros": [],
    }
    assert payload["flow_calibration"]["factors"] == {"T0": 0.04}


def test_printer_save_keeps_existing_capabilities_when_omitted(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {
                "id": "voron",
                "adapter": "moonraker",
                "base_url": "http://voron.local:7125",
                "build_plates": ["textured_pei"],
                "flow_calibration": {"enable_gcode": "FLOW_CALIBRATE ENABLE=1"},
            }
        ],
    )
    saved = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "voron",
            "confirm": "SAVE PRINTER voron",
            "adapter": "moonraker",
            "base_url": "http://voron.local:7125",
        },
    )
    assert saved["ok"] is True
    assert saved["printer"]["build_plates"] == ["textured_pei"]
    assert saved["printer"]["flow_calibration"]["enable_gcode"] == "FLOW_CALIBRATE ENABLE=1"
