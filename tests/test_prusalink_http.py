"""PrusaLink (Digest auth) and declarative HTTP printer adapters."""

from __future__ import annotations

import httpx
import pytest

from print3d.config import PrinterSettings, Secrets
from print3d.errors import AdapterUnavailable, InvalidArgument
from print3d.printers import HttpPrinter, PrusaLinkPrinter, build_printer


def _digest_handler(payload: dict, *, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if "authorization" not in {key.lower() for key in request.headers}:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Digest realm="PrusaLink", nonce="abcdef0123456789", qop="auth"'
                    )
                },
                json={"error": "unauthorized"},
            )
        if request.url.path == "/api/v1/status":
            return httpx.Response(200, json=payload)
        return httpx.Response(204)

    return handler


def _prusalink(payload: dict, **kwargs):
    seen: list[httpx.Request] = []
    printer = PrusaLinkPrinter(
        "mk4",
        base_url="http://prusa.local",
        username="maker",
        password="secret",
        transport=httpx.MockTransport(_digest_handler(payload, seen=seen)),
        **kwargs,
    )
    return printer, seen


def test_prusalink_status_maps_v1_payload():
    payload = {
        "printer": {
            "state": "PRINTING",
            "temperature": {
                "bed": {"actual": 60.0, "target": 60.0},
                "tool0": {"actual": 215.0, "target": 215.0},
            },
        },
        "job": {
            "progress": 42.0,
            "file": {"name": "cube.gcode", "display_name": "cube"},
        },
    }
    printer, seen = _prusalink(payload)
    state = printer.status()
    assert state.online is True
    assert state.state == "printing"
    assert state.busy is True
    assert state.progress == pytest.approx(0.42)
    assert state.filename == "cube.gcode"
    assert state.bed_temp == 60.0
    assert state.nozzle_temp == 215.0
    # Digest auth retried once after the 401 challenge.
    assert len([r for r in seen if r.url.path == "/api/v1/status"]) == 2
    assert any(r.headers["Authorization"].startswith("Digest ") for r in seen)


def test_prusalink_falls_back_to_legacy_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {key.lower() for key in request.headers}:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Digest realm="PrusaLink", nonce="n", qop="auth"'},
            )
        if request.url.path == "/api/v1/status":
            return httpx.Response(404, json={"error": "not found"})
        if request.url.path == "/api/printer":
            return httpx.Response(
                200,
                json={
                    "state": {"text": "Operational", "flags": {"operational": True}},
                    "temperature": {"bed": {"actual": 25.0}, "tool0": {"actual": 24.0}},
                },
            )
        if request.url.path == "/api/job":
            return httpx.Response(200, json={"job": {"file": {"name": "old.gcode"}}})
        return httpx.Response(404)

    printer = PrusaLinkPrinter(
        "mk4",
        base_url="http://prusa.local",
        password="secret",
        transport=httpx.MockTransport(handler),
    )
    state = printer.status()
    assert state.state == "idle"
    assert state.filename == "old.gcode"
    assert state.bed_temp == 25.0


def test_prusalink_upload_uses_digest_and_boolean_headers(tmp_path):
    seen: list[httpx.Request] = []
    printer = PrusaLinkPrinter(
        "mk4",
        base_url="http://prusa.local",
        password="secret",
        transport=httpx.MockTransport(_digest_handler({}, seen=seen)),
    )
    gcode = tmp_path / "cube.gcode"
    gcode.write_bytes(b"G28\nG1 X1 E0.1\n")
    remote = printer.upload(gcode, name="cube.gcode")
    assert remote == "local/cube.gcode"
    put = [r for r in seen if r.method == "PUT"]
    assert put, seen
    assert put[-1].url.path == "/api/v1/files/local/cube.gcode"
    assert put[-1].headers["Print-After-Upload"] == "?0"
    assert put[-1].headers["Overwrite"] == "?1"
    assert put[-1].read() == b"G28\nG1 X1 E0.1\n"


def test_prusalink_start_and_cancel_paths():
    seen: list[httpx.Request] = []
    printer = PrusaLinkPrinter(
        "mk4",
        base_url="http://prusa.local",
        password="secret",
        transport=httpx.MockTransport(_digest_handler({}, seen=seen)),
    )
    printer.start("local/cube.gcode")
    printer.cancel()
    assert any(r.method == "POST" and r.url.path == "/api/v1/files/local/cube.gcode" for r in seen)
    assert any(r.method == "DELETE" and r.url.path == "/api/v1/job" for r in seen)


