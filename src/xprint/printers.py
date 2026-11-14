"""Printer adapters: status, upload, start and cancel.

Implemented adapters:

* ``moonraker`` - Klipper's documented HTTP API.
* ``octoprint`` - OctoPrint's documented REST API.
* ``bambu_lan``  - Bambu Lab LAN mode (MQTT/TLS + implicit FTPS + 3MF), see
  :mod:`xprint.bambu`.  The protocol is community documented and unit tested
  here, but not verified against real hardware yet.
* ``prusalink`` - Prusa's local HTTP API (HTTP Digest auth, v1 + legacy fallback).
* ``http``      - declarative adapter: endpoints, field paths and state names
  come from ``config.json`` so an unknown printer needs no code change.

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
PRINTER_ADAPTERS = ("moonraker", "octoprint", "bambu_lan", "prusalink", "http")
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
    build_plate: str | None = None
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


SLOT_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalise_slots(slots: list[dict[str, Any]] | None, *, toolheads: int) -> list[dict[str, Any]]:
    """Validate the per-slot material/colour table for a printer."""
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(slots or []):
        if not isinstance(raw, dict):
            raise InvalidArgument(f"slots[{index}] must be an object")
        material = str(raw.get("material") or "").strip().upper()
        color = str(raw.get("color") or "").strip()
        name = str(raw.get("name") or "").strip()
        if color and not SLOT_COLOR_RE.match(color):
            raise InvalidArgument(
                f"slots[{index}].color must look like #RRGGBB",
                details={"value": color},
            )
        toolhead = raw.get("toolhead")
        if toolhead is not None:
            try:
                toolhead = int(toolhead)
            except (TypeError, ValueError):
                raise InvalidArgument(f"slots[{index}].toolhead must be an integer") from None
            if not 0 <= toolhead < toolheads:
                raise InvalidArgument(
                    f"slots[{index}].toolhead must be between 0 and {toolheads - 1}"
                )
        item: dict[str, Any] = {}
        if material:
            item["material"] = material
        if color:
            item["color"] = color.upper()
        if name:
            item["name"] = name
        if toolhead is not None:
            item["toolhead"] = toolhead
        out.append(item)
    if len(out) > 16:
        raise InvalidArgument("at most 16 filament slots are supported")
    return out


FLOW_CALIBRATION_KEYS = (
    "enable_gcode",
    "disable_gcode",
    "factors_url",
    "factors_key",
    "extruder_keys",
    "trim_unused_tools",
    "tool_macros",
)


def normalise_flow_calibration(value: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the optional vendor flow-calibration block."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidArgument("flow_calibration must be an object")
    out: dict[str, Any] = {}
    for key in FLOW_CALIBRATION_KEYS:
        raw = value.get(key)
        if raw in (None, ""):
            continue
        if key in {"extruder_keys", "tool_macros"}:
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise InvalidArgument(f"flow_calibration.{key} must be a list of strings")
            out[key] = [item.strip() for item in raw if item.strip()]
        elif key == "trim_unused_tools":
            if not isinstance(raw, bool):
                raise InvalidArgument("flow_calibration.trim_unused_tools must be a boolean")
            out[key] = raw
        else:
            if not isinstance(raw, str):
                raise InvalidArgument(f"flow_calibration.{key} must be a string")
            out[key] = raw.strip()
    unknown = set(value) - set(FLOW_CALIBRATION_KEYS)
    if unknown:
        raise InvalidArgument(
            f"Unknown flow_calibration key(s): {', '.join(sorted(unknown))}",
            hint=f"Known keys: {', '.join(FLOW_CALIBRATION_KEYS)}",
        )
    return out


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
        flow_calibration: dict[str, Any] | None = None,
        plate_keys: list[str] | None = None,
    ) -> None:
        if not base_url:
            raise InvalidArgument(f"Printer {printer_id} has no base_url")
        self.id = printer_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._transport = transport
        self.flow_calibration_cfg = dict(flow_calibration or {})
        self.plate_keys = list(plate_keys or [])

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

    # -- calibration ------------------------------------------------------
    def run_gcode(self, script: str) -> None:
        raise AdapterUnavailable(
            f"The {self.adapter!r} adapter cannot send raw G-code",
            hint="Configure a printer adapter that supports gcode/script, or calibrate in the slicer.",
        )

    def flow_calibration(self) -> dict[str, Any]:
        """Read vendor flow/pressure-advance factors from a configured JSON file."""
        url = str(self.flow_calibration_cfg.get("factors_url") or "").strip()
        if not url:
            return {"source": None, "factors": {}, "env": {}}
        payload = self._json(self._request("GET", url))
        env = payload.get("env") if isinstance(payload, dict) else {}
        return {"source": url, "factors": self._flow_factors(payload), "env": env or {}}

    def _flow_factors(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(self.flow_calibration_cfg.get("factors_key") or "factor")
        node: Any = payload
        for part in key.split("."):
            if not isinstance(node, dict):
                node = {}
                break
            node = node.get(part)
        if not isinstance(node, dict):
            return {}
        keys = self.flow_calibration_cfg.get("extruder_keys") or []
        if keys:
            factors = {}
            for index, name in enumerate(keys):
                value = node.get(name)
                if isinstance(value, (int, float)):
                    factors[f"T{index}"] = float(value)
            return factors
        return {
            str(name): float(value)
            for name, value in node.items()
            if isinstance(value, (int, float))
        }

    def set_flow_calibrate(self, enabled: bool) -> None:
        """Run the configured calibration-enable/disable G-code before a print."""
        key = "enable_gcode" if enabled else "disable_gcode"
        script = str(self.flow_calibration_cfg.get(key) or "").strip()
        if not script:
            raise AdapterUnavailable(
                f"Printer {self.id} has no flow-calibration command configured",
                hint=(
                    "Set printers[].flow_calibration.enable_gcode (and disable_gcode) "
                    "to your firmware's calibration macro, then call printer_save."
                ),
            )
        self.run_gcode(script)

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
                "print_task_config": "",
            },
        )
        payload = self._json(response)
        status = (payload.get("result") or {}).get("status") or {}
        stats = status.get("print_stats") or {}
        progress = (status.get("virtual_sdcard") or {}).get("progress")
        bed = status.get("heater_bed") or {}
        nozzle = status.get("extruder") or {}
        state = str(stats.get("state") or "unknown")
        task = status.get("print_task_config") or {}
        detected = None
        keys = self.plate_keys or ["build_plate", "bed_type", "plate_type", "curr_bed_type"]
        for key in keys:
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                detected = value.strip()
                break
        return PrinterState(
            online=True,
            state=state,
            detail=stats.get("message") or None,
            filename=stats.get("filename") or None,
            progress=float(progress) if isinstance(progress, (int, float)) else None,
            bed_temp=bed.get("temperature"),
            nozzle_temp=nozzle.get("temperature"),
            build_plate=detected,
            raw={"print_stats": stats, "print_task_config": task},
        )

    def run_gcode(self, script: str) -> None:
        self._request("POST", "/printer/gcode/script", params={"script": script})

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

    def run_gcode(self, script: str) -> None:
        self._request("POST", "/api/printer/command", json={"commands": [script]})

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


def _render_template(template: str, values: dict[str, str], *, label: str) -> str:
    """Fill ``{name}`` placeholders, refusing unknown keys instead of crashing."""

    try:
        rendered = template.format(**values)
    except KeyError as exc:
        raise InvalidArgument(
            f"{label} references an unknown placeholder: {exc.args[0]}",
            hint=f"Available placeholders: {', '.join(sorted(values))}",
        ) from exc
    except (IndexError, ValueError) as exc:
        raise InvalidArgument(f"{label} is not a valid template: {template!r}") from exc
    if not rendered.startswith("/"):
        raise InvalidArgument(
            f"{label} must be an absolute path on the printer, got {rendered!r}",
            hint='Start endpoint paths with "/" (for example "/api/status").',
        )
    return rendered


def _dig(payload: Any, path: str | None, default: Any = None) -> Any:
    if not path:
        return default
    value: Any = payload
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if 0 <= index < len(value) else None
        else:
            return default
        if value is None:
            return default
    return value


_PRUSALINK_STATES = {
    "IDLE": "idle",
    "READY": "idle",
    "PRINTING": "printing",
    "PAUSED": "paused",
    "FINISHED": "complete",
    "STOPPED": "cancelled",
    "CANCELLED": "cancelled",
    "ERROR": "error",
    "ATTENTION": "error",
    "BUSY": "busy",
    "PREPARING": "preparing",
}


class PrusaLinkPrinter(_HttpPrinter):
    """PrusaLink (local Prusa firmware API, HTTP Digest auth)."""

    adapter = "prusalink"

    def __init__(
        self,
        printer_id: str,
        *,
        base_url: str,
        username: str = "maker",
        password: str | None = None,
        storage: str = "local",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            printer_id,
            base_url=base_url,
            api_key=None,
            timeout_s=timeout_s,
            transport=transport,
        )
        self.username = username or "maker"
        self.password = password or ""
        self.storage = storage or "local"
        if not self.password:
            raise AdapterUnavailable(
                f"Printer {printer_id} has no PrusaLink password",
                hint=(
                    f"Store the PrusaLink password in secrets.json under providers.{printer_id}, "
                    "or set password_env."
                ),
            )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self._transport,
            headers={"Accept": "application/json"},
            auth=httpx.DigestAuth(self.username, self.password),
            follow_redirects=True,
        )

    def _file_path(self, filename: str) -> str:
        name = filename
        prefix = f"{self.storage}/"
        if name.startswith(prefix):
            name = name[len(prefix) :]
        return f"/api/v1/files/{self.storage}/{quote(name, safe='')}"

    def status(self) -> PrinterState:
        try:
            payload = self._json(self._request("GET", "/api/v1/status"))
        except AdapterUnavailable as first_error:
            try:
                return self._legacy_status()
            except AdapterUnavailable:
                raise first_error from None
        printer = payload.get("printer") or {}
        job = payload.get("job") or {}
        temps = printer.get("temperature") or {}
        raw_state = str(printer.get("state") or "unknown")
        state = _PRUSALINK_STATES.get(raw_state.upper(), raw_state.lower())
        progress = job.get("progress")
        if isinstance(progress, (int, float)):
            progress = float(progress) / 100.0 if progress > 1 else float(progress)
        else:
            progress = None
        file_payload = job.get("file") or {}
        bed = (temps.get("bed") or {}).get("actual")
        tool = (temps.get("tool0") or {}).get("actual")
        return PrinterState(
            online=True,
            state=state,
            detail=raw_state,
            filename=file_payload.get("name") or file_payload.get("display_name"),
            progress=progress,
            bed_temp=bed,
            nozzle_temp=tool,
            raw={"printer": printer, "job": job},
        )

    def _legacy_status(self) -> PrinterState:
        """Fallback for firmware that only ships the OctoPrint-compatible shim."""

        printer = self._json(self._request("GET", "/api/printer"))
        job = self._json(self._request("GET", "/api/job"))
        state_payload = printer.get("state") or {}
        flags = state_payload.get("flags") or {}
        if flags.get("printing"):
            state = "printing"
        elif flags.get("paused"):
            state = "paused"
        elif flags.get("error") or flags.get("closedOrError"):
            state = "error"
        elif flags.get("operational"):
            state = "idle"
        else:
            state = str(state_payload.get("text") or "unknown").lower()
        temps = printer.get("temperature") or {}
        progress = (job.get("progress") or {}).get("completion")
        file_payload = (job.get("job") or {}).get("file") or {}
        return PrinterState(
            online=True,
            state=state,
            detail=state_payload.get("text"),
            filename=file_payload.get("name"),
            progress=float(progress) / 100.0 if isinstance(progress, (int, float)) else None,
            bed_temp=(temps.get("bed") or {}).get("actual"),
            nozzle_temp=(temps.get("tool0") or {}).get("actual"),
            raw={"printer": printer, "job": job, "legacy": True},
        )

    def upload(self, path: Path, *, name: str) -> str:
        self._request(
            "PUT",
            self._file_path(name),
            content=path.read_bytes(),
            headers={
                "Content-Type": "application/octet-stream",
                "Print-After-Upload": "?0",
                "Overwrite": "?1",
            },
        )
        return f"{self.storage}/{name}"

    def start(self, filename: str) -> None:
        self._request("POST", self._file_path(filename))

    def cancel(self) -> None:
        self._request("DELETE", "/api/v1/job")


_HTTP_SECTIONS = {
    "status": ("path",),
    "upload": ("path",),
    "start": ("path",),
    "cancel": ("path",),
}


def validate_http_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the declarative adapter block, returning it for chaining."""

    if not isinstance(config, dict) or not config:
        raise InvalidArgument(
            "The http adapter needs an 'http' block",
            hint=(
                'Example: {"status": {"path": "/api/status", "map": {"state": "state"}}, '
                '"upload": {"path": "/api/files/{filename}"}, '
                '"start": {"path": "/api/files/{filename}/start"}, '
                '"cancel": {"path": "/api/job/cancel"}}'
            ),
        )
    for section, required in _HTTP_SECTIONS.items():
        value = config.get(section)
        if not isinstance(value, dict):
            raise InvalidArgument(
                f"The http adapter is missing the {section!r} section",
                hint=f'Add "http"."{section}" with a path.',
            )
        for key in required:
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise InvalidArgument(
                    f"The http adapter's {section!r} section needs a {key!r} string"
                )
        method = value.get("method")
        if method is not None and not isinstance(method, str):
            raise InvalidArgument(f"The http adapter's {section!r} method must be a string")
    status_map = config["status"].get("map")
    if not isinstance(status_map, dict) or not isinstance(status_map.get("state"), str):
        raise InvalidArgument(
            'The http adapter\'s "status" section needs a "map" with a "state" field path',
            hint='Example: "map": {"state": "state", "progress": "progress"}',
        )
    return config


