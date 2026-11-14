"""Printer adapter tests (Moonraker / OctoPrint) with mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from xprint.config import PrinterSettings, Secrets
from xprint.errors import AdapterUnavailable, AdapterUnknown, InvalidArgument
from xprint.printers import MoonrakerPrinter, OctoPrintPrinter, build_printer

MOON = "http://printer.local:7125"
OCTO = "http://octo.local"


def _secrets(data: dict | None = None) -> Secrets:
    return Secrets(data or {})


@respx.mock
def test_moonraker_status_maps_printing_state() -> None:
    route = respx.get(f"{MOON}/printer/objects/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "status": {
                        "print_stats": {"state": "printing", "filename": "part.gcode"},
                        "virtual_sdcard": {"progress": 0.42},
                        "heater_bed": {"temperature": 60.5},
                        "extruder": {"temperature": 215.0},
                    }
                }
            },
        )
    )
    printer = MoonrakerPrinter("u1", base_url=MOON)
    state = printer.status()
    assert route.called
    assert state.online is True
    assert state.state == "printing"
    assert state.busy is True
    assert state.filename == "part.gcode"
    assert state.progress == pytest.approx(0.42)
    assert state.bed_temp == 60.5
    assert state.nozzle_temp == 215.0


@respx.mock
def test_moonraker_upload_and_start(tmp_path) -> None:
    upload = respx.post(f"{MOON}/server/files/upload").mock(
        return_value=httpx.Response(201, json={"item": {"path": "slice-1.gcode"}})
    )
    start = respx.post(f"{MOON}/printer/print/start").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    gcode = tmp_path / "slice-1.gcode"
    gcode.write_text("; gcode\n", encoding="utf-8")
    printer = MoonrakerPrinter("u1", base_url=MOON, api_key="secret")
    uploaded = printer.upload(gcode, name="slice-1.gcode")
    assert uploaded == "slice-1.gcode"
    assert b'name="root"' in upload.calls[0].request.content
    assert b"gcodes" in upload.calls[0].request.content
    assert b"slice-1.gcode" in upload.calls[0].request.content
    assert upload.calls[0].request.headers["X-Api-Key"] == "secret"
    printer.start(uploaded)
    assert start.called
    assert start.calls[0].request.url.params["filename"] == "slice-1.gcode"


@respx.mock
def test_moonraker_rejects_bad_key() -> None:
    respx.get(f"{MOON}/printer/objects/query").mock(return_value=httpx.Response(401))
    printer = MoonrakerPrinter("u1", base_url=MOON, api_key="wrong")
    with pytest.raises(AdapterUnavailable, match="rejected the API key"):
        printer.status()


@respx.mock
def test_moonraker_unreachable() -> None:
    respx.get(f"{MOON}/printer/objects/query").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    printer = MoonrakerPrinter("u1", base_url=MOON)
    with pytest.raises(AdapterUnavailable, match="not reachable"):
        printer.status()


@respx.mock
def test_octoprint_status_maps_flags() -> None:
    respx.get(f"{OCTO}/api/printer").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": {"text": "Operational", "flags": {"operational": True, "printing": True}},
                "temperature": {"tool0": {"actual": 210.0}, "bed": {"actual": 60.0}},
            },
        )
    )
    respx.get(f"{OCTO}/api/job").mock(
        return_value=httpx.Response(
            200,
            json={
                "progress": {"completion": 55.0},
                "job": {"file": {"name": "benchy.gcode"}},
            },
        )
    )
    printer = OctoPrintPrinter("octo", base_url=OCTO, api_key="k")
    state = printer.status()
    assert state.state == "printing"
    assert state.progress == pytest.approx(0.55)
    assert state.filename == "benchy.gcode"
    assert state.bed_temp == 60.0


@respx.mock
def test_octoprint_upload_and_start(tmp_path) -> None:
    respx.post(f"{OCTO}/api/files/local").mock(
        return_value=httpx.Response(
            201, json={"files": {"local": {"name": "s.gcode", "path": "s.gcode"}}}
        )
    )
    start = respx.post(f"{OCTO}/api/files/local/s.gcode").mock(return_value=httpx.Response(204))
    gcode = tmp_path / "s.gcode"
    gcode.write_text("; g\n", encoding="utf-8")
    printer = OctoPrintPrinter("octo", base_url=OCTO)
    printer.upload(gcode, name="s.gcode")
    printer.start("s.gcode")
    assert start.called
    assert start.calls[0].request.content


def test_build_printer_dispatches_and_requires_config() -> None:
    settings = PrinterSettings(id="u1", adapter="moonraker", base_url=MOON)
    printer = build_printer(settings, secrets=_secrets())
    assert isinstance(printer, MoonrakerPrinter)

    with pytest.raises(AdapterUnknown):
        build_printer(
            PrinterSettings(id="x", adapter="bambu", base_url="http://x"), secrets=_secrets()
        )
    with pytest.raises(AdapterUnavailable):
        build_printer(
            PrinterSettings(id="x", adapter="moonraker", base_url=MOON, enabled=False),
            secrets=_secrets(),
        )
    with pytest.raises(InvalidArgument):
        build_printer(PrinterSettings(id="x", adapter="moonraker"), secrets=_secrets())


def test_build_printer_uses_configured_key() -> None:
    settings = PrinterSettings(id="u1", adapter="moonraker", base_url=MOON, api_key_env="U1_KEY")
    printer = build_printer(
        settings,
        secrets=_secrets({"providers": {"u1": "from-secrets"}}),
    )
    assert isinstance(printer, MoonrakerPrinter)
    assert printer.api_key == "from-secrets"


@respx.mock
def test_moonraker_reads_flow_calibration_factors() -> None:
    route = respx.get(f"{MOON}/server/files/config/snapmaker/flow_calibrator.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "factor": {"extruder": 0.055761, "extruder1": 0.02},
                "env": {"k_min": 0.005, "k_max": 0.065},
            },
        )
    )
    printer = MoonrakerPrinter("u1", base_url=MOON)
    data = printer.flow_calibration()
    assert route.called
    assert data["source"] == "snapmaker/flow_calibrator.json"
    assert data["factors"]["extruder"] == pytest.approx(0.055761)
    assert data["env"]["k_max"] == pytest.approx(0.065)


@respx.mock
def test_moonraker_sets_flow_calibration_preference() -> None:
    route = respx.post(f"{MOON}/printer/gcode/script").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    printer = MoonrakerPrinter("u1", base_url=MOON)
    printer.set_flow_calibrate(True)
    assert route.called
    assert route.calls[0].request.url.params["script"] == "SET_PRINT_PREFERENCES FLOW_CALIBRATE=1"