def test_prusalink_requires_password():
    with pytest.raises(AdapterUnavailable):
        PrusaLinkPrinter("mk4", base_url="http://prusa.local", password="")


HTTP_CONFIG = {
    "status": {
        "path": "/state",
        "map": {
            "state": "state",
            "progress": "job.percent",
            "filename": "job.file",
            "bed_temp": "temps.bed",
            "nozzle_temp": "temps.nozzle",
        },
        "states": {"IDLE": "idle", "RUN": "printing", "PAUSE": "paused"},
    },
    "upload": {"path": "/files/{filename}", "method": "PUT"},
    "start": {
        "path": "/files/{filename}/start",
        "method": "POST",
        "body": {"start": True},
    },
    "cancel": {"path": "/job/stop", "method": "POST"},
}


def _http_printer(handler, **kwargs):
    return HttpPrinter(
        "vendorp",
        base_url="http://printer.local",
        http=dict(HTTP_CONFIG),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_http_status_mapping_and_progress():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/state"
        return httpx.Response(
            200,
            json={
                "state": "RUN",
                "job": {"percent": 37, "file": "bracket.gcode"},
                "temps": {"bed": 60, "nozzle": 210},
            },
        )

    state = _http_printer(handler).status()
    assert state.state == "printing"
    assert state.busy is True
    assert state.progress == pytest.approx(0.37)
    assert state.filename == "bracket.gcode"
    assert state.bed_temp == 60
    assert state.nozzle_temp == 210


def test_http_upload_start_cancel(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    printer = _http_printer(handler)
    gcode = tmp_path / "bracket.gcode"
    gcode.write_bytes(b"G28\n")
    printer.upload(gcode, name="bracket.gcode")
    printer.start("bracket.gcode")
    printer.cancel()

    put = [r for r in seen if r.method == "PUT"]
    assert put[0].url.path == "/files/bracket.gcode"
    assert put[0].read() == b"G28\n"
    assert any(r.method == "POST" and r.url.path == "/files/bracket.gcode/start" for r in seen)
    assert any(r.method == "POST" and r.url.path == "/job/stop" for r in seen)
    start = [r for r in seen if r.url.path == "/files/bracket.gcode/start"][0]
    assert start.read() == b'{"start":true}'


def test_http_header_auth_uses_api_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"state": "IDLE"})

    config = dict(HTTP_CONFIG)
    config["auth"] = {"type": "header", "header": "X-Api-Key"}
    printer = HttpPrinter(
        "vendorp",
        base_url="http://printer.local",
        http=config,
        api_key="from-secrets",
        transport=httpx.MockTransport(handler),
    )
    printer.status()
    assert seen[0].headers["X-Api-Key"] == "from-secrets"


def test_http_requires_sections():
    with pytest.raises(InvalidArgument) as excinfo:
        HttpPrinter(
            "vendorp",
            base_url="http://printer.local",
            http={"status": {"path": "/state", "map": {"state": "state"}}},
        )
    assert "upload" in str(excinfo.value)


def test_http_rejects_unknown_placeholder(tmp_path):
    config = dict(HTTP_CONFIG)
    config["upload"] = {"path": "/files/{nope}"}
    printer = HttpPrinter("vendorp", base_url="http://printer.local", http=config)
    gcode = tmp_path / "x.gcode"
    gcode.write_bytes(b"G28\n")
    with pytest.raises(InvalidArgument) as excinfo:
        printer.upload(gcode, name="x.gcode")
    assert "nope" in str(excinfo.value)


def test_build_printer_dispatch_prusalink():
    settings = PrinterSettings(
        id="mk4",
        adapter="prusalink",
        base_url="http://prusa.local",
        username="maker",
        storage="usb",
    )
    printer = build_printer(settings, secrets=Secrets({"providers": {"mk4": "pw"}}))
    assert isinstance(printer, PrusaLinkPrinter)
    assert printer.storage == "usb"


def test_build_printer_http_requires_config():
    settings = PrinterSettings(id="vendorp", adapter="http", base_url="http://x")
    with pytest.raises(InvalidArgument):
        build_printer(settings, secrets=Secrets({}))
