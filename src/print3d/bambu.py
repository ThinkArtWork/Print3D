"""Bambu Lab LAN adapter: MQTT reports/commands + implicit FTPS upload.

Bambu printers in LAN mode expose two services:

* MQTT over TLS on port 8883 (user ``bblp``, password = LAN access code);
  commands are JSON on ``device/<serial>/request`` and reports arrive on
  ``device/<serial>/report``.
* An implicit-TLS FTP server on port 990 used to upload print projects
  (``.3mf`` archives whose ``Metadata/plate_1.gcode`` is the sliced plate).

This module is deliberately self-contained and does not import any vendor
code.  It implements only the small, documented-by-community subset Print3D
needs: read state, upload a 3MF, start a project, stop a print.

The protocol is reverse engineered from community clients and verified with
unit tests only; a real Bambu printer was not available while writing it.
"""

from __future__ import annotations

import ftplib
import io
import json
import random
import socket
import ssl
import string
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AdapterUnavailable, InvalidArgument
from .printers import PrinterState

BAMBU_MQTT_PORT = 8883
BAMBU_FTP_PORT = 990
BAMBU_MQTT_USER = "bblp"
DEFAULT_TIMEOUT_S = 30.0

#: ``gcode_state`` values reported by the printer.
_STATE_MAP = {
    "IDLE": "idle",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "PAUSED": "paused",
    "FINISH": "complete",
    "FINISHED": "complete",
    "FAILED": "error",
    "PREPARE": "preparing",
    "SLICING": "preparing",
    "UNKNOWN": "unknown",
}

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
  <Default Extension="gcode" ContentType="text/x.gcode"/>
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="config" ContentType="text/xml"/>
</Types>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices/>
        <triangles/>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1"/>
  </build>
</model>
"""

_SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
  </header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="0"/>
    <metadata key="weight" value="0"/>
  </plate>
</config>
"""


def build_3mf(gcode: bytes, *, name: str = "plate") -> bytes:
    """Wrap sliced G-code in the minimal 3MF layout Bambu printers accept."""
    if not gcode:
        raise InvalidArgument("Cannot build a 3MF from empty G-code")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("3D/3dmodel.model", _MODEL)
        archive.writestr("Metadata/plate_1.gcode", gcode)
        archive.writestr("Metadata/plate_1.json", json.dumps({"name": name, "index": 1}))
        archive.writestr("Metadata/slice_info.config", _SLICE_INFO)
    return buffer.getvalue()


