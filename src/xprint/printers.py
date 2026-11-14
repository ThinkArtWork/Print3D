"""Printer adapters: status, upload, start and cancel.

Implemented adapters:

* ``moonraker`` - Klipper's documented HTTP API.
* ``octoprint`` - OctoPrint's documented REST API.
* ``bambu_lan``  - Bambu Lab LAN mode (MQTT/TLS + implicit FTPS + 3MF), see
  :mod:`xprint.bambu`.  The protocol is community documented and unit tested
  here, but not verified against real hardware yet.

No vendor code is bundled or linked.  Every write action goes through an
explicit ``confirm`` string in the MCP tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import PrinterSettings, Secrets
from .errors import AdapterUnavailable, AdapterUnknown, InvalidArgument

PRINTER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
PRINTER_ADAPTERS = ("moonraker", "octoprint", "bambu_lan")
DEFAULT_TIMEOUT_S = 30.0

#: States in which a printer must not receive a new job.
BUSY_STATES = {
    "printing",
    "paused",
    "pausing",
    "resuming",
    "cancelling",
    "starting",
    "busy",
    "sending",
}

#: States that are safe for a new upload/print.
IDLE_STATES = {
    "standby",
    "ready",
    "operational",
    "idle",
    "complete",
    "completed",
    "finished",
    "cancelled",
    "canceled",
    "offline",
    "unknown",
}


@dataclass
class PrinterState:
    """Normalised printer state shared by every adapter."""

    online: bool
    state: str
    detail: str | None = None
    filename: str | None = None
    progress: float | None = None
    bed_temp: float | None = None
    nozzle_temp: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.state.lower() in BUSY_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "state": self.state,
            "busy": self.busy,
            "detail": self.detail,
            "filename": self.filename,
            "progress": None if self.progress is None else round(self.progress, 4),
            "bed_temp": self.bed_temp,
            "nozzle_temp": self.nozzle_temp,
        }


def validate_printer_id(value: str) -> str:
    if not isinstance(value, str) or not PRINTER_ID_RE.fullmatch(value):
        raise InvalidArgument(
            f"Invalid printer id: {value!r}",
            hint="Use 1-32 letters, digits, '-' or '_' (for example 'u1').",
        )
    return value


class _HttpPrinter:
    adapter = "http"

    def __init__(
        self,
        printer_id: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise InvalidArgument(f"Printer {printer_id} has no base_url")
        self.id = printer_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._transport = transport

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self._transport,
            headers=self._headers(),
            follow_redirects=True,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._client() as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise AdapterUnavailable(
                f"{self.id}: {self.adapter} timed out after {self.timeout_s:g}s",
                hint="Check the printer address and network, or raise timeout_s.",
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterUnavailable(
                f"{self.id}: {self.adapter} is not reachable at {self.base_url}",
                details={"error": str(exc)},
                hint="Check base_url, that the printer is on, and its API is enabled.",
            ) from exc
        if response.status_code in (401, 403):
            raise AdapterUnavailable(
                f"{self.id}: {self.adapter} rejected the API key",
                hint="Set the api key (printer api_key_env or secrets.json providers.<id>).",
            )
        if response.status_code >= 400:
            raise AdapterUnavailable(
                f"{self.id}: {self.adapter} returned HTTP {response.status_code}",
                details={"body": response.text[:500]},
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # -- interface --------------------------------------------------------
    def status(self) -> PrinterState:  # pragma: no cover - overridden
        raise NotImplementedError

    def upload(self, path: Path, *, name: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def start(self, filename: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def cancel(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class MoonrakerPrinter(_HttpPrinter):
    """Klipper + Moonraker (documented REST API)."""

    adapter = "moonraker"

    def status(self) -> PrinterState:
        response = self._request(
            "GET",
            "/printer/objects/query",
            params={
                "print_stats": "",
                "heater_bed": "",
                "extruder": "",
                "virtual_sdcard": "",
            },
        )
        payload = self._json(response)
        status = (payload.get("result") or {}).get("status") or {}
        stats = status.get("print_stats") or {}
        progress = (status.get("virtual_sdcard") or {}).get("progress")
        bed = status.get("heater_bed") or {}
        nozzle = status.get("extruder") or {}
        state = str(stats.get("state") or "unknown")
        return PrinterState(
            online=True,
            state=state,
            detail=stats.get("message") or None,
            filename=stats.get("filename") or None,
            progress=float(progress) if isinstance(progress, (int, float)) else None,
            bed_temp=bed.get("temperature"),
            nozzle_temp=nozzle.get("temperature"),
            raw={"print_stats": stats},
        )

    def upload(self, path: Path, *, name: str) -> str:
        response = self._request(
            "POST",
            "/server/files/upload",
            files={"file": (name, path.read_bytes(), "application/octet-stream")},
            data={"root": "gcodes"},
        )
        payload = self._json(response)
        item = payload.get("item") or {}
        return str(item.get("path") or name)

    def start(self, filename: str) -> None:
        self._request("POST", "/printer/print/start", params={"filename": filename})

    def cancel(self) -> None:
        self._request("POST", "/printer/print/cancel")


class OctoPrintPrinter(_HttpPrinter):
    """OctoPrint's documented REST API (X-Api-Key)."""

    adapter = "octoprint"

    def status(self) -> PrinterState:
        printer = self._json(self._request("GET", "/api/printer"))
        job_payload = self._json(self._request("GET", "/api/job"))
        state_payload = printer.get("state") or {}
        flags = state_payload.get("flags") or {}
        text = str(state_payload.get("text") or "")
        if flags.get("printing"):
            state = "printing"
        elif flags.get("paused"):
            state = "paused"
        elif flags.get("error") or flags.get("closedOrError"):
            state = "error"
        elif flags.get("operational"):
            state = "operational"
        else:
            state = text.lower() or "unknown"
        progress_payload = (job_payload.get("progress") or {}).get("completion")
        progress = (
            float(progress_payload) / 100.0 if isinstance(progress_payload, (int, float)) else None
        )
        file_payload = (job_payload.get("job") or {}).get("file") or {}
        temps = printer.get("temperature") or {}
        tool = temps.get("tool0") or {}
        bed = temps.get("bed") or {}
        return PrinterState(
            online=True,
            state=state,
            detail=text or None,
            filename=file_payload.get("name") or None,
            progress=progress,
            bed_temp=bed.get("actual"),
            nozzle_temp=tool.get("actual"),
            raw={"flags": flags, "job": job_payload.get("job") or {}},
        )

    def upload(self, path: Path, *, name: str) -> str:
        response = self._request(
            "POST",
            "/api/files/local",
            files={"file": (name, path.read_bytes(), "application/octet-stream")},
        )
        payload = self._json(response)
        file_payload = (payload.get("files") or {}).get("local") or {}
        return str(file_payload.get("path") or file_payload.get("name") or name)

    def start(self, filename: str) -> None:
        self._request(
            "POST",
            f"/api/files/local/{quote(filename)}",
            json={"command": "select", "print": True},
        )

    def cancel(self) -> None:
        self._request("POST", "/api/job", json={"command": "cancel"})


