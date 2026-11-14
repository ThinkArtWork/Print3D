"""Tests for the slice MCP tools and the async slice job."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session
from tests.stl_helpers import write_stl
from tests.test_slicers import _fake_slicer, _vendor

from xprint.assets import AssetStore
from xprint.server import Runtime, build_server

SCRIPT = """\
from build123d import *

def build_part():
    return Box(20, 20, 20)

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


def _configure(workspace, tmp_path: Path, **overrides) -> dict[str, str]:
    machine, process, filament, _ = _vendor(tmp_path)
    target = {
        "printer_model": "Fake A1",
        "slicer": "orca",
        "slicer_path": str(_fake_slicer(tmp_path)),
        "machine": str(machine),
        "process": str(process),
        "filament": [str(filament)],
        "timeout_s": 30,
    }
    target.update(overrides)
    workspace.config_path.write_text(json.dumps({"targets": {"a1": target}}), encoding="utf-8")
    return target


def _call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = asyncio.run(scenario())
    return json.loads(result.content[0].text)


def _built_asset(runtime, tmp_path: Path) -> tuple[str, str]:
    written = _call(
        runtime, "model_write", {"script": SCRIPT, "name": "cube", "confirm": "WRITE cube"}
    )
    stl = tmp_path / "model.stl"
    write_stl(stl)
    AssetStore(runtime.workspace).record_build(
        written["asset_id"],
        written["revision"],
        build={"ok": True, "checks": {"watertight": True}},
        artifacts={"model.stl": stl},
    )
    return written["asset_id"], written["revision"]


