"""Bambu LAN adapter: 3MF packaging, report mapping and command payloads."""

from __future__ import annotations

import io
import zipfile

import pytest

from xprint.bambu import BambuLanPrinter, build_3mf, parse_host
from xprint.config import PrinterSettings, Secrets
from xprint.errors import AdapterUnavailable, InvalidArgument
from xprint.printers import build_printer

GCODE = b"; test\nG1 X1 Y1 E0.1\n"


class FakeMqtt:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.sent: list[dict] = []
        self.closed = False

    def request(self, payload, *, timeout_s=None):
        self.sent.append(payload)
        return self.reply

    def close(self):
        self.closed = True


class FakeFtp:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []

    def upload(self, local_path, remote_path):
        self.uploads.append((remote_path, local_path.read_bytes()))


def make_printer(**kwargs):
    defaults = dict(
        id="x1",
        host="192.168.1.50",
        serial="01S00C123",
        access_code="12345678",
        mqtt=FakeMqtt({"print": {}}),
        ftp=FakeFtp(),
    )
    defaults.update(kwargs)
    return BambuLanPrinter(**defaults)


def test_build_3mf_contains_plate_and_metadata():
    data = build_3mf(GCODE, name="plate")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert "Metadata/plate_1.gcode" in names
        assert "Metadata/slice_info.config" in names
        assert "3D/3dmodel.model" in names
        assert archive.read("Metadata/plate_1.gcode") == GCODE


def test_build_3mf_rejects_empty_gcode():
    with pytest.raises(InvalidArgument):
        build_3mf(b"", name="plate")


def test_parse_host_accepts_urls():
    assert parse_host("http://192.168.1.50:7125") == "192.168.1.50"
    assert parse_host("https://printer.local/") == "printer.local"
    assert parse_host("10.0.0.9") == "10.0.0.9"


def test_status_maps_bambu_report():
    mqtt = FakeMqtt(
        {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": 42,
                "subtask_name": "cube",
                "bed_temper": 60.0,
                "nozzle_temper": 210.0,
            }
        }
    )
    printer = make_printer(mqtt=mqtt)
    state = printer.status()
    assert state.online is True
    assert state.state == "printing"
    assert state.busy is True
    assert state.filename == "cube"
    assert state.progress == pytest.approx(0.42)
    assert state.bed_temp == 60.0
    assert state.nozzle_temp == 210.0


def test_status_maps_idle_and_finished():
    printer = make_printer(mqtt=FakeMqtt({"print": {"gcode_state": "IDLE"}}))
    assert printer.status().state == "idle"
    assert printer.status().busy is False
    printer = make_printer(mqtt=FakeMqtt({"print": {"gcode_state": "FINISH"}}))
    assert printer.status().state == "complete"


def test_upload_builds_3mf_and_returns_remote_path(tmp_path):
    ftp = FakeFtp()
    printer = make_printer(ftp=ftp)
    source = tmp_path / "plate.gcode"
    source.write_bytes(GCODE)
    remote = printer.upload(source, name="plate")
    assert remote == "/cache/plate.3mf"
    assert ftp.uploads[0][0] == "/cache/plate.3mf"
    with zipfile.ZipFile(io.BytesIO(ftp.uploads[0][1])) as archive:
        assert archive.read("Metadata/plate_1.gcode") == GCODE


def test_start_sends_project_file(tmp_path):
    mqtt = FakeMqtt({"print": {}})
    printer = make_printer(mqtt=mqtt)
    source = tmp_path / "plate.gcode"
    source.write_bytes(GCODE)
    printer.upload(source, name="plate")
    printer.start("/cache/plate.3mf")
    payload = mqtt.sent[-1]["print"]
    assert payload["command"] == "project_file"
    assert payload["param"] == "Metadata/plate_1.gcode"
    assert payload["url"] == "ftp://192.168.1.50/cache/plate.3mf"
    assert payload["subtask_name"] == "plate"
    assert len(payload["md5"]) == 32


def test_cancel_sends_stop():
    mqtt = FakeMqtt({"print": {}})
    printer = make_printer(mqtt=mqtt)
    printer.cancel()
    assert mqtt.sent[-1]["print"]["command"] == "stop"


def test_requires_serial_and_access_code():
    with pytest.raises(InvalidArgument):
        make_printer(serial="")
    with pytest.raises(AdapterUnavailable):
        make_printer(access_code="")


def test_build_printer_dispatch():
    settings = PrinterSettings(
        id="x1",
        adapter="bambu_lan",
        base_url="http://192.168.1.50",
        serial="01S00C123",
        model="Bambu Lab X1C",
    )
    secrets = Secrets({"providers": {"x1": "87654321"}})
    printer = build_printer(settings, secrets=secrets)
    assert isinstance(printer, BambuLanPrinter)
    assert printer.host == "192.168.1.50"
    assert printer.serial == "01S00C123"
    assert printer.access_code == "87654321"


def test_build_printer_requires_access_code():
    settings = PrinterSettings(
        id="x1",
        adapter="bambu_lan",
        base_url="http://192.168.1.50",
        serial="01S00C123",
    )
    with pytest.raises(AdapterUnavailable) as excinfo:
        build_printer(settings, secrets=Secrets({}))
    assert "access code" in str(excinfo.value)


def test_build_printer_unknown_adapter():
    settings = PrinterSettings(id="x1", adapter="vendor_cloud", base_url="http://x")
    from xprint.errors import AdapterUnknown

    with pytest.raises(AdapterUnknown):
        build_printer(settings, secrets=Secrets({}))
