"""Configuration and secret loading.

Layering (highest wins):

1. CLI flags (``--workspace``, ``--config``, ``--host``, ``--port``)
2. environment variables (``XPRINT_WORKSPACE``, ``XPRINT_CONFIG``, ...)
3. ``<workspace>/config.json``
4. built-in defaults

API keys are never stored in ``config.json``.  They come from the environment
(variable named by the provider entry's ``api_key_env``, else
``XPRINT_<ID>_API_KEY``, else ``<ID>_API_KEY``) or from ``secrets.json`` with
mode ``0600``.  Only ``has_key`` ever leaves this module.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError
from .workspace import DEFAULT_WORKSPACE, Workspace, atomic_write_text

ENV_PREFIX = "XPRINT_"


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    token_env: str = "XPRINT_TOKEN"


class ProvidersSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    cloud: dict[str, dict[str, Any]] = Field(default_factory=dict)
    local: dict[str, dict[str, Any]] = Field(default_factory=dict)
    default: str | None = None


class ImageProviderSettings(BaseModel):
    """An OpenAI-compatible text-to-image endpoint used for concept art.

    The concept image is the visual reference of the ``text -> image -> model``
    path; any endpoint that speaks ``POST /images/generations`` works.
    """

    model_config = ConfigDict(extra="allow")

    adapter: str = "openai"
    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-image-1"
    size: str = "1024x1024"
    timeout_s: float = Field(default=120.0, gt=1, le=600)
    api_key_env: str = "OPENAI_API_KEY"


class CadSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    backend: str = "text2cad"
    cad_server_version: str | None = None
    python: str | None = None
    timeout_s: float = Field(default=120.0, gt=1, le=600)


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    backend: str = "blender_bpy"
    blender_path: str | None = None


class TargetSettings(BaseModel):
    """A slicing target: which slicer, which profiles, which machine."""

    model_config = ConfigDict(extra="allow")

    printer_model: str
    slicer: str
    slicer_path: str | None = None
    profiles_root: str | None = None
    drop_profile_keys: list[str] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)


class PrinterSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    model: str | None = None
    adapter: str
    base_url: str | None = None
    toolheads: int = Field(default=1, ge=1, le=4)


class Config(BaseModel):
    model_config = ConfigDict(extra="allow")

    server: ServerSettings = Field(default_factory=ServerSettings)
    workspace: str = DEFAULT_WORKSPACE
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    images: dict[str, ImageProviderSettings] = Field(
        default_factory=lambda: {"openai": ImageProviderSettings()}
    )
    cad: CadSettings = Field(default_factory=CadSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    targets: dict[str, TargetSettings] = Field(default_factory=dict)
    printers: list[PrinterSettings] = Field(default_factory=list)

    def target(self, name: str) -> TargetSettings:
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(
                f"Unknown slicing target: {name}",
                hint=f"Configured targets: {', '.join(sorted(self.targets)) or '(none)'}",
            ) from None

    def printer(self, printer_id: str) -> PrinterSettings:
        for printer in self.printers:
            if printer.id == printer_id:
                return printer
        known = ", ".join(p.id for p in self.printers) or "(none)"
        raise ConfigError(
            f"Unknown printer: {printer_id}",
            hint=f"Configured printers: {known}",
        )


class LoadedConfig:
    """A validated config plus provenance for ``system_status``."""

    def __init__(
        self,
        config: Config,
        *,
        path: Path | None,
        exists: bool,
        warnings: list[str] | None = None,
    ) -> None:
        self.config = config
        self.path = path
        self.exists = exists
        self.warnings = list(warnings or [])


def default_config_dict(workspace: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
    """The config written by ``xprint init`` — no secrets, no machine paths."""
    return {
        "server": {"host": "127.0.0.1", "port": 8765, "token_env": "XPRINT_TOKEN"},
        "workspace": workspace,
        "providers": {
            "cloud": {
                "hunyuan": {
                    "adapter": "hunyuan",
                    "base_url": "https://api.ai3d.cloud.tencent.com",
                    "api_key_env": "HUNYUAN3D_API_KEY",
                },
                "tripo": {
                    "adapter": "tripo",
                    "base_url": "https://api.tripo3d.ai",
                    "api_key_env": "TRIPO_API_KEY",
                },
            },
            "local": {},
            "default": None,
        },
        "images": {
            "openai": {
                "adapter": "openai",
                "enabled": True,
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-image-1",
                "size": "1024x1024",
                "timeout_s": 120,
                "api_key_env": "OPENAI_API_KEY",
            },
        },
        "cad": {
            "backend": "text2cad",
            "cad_server_version": "0.11.10",
            "python": None,
            "timeout_s": 120,
        },
        "render": {"backend": "blender_bpy", "blender_path": None},
        "targets": {},
        "printers": [],
    }


def _read_json(path: Path) -> Any:
    try:
        # utf-8-sig: third-party tools sometimes write a BOM.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}", details={"path": str(path)}) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {path} at line {exc.lineno} column {exc.colno}: {exc.msg}",
            hint="Fix the JSON syntax, or delete the file and run `xprint init`.",
            details={"path": str(path)},
        ) from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"{name} must be an integer, got {raw!r}", details={"variable": name}
        ) from None


def load_config(
    *,
    path: str | os.PathLike[str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
) -> LoadedConfig:
    """Load and validate configuration, applying environment overrides."""
    warnings: list[str] = []

    env_workspace = os.environ.get(f"{ENV_PREFIX}WORKSPACE") or None
    base_workspace = str(workspace or env_workspace or DEFAULT_WORKSPACE)
    base = Workspace(base_workspace)

    explicit = Path(str(path)).expanduser() if path else None
    env_config = os.environ.get(f"{ENV_PREFIX}CONFIG") or None
    config_path = explicit or (Path(env_config).expanduser() if env_config else None)
    config_path = config_path or base.config_path

    if config_path.exists() and config_path.is_file():
        raw = _read_json(config_path)
        try:
            cfg = Config.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
            raise ConfigError(
                f"Invalid config at {where}: {first.get('msg')}",
                hint=f"Fix {config_path} or run `xprint init --force`.",
                details={"path": str(config_path), "errors": exc.errors()[:5]},
            ) from exc
        exists = True
    else:
        cfg = Config.model_validate(default_config_dict(base_workspace))
        exists = False
        warnings.append(
            f"No config at {config_path}; using defaults (run `xprint init` to write one)."
        )

    # Environment overrides (flags are applied by the CLI on top).
    if env_workspace:
        cfg.workspace = env_workspace
    host = os.environ.get(f"{ENV_PREFIX}HOST")
    if host:
        cfg.server.host = host
    cfg.server.port = _env_int(f"{ENV_PREFIX}PORT", cfg.server.port)

    return LoadedConfig(cfg, path=config_path, exists=exists, warnings=warnings)


def write_config(loaded: LoadedConfig, *, force: bool = False) -> Path:
    """Write the current config back to disk (used by ``xprint init``)."""
    target = loaded.path or Path(loaded.config.workspace).expanduser() / "config.json"
    if target.exists() and not force:
        raise ConfigError(
            f"Config already exists: {target}",
            hint="Pass --force to overwrite (a .bak copy is kept).",
        )
    if target.exists():
        backup = target.with_suffix(".json.bak")
        backup.write_text(target.read_text(encoding="utf-8-sig"), encoding="utf-8")
    payload = loaded.config.model_dump(mode="json")
    atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", mode=0o644)
    return target


def _provider_env_names(provider_id: str) -> list[str]:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", provider_id).upper()
    return [f"{ENV_PREFIX}{slug}_API_KEY", f"{slug}_API_KEY"]


class Secrets:
    """API keys from ``secrets.json`` (never logged, never echoed)."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        path: Path | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self._data = data or {}
        self.path = path
        self.warnings = list(warnings or [])

    @classmethod
    def load(cls, path: Path) -> Secrets:
        warnings: list[str] = []
        if not path.exists():
            return cls({}, path=path, warnings=warnings)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            warnings.append(
                f"secrets.json is accessible by other users (mode {mode:o}); chmod 600."
            )
        data = _read_json(path)
        if not isinstance(data, dict):
            raise ConfigError(
                f"{path} must contain a JSON object",
                hint='Expected {"providers": {"hunyuan": "..."}}.',
            )
        return cls(data, path=path, warnings=warnings)

    def raw_provider_key(self, provider_id: str) -> str | None:
        providers = self._data.get("providers")
        if isinstance(providers, dict):
            value = providers.get(provider_id)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def section(self, name: str) -> dict[str, Any]:
        """Return one top-level secrets section as a dict (empty when absent)."""
        value = self._data.get(name)
        return value if isinstance(value, dict) else {}

    def provider_key(self, provider_id: str, *, api_key_env: str | None = None) -> str | None:
        """Resolve a key from env first, then secrets.json."""
        names = [api_key_env] if api_key_env else []
        names.extend(_provider_env_names(provider_id))
        for name in names:
            if not name:
                continue
            value = os.environ.get(name)
            if value and value.strip():
                return value.strip()
        return self.raw_provider_key(provider_id)

    def has_key(self, provider_id: str, *, api_key_env: str | None = None) -> bool:
        return self.provider_key(provider_id, api_key_env=api_key_env) is not None

    def key_source(self, provider_id: str, *, api_key_env: str | None = None) -> str | None:
        names = [api_key_env] if api_key_env else []
        names.extend(_provider_env_names(provider_id))
        for name in names:
            if name and os.environ.get(name):
                return "env"
        return "secrets" if self.raw_provider_key(provider_id) else None


def redact(text: str, secrets: list[str | None]) -> str:
    """Mask every known secret inside ``text`` (for logs and errors)."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, mask_secret(secret))
    return out


def mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "***"
    return f"{secret[:2]}***{secret[-2:]}"


def load_all(
    *,
    path: str | os.PathLike[str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
) -> tuple[LoadedConfig, Secrets]:
    loaded = load_config(path=path, workspace=workspace)
    ws = Workspace(loaded.config.workspace)
    secrets = Secrets.load(ws.secrets_path)
    loaded.warnings.extend(secrets.warnings)
    return loaded, secrets


__all__ = [
    "Config",
    "ImageProviderSettings",
    "LoadedConfig",
    "Secrets",
    "default_config_dict",
    "load_all",
    "load_config",
    "mask_secret",
    "redact",
    "write_config",
]