_PRINTER_CLASSES: dict[str, type[_HttpPrinter]] = {
    "moonraker": MoonrakerPrinter,
    "octoprint": OctoPrintPrinter,
}


def build_printer(
    printer: PrinterSettings,
    *,
    secrets: Secrets,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Instantiate the adapter for one configured printer."""
    adapter = (printer.adapter or "").lower()
    if adapter not in PRINTER_ADAPTERS:
        raise AdapterUnknown(
            f"Unknown printer adapter: {printer.adapter}",
            hint=f"Supported adapters: {', '.join(PRINTER_ADAPTERS)}",
        )
    if not printer.enabled:
        raise AdapterUnavailable(f"Printer {printer.id} is disabled")
    if adapter == "bambu_lan":
        from .bambu import BambuLanPrinter, parse_host

        code = secrets.provider_key(
            printer.id,
            api_key_env=printer.access_code_env or printer.api_key_env,
        )
        if not code:
            raise AdapterUnavailable(
                f"Printer {printer.id} has no LAN access code",
                hint=(
                    "Store the printer's LAN access code in secrets.json under "
                    f"providers.{printer.id}, or set {printer.access_code_env or 'XPRINT_<ID>_ACCESS_CODE'}."
                ),
            )
        return BambuLanPrinter(
            printer.id,
            host=parse_host(printer.base_url or ""),
            serial=printer.serial or "",
            access_code=code,
            timeout_s=printer.timeout_s,
        )
    key = secrets.provider_key(printer.id, api_key_env=printer.api_key_env)
    return _PRINTER_CLASSES[adapter](
        printer.id,
        base_url=printer.base_url or "",
        api_key=key,
        timeout_s=printer.timeout_s,
        transport=transport,
    )