class _BambuMqtt:
    """Thin paho-mqtt wrapper with one request -> reply round trip."""

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.timeout_s = timeout_s
        self._client: Any = None
        self._replies: list[dict[str, Any]] = []

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self._replies.append(payload)

    def connect(self) -> None:
        import paho.mqtt.client as mqtt

        client_id = "print3d-" + "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(BAMBU_MQTT_USER, self.access_code)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(context)
        client.on_message = self._on_message
        client.connect(self.host, BAMBU_MQTT_PORT, keepalive=30)
        client.subscribe(f"device/{self.serial}/report")
        client.loop_start()
        self._client = client

    def request(self, payload: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        if self._client is None:
            self.connect()
        else:
            self._client.subscribe(f"device/{self.serial}/report")
        self._replies.clear()
        self._client.publish(
            f"device/{self.serial}/request",
            json.dumps(payload),
            qos=1,
        )
        deadline = time.monotonic() + (timeout_s or self.timeout_s)
        while time.monotonic() < deadline:
            while self._replies:
                reply = self._replies.pop(0)
                if "print" in reply or "info" in reply:
                    return reply
            time.sleep(0.05)
        raise AdapterUnavailable(
            f"Bambu printer {self.host} did not answer within {self.timeout_s:.0f}s",
            hint="Check LAN mode, the access code, and that the printer serial is correct.",
        )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            finally:
                self._client = None


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Implicit-TLS FTP client: TLS starts before the FTP banner."""

    def __init__(
        self,
        host: str,
        access_code: str,
        *,
        port: int = BAMBU_FTP_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__(timeout=timeout_s)
        self.host = host
        self.access_code = access_code
        self.port = port
        self.timeout_s = timeout_s
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.context = context

    def connect(  # type: ignore[override]
        self,
        host: str = "",
        port: int = 0,
        timeout: float = -1,
        source_address: tuple[str, int] | None = None,
    ) -> str:
        sock = socket.create_connection(
            (host or self.host, port or self.port),
            timeout=timeout if timeout and timeout > 0 else self.timeout_s,
            source_address=source_address,
        )
        self.sock = self.context.wrap_socket(sock, server_hostname=host or self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def upload(self, local_path: Path, remote_path: str) -> None:
        self.connect()
        try:
            self.login(BAMBU_MQTT_USER, self.access_code)
            self.prot_p()
            with local_path.open("rb") as handle:
                self.storbinary(f"STOR {remote_path}", handle)
        finally:
            try:
                self.quit()
            except (OSError, EOFError):
                self.close()


@dataclass
class BambuLanPrinter:
    """Adapter for one Bambu Lab printer in LAN mode."""

    id: str
    host: str
    serial: str
    access_code: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    mqtt: Any | None = None
    ftp: Any | None = None
    _last_upload: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    adapter = "bambu_lan"

    def __post_init__(self) -> None:
        if not self.host:
            raise InvalidArgument(f"Printer {self.id} has no host")
        if not self.serial:
            raise InvalidArgument(
                f"Printer {self.id} has no serial",
                hint="Add the printer serial (Settings > Device on the printer).",
            )
        if not self.access_code:
            raise AdapterUnavailable(
                f"Printer {self.id} has no LAN access code",
                hint="Set the access code in secrets.json or via access_code_env.",
            )

    # -- transport --------------------------------------------------------
    def _mqtt(self) -> _BambuMqtt:
        if self.mqtt is None:
            self.mqtt = _BambuMqtt(
                self.host, self.serial, self.access_code, timeout_s=self.timeout_s
            )
        return self.mqtt

    def _ftp(self) -> Any:
        if self.ftp is None:
            self.ftp = _ImplicitFTP_TLS(self.host, self.access_code, timeout_s=self.timeout_s)
        return self.ftp

    # -- adapter API ------------------------------------------------------
    def status(self) -> PrinterState:
        reply = self._mqtt().request({"pushall": {}})
        payload = reply.get("print") or {}
        state = _STATE_MAP.get(str(payload.get("gcode_state", "")).upper(), "unknown")
        percent = payload.get("mc_percent")
        return PrinterState(
            online=True,
            state=state,
            detail=payload.get("gcode_state"),
            filename=payload.get("subtask_name") or payload.get("gcode_file"),
            progress=(float(percent) / 100.0) if isinstance(percent, (int, float)) else None,
            bed_temp=payload.get("bed_temper"),
            nozzle_temp=payload.get("nozzle_temper"),
            raw=payload,
        )

    def upload(self, path: Path, *, name: str) -> str:
        archive = build_3mf(path.read_bytes(), name=name)
        remote = f"/cache/{name}.3mf"
        with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as handle:
            handle.write(archive)
            temp = Path(handle.name)
        try:
            self._ftp().upload(temp, remote)
        finally:
            temp.unlink(missing_ok=True)
        self._last_upload = {"remote": remote, "md5": _md5(archive), "name": name}
        return remote

    def start(self, filename: str) -> None:
        remote = filename or self._last_upload.get("remote")
        if not remote:
            raise InvalidArgument("No uploaded 3MF to print")
        payload = {
            "print": {
                "sequence_id": str(int(time.time() * 1000)),
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "subtask_name": self._last_upload.get("name") or Path(remote).stem,
                "url": f"ftp://{self.host}{remote}",
                "md5": self._last_upload.get("md5", ""),
                "use_ams": False,
                "timelapse": False,
            }
        }
        self._mqtt().request(payload)

    def cancel(self) -> None:
        self._mqtt().request(
            {
                "print": {
                    "sequence_id": str(int(time.time() * 1000)),
                    "command": "stop",
                }
            }
        )


def _md5(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def parse_host(value: str) -> str:
    """Accept ``192.168.1.20``, ``http://…`` or ``https://…`` as base URL."""
    text = (value or "").strip()
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.split("/")[0].split(":")[0]


__all__ = [
    "BAMBU_FTP_PORT",
    "BAMBU_MQTT_PORT",
    "BambuLanPrinter",
    "build_3mf",
    "parse_host",
]