class HttpPrinter(_HttpPrinter):
    """Declarative adapter driven by the printer's ``http`` config block."""

    adapter = "http"

    def __init__(
        self,
        printer_id: str,
        *,
        base_url: str,
        http: dict[str, Any],
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        storage: str = "local",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            printer_id,
            base_url=base_url,
            api_key=api_key,
            timeout_s=timeout_s,
            transport=transport,
        )
        self.http = validate_http_config(http)
        self.username = username
        self.password = password
        self.storage = storage or "local"
        auth = self.http.get("auth") or {}
        kind = str(auth.get("type") or "none").lower()
        if kind not in {"none", "header", "basic", "digest"}:
            raise InvalidArgument(
                f"Unknown http auth type: {auth.get('type')}",
                hint="Use one of: none, header, basic, digest.",
            )
        self._auth_kind = kind

    def _client(self) -> httpx.Client:
        auth: httpx.Auth | None = None
        if self._auth_kind == "basic":
            auth = httpx.BasicAuth(self.username or "", self.password or "")
        elif self._auth_kind == "digest":
            auth = httpx.DigestAuth(self.username or "", self.password or "")
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self._transport,
            headers=self._headers(),
            auth=auth,
            follow_redirects=True,
        )

    def _values(self, **extra: str) -> dict[str, str]:
        values = {
            "base_url": self.base_url,
            "printer_id": self.id,
            "api_key": self.api_key or "",
            "username": self.username or "",
            "storage": self.storage,
        }
        values.update(extra)
        return values

    def _section(self, name: str) -> dict[str, Any]:
        return self.http[name]

    def _call(self, name: str, *, filename: str | None = None) -> httpx.Response:
        section = self._section(name)
        values = self._values(
            filename=quote(filename or "", safe=""),
            name=filename or "",
        )
        path = _render_template(str(section["path"]), values, label=f"http.{name}.path")
        method = str(section.get("method") or ("GET" if name == "status" else "POST")).upper()
        headers = {
            str(key): _render_template(str(value), values, label=f"http.{name}.headers")
            if isinstance(value, str) and value.startswith("/")
            else str(value)
            for key, value in (section.get("headers") or {}).items()
        }
        kwargs: dict[str, Any] = {"headers": headers} if headers else {}
        body = section.get("body")
        if isinstance(body, dict):
            kwargs["json"] = body
        return self._request(method, path, **kwargs)

    def status(self) -> PrinterState:
        section = self._section("status")
        values = self._values()
        path = _render_template(str(section["path"]), values, label="http.status.path")
        method = str(section.get("method") or "GET").upper()
        payload = self._json(self._request(method, path))
        mapping = section.get("map") or {}
        states = {str(k).upper(): str(v) for k, v in (section.get("states") or {}).items()}
        raw_state = str(_dig(payload, mapping.get("state"), "unknown"))
        state = states.get(raw_state.upper(), raw_state.lower())
        progress = _dig(payload, mapping.get("progress"))
        if isinstance(progress, (int, float)):
            if section.get("progress_percent", True) or progress > 1:
                progress = float(progress) / 100.0
            else:
                progress = float(progress)
        else:
            progress = None
        return PrinterState(
            online=True,
            state=state,
            detail=raw_state,
            filename=_dig(payload, mapping.get("filename")),
            progress=progress,
            bed_temp=_dig(payload, mapping.get("bed_temp")),
            nozzle_temp=_dig(payload, mapping.get("nozzle_temp")),
            raw={},
        )

    def upload(self, path: Path, *, name: str) -> str:
        section = self._section("upload")
        values = self._values(filename=quote(name, safe=""), name=name)
        target = _render_template(str(section["path"]), values, label="http.upload.path")
        method = str(section.get("method") or "PUT").upper()
        data = path.read_bytes()
        headers: dict[str, str] = {}
        if section.get("multipart"):
            field_name = str(section.get("field") or "file")
            content_type = str(section.get("content_type") or "application/octet-stream")
            self._request(method, target, files={field_name: (name, data, content_type)})
        else:
            headers["Content-Type"] = str(section.get("content_type") or "application/octet-stream")
            for key, value in (section.get("headers") or {}).items():
                headers[str(key)] = str(value).format(
                    **values,
                )
            self._request(method, target, content=data, headers=headers)
        return name

    def start(self, filename: str) -> None:
        self._call("start", filename=filename)

    def cancel(self) -> None:
        self._call("cancel")


_PRINTER_CLASSES: dict[str, type[_HttpPrinter]] = {
    "moonraker": MoonrakerPrinter,
    "octoprint": OctoPrintPrinter,
    "prusalink": PrusaLinkPrinter,
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
    if adapter == "http":
        return HttpPrinter(
            printer.id,
            base_url=printer.base_url or "",
            http=printer.http,
            api_key=key,
            username=printer.username,
            timeout_s=printer.timeout_s,
            transport=transport,
        )
    if adapter == "prusalink":
        password = secrets.provider_key(
            printer.id,
            api_key_env=printer.password_env or printer.api_key_env,
        )
        return PrusaLinkPrinter(
            printer.id,
            base_url=printer.base_url or "",
            username=printer.username or "maker",
            password=password,
            storage=printer.storage,
            timeout_s=printer.timeout_s,
            transport=transport,
        )
    return _PRINTER_CLASSES[adapter](
        printer.id,
        base_url=printer.base_url or "",
        api_key=key,
        timeout_s=printer.timeout_s,
        transport=transport,
        flow_calibration=printer.flow_calibration,
        plate_keys=printer.plate_keys,
    )