def _wait(runtime, job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = _call(runtime, "job_status", {"job_id": job_id})
        state = payload["job"]["state"]
        if state in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_slice_capabilities_reports_the_target(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    payload = _call(runtime, "slice_capabilities", {})
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert payload["ready"] == 1
    target = payload["targets"][0]
    assert target["name"] == "a1"
    assert target["printer_model"] == "Fake A1"
    assert target["bed_mm"] == [256.0, 256.0]
    assert target["problems"] == []
    assert target["profiles"]["machine"].endswith("Fake A1.json")


def test_slice_plan_is_read_only_and_returns_the_confirm_string(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, revision = _built_asset(runtime, tmp_path)
    payload = _call(runtime, "slice_plan", {"asset_id": asset_id, "target": "a1"})
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert payload["revision"] == revision
    assert payload["confirm"] == f"SLICE {asset_id} a1"
    assert payload["command"][0].endswith("fake-orca")
    assert "--load-settings" in payload["command"]
    assert payload["bed_mm"] == [256.0, 256.0]
    assert payload["checks"]["triangles"] == 4
    assert not (workspace.slices_dir / "plan" / "output").exists()


def test_slice_plan_requires_a_built_model(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    written = _call(
        runtime, "model_write", {"script": SCRIPT, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = _call(runtime, "slice_plan", {"asset_id": written["asset_id"], "target": "a1"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "model_build" in payload["error"]["hint"]


def test_slice_run_requires_exact_confirm(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, _ = _built_asset(runtime, tmp_path)
    payload = _call(
        runtime, "slice_run", {"asset_id": asset_id, "target": "a1", "confirm": "slice it"}
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert f"SLICE {asset_id} a1" in payload["error"]["message"]


def test_slice_run_slices_and_records(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, revision = _built_asset(runtime, tmp_path)
    try:
        started = _call(
            runtime,
            "slice_run",
            {"asset_id": asset_id, "target": "a1", "confirm": f"SLICE {asset_id} a1"},
        )
        assert started["ok"] is True
        assert started["writes_performed"] is True
        assert started["slice_id"].startswith("s_")
        finished = _wait(runtime, started["job_id"])
        assert finished["job"]["state"] == "succeeded", finished
        assert finished["job"]["result"]["slice_id"] == started["slice_id"]
        inspected = _call(runtime, "slice_inspect", {"slice_id": started["slice_id"]})
        assert inspected["ok"] is True
        record = inspected["slice"]
        assert record["ok"] is True
        assert record["asset_id"] == asset_id
        assert record["revision"] == revision
        assert record["metadata"]["total_layers"] == 50
        assert record["checks"]["layers"] == 50
        assert record["checks"]["bed_mm"] == [256.0, 256.0]
        assert record["gcode"]["bytes"] > 0
        assert inspected["gcode"]["present"] is True
        assert Path(inspected["gcode"]["path"]).is_file()
    finally:
        if runtime.runner is not None:
            runtime.runner.shutdown(wait=False)


def test_slice_run_records_a_failure(workspace, tmp_path, monkeypatch) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, _ = _built_asset(runtime, tmp_path)
    monkeypatch.setenv("FAKE_SLICER_FAIL", "1")
    try:
        started = _call(
            runtime,
            "slice_run",
            {"asset_id": asset_id, "target": "a1", "confirm": f"SLICE {asset_id} a1"},
        )
        finished = _wait(runtime, started["job_id"])
        assert finished["job"]["state"] == "failed"
        assert finished["job"]["error"]["code"] == "adapter_unavailable"
        inspected = _call(runtime, "slice_inspect", {"slice_id": started["slice_id"]})
        assert inspected["slice"]["ok"] is False
        assert inspected["slice"]["exit_code"] == 7
    finally:
        if runtime.runner is not None:
            runtime.runner.shutdown(wait=False)


def test_slice_plan_previews_material_and_process_options(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, revision = _built_asset(runtime, tmp_path)
    payload = _call(
        runtime,
        "slice_plan",
        {
            "asset_id": asset_id,
            "target": "a1",
            "material": "petg",
            "layer_height": 0.24,
            "infill": "gyroid",
            "infill_density": 30,
            "strength": "strong",
        },
    )
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert payload["revision"] == revision
    assert payload["options"]["material"] == "PETG"
    assert payload["options"]["nozzle_temp"] == 240
    assert payload["options"]["bed_temp"] == 80
    assert payload["options"]["walls"] == 4
    assert payload["profile_overrides"]["process"]["layer_height"] == "0.24"
    assert payload["profile_overrides"]["filament"]["temperature"] == "240"
    assert any("private copy" in item for item in payload["warnings"])
    assert not (workspace.slices_dir / "plan" / "output").exists()


def test_slice_plan_rejects_an_unknown_material(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, _ = _built_asset(runtime, tmp_path)
    payload = _call(
        runtime,
        "slice_plan",
        {"asset_id": asset_id, "target": "a1", "material": "unobtainium"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "material" in payload["error"]["message"].lower()


def test_slice_run_records_the_requested_options(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, revision = _built_asset(runtime, tmp_path)
    payload = _call(
        runtime,
        "slice_run",
        {
            "asset_id": asset_id,
            "target": "a1",
            "revision": revision,
            "confirm": f"SLICE {asset_id} a1",
            "material": "PETG",
            "layer_height": 0.24,
            "infill": "gyroid",
            "infill_density": 30,
            "strength": "strong",
        },
    )
    assert payload["ok"] is True
    job = _wait(runtime, payload["job_id"])
    assert job["job"]["state"] == "succeeded", job

    inspected = _call(runtime, "slice_inspect", {"slice_id": payload["slice_id"]})
    record = inspected["slice"]
    assert record["options"]["material"] == "PETG"
    assert record["options"]["layer_height"] == 0.24
    assert record["profiles"]["options"]["bed_temp"] == 80
    assert record["profiles"]["filament_overrides"]["temperature"] == "240"


def test_slice_plan_pins_a_printer_slot_and_checks_its_material(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, _ = _built_asset(runtime, tmp_path)
    saved = _call(
        runtime,
        "printer_save",
        {
            "printer_id": "u1",
            "confirm": "SAVE PRINTER u1",
            "base_url": "http://printer.local:7125",
            "toolheads": 4,
            "slots": [{"material": "PLA"}, {"material": "PETG"}],
            "build_plate": "pei",
        },
    )
    assert saved["ok"] is True

    plan = _call(
        runtime,
        "slice_plan",
        {"asset_id": asset_id, "target": "a1", "printer": "u1", "slot": 1},
    )
    assert plan["ok"] is True
    assert plan["options"]["material"] == "PETG"
    assert plan["options"]["toolhead"] == 1
    assert plan["options"]["build_plate"] == "textured_pei"
    assert plan["options"]["bed_temp"] == 80
    assert plan["slot"] == {
        "slot": 1,
        "printer": "u1",
        "material": "PETG",
        "color": None,
        "name": None,
        "configured": True,
        "build_plate": "textured_pei",
    }

    mismatch = _call(
        runtime,
        "slice_plan",
        {"asset_id": asset_id, "target": "a1", "printer": "u1", "slot": 1, "material": "PLA"},
    )
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "invalid_argument"
    assert "holds PETG" in mismatch["error"]["message"]


def test_slice_plan_build_plate_selects_the_orca_bed_key(workspace, tmp_path) -> None:
    _configure(workspace, tmp_path)
    runtime = Runtime.create(workspace=workspace)
    asset_id, _ = _built_asset(runtime, tmp_path)

    plan = _call(
        runtime,
        "slice_plan",
        {
            "asset_id": asset_id,
            "target": "a1",
            "material": "PLA",
            "build_plate": "cool",
        },
    )
    assert plan["ok"] is True
    assert plan["options"]["build_plate"] == "cool_plate"
    assert plan["options"]["bed_temp"] == 45
    assert plan["profile_overrides"]["process"]["curr_bed_type"] == "Cool Plate"
    compact = {
        key: value
        for key, value in plan.items()
        if key in {"asset_id", "target", "options", "profile_overrides", "slot"}
    }
    assert compact["slot"]["build_plate"] == "cool_plate"

    unknown = _call(
        runtime,
        "slice_plan",
        {"asset_id": asset_id, "target": "a1", "build_plate": "cardboard"},
    )
    assert unknown["ok"] is False
    assert "Unknown build plate" in unknown["error"]["message"]
