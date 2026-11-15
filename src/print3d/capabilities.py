"""Capability discovery.

``capabilities`` tells an agent what this machine can actually do — it must
never claim an adapter works when the tool is missing.  Each unavailable entry
carries a ``reason`` and a ``hint``, so the agent can tell the user what to
install instead of guessing.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, processes
from .config import LoadedConfig, Secrets
from .jobs import JobStore
from .registry import Registry
from .slicers import normalise_slicer
from .workspace import Workspace


def _exists(raw: str | None) -> bool:
    if not raw:
        return False
    return Path(raw).expanduser().exists()


def _which(name: str) -> str | None:
    return shutil.which(name)


def probe_providers(cfg: LoadedConfig, secrets: Secrets) -> dict[str, Any]:
    config = cfg.config
    cloud = []
    for provider_id, entry in sorted(config.providers.cloud.items()):
        adapter = str(entry.get("adapter", provider_id)).strip().lower().replace("-", "_")
        if adapter == "hunyuan":
            secret_id = os.environ.get(str(entry.get("secret_id_env") or "")) or (
                secrets.provider_field(provider_id, "secret_id")
            )
            secret_key = os.environ.get(str(entry.get("secret_key_env") or "")) or (
                secrets.provider_field(provider_id, "secret_key")
            )
            has_key = bool(secret_id and secret_key)
            key_source = (
                "env"
                if os.environ.get(str(entry.get("secret_id_env") or ""))
                else ("secrets" if secret_id else None)
            )
        else:
            has_key = secrets.has_key(provider_id, api_key_env=entry.get("api_key_env"))
            key_source = secrets.key_source(provider_id, api_key_env=entry.get("api_key_env"))
        enabled = bool(entry.get("enabled", True))
        cloud.append(
            {
                "id": provider_id,
                "adapter": adapter,
                "enabled": enabled,
                "base_url": entry.get("base_url") or entry.get("region"),
                "model": entry.get("model"),
                "has_key": has_key,
                "key_source": key_source,
                "ready": bool(enabled and has_key),
                "configured": True,
            }
        )
    local = []
    for provider_id, entry in sorted(config.providers.local.items()):
        command = entry.get("command")
        tokens = (
            command
            if isinstance(command, list)
            else ([command] if isinstance(command, str) else [])
        )
        executable = str(tokens[0]) if tokens else None
        available = bool(executable) and (
            Path(executable).expanduser().is_file() or bool(shutil.which(executable))
        )
        local.append(
            {
                "id": provider_id,
                "adapter": str(entry.get("adapter", provider_id)),
                "mode": str(entry.get("mode") or "both"),
                "enabled": bool(entry.get("enabled", True)),
                "command": executable,
                "available": available,
                "configured": True,
            }
        )
    return {
        "cloud": cloud,
        "local": local,
        "default": config.providers.default,
        "usable": [item["id"] for item in cloud if item["ready"]],
    }


def probe_images(cfg: LoadedConfig, secrets: Secrets) -> list[dict[str, Any]]:
    """Report text-to-image providers (concept art) without exposing keys."""
    entries: list[dict[str, Any]] = []
    for image_id, settings in sorted(cfg.config.images.items()):
        key = secrets.provider_key(image_id, api_key_env=settings.api_key_env)
        entries.append(
            {
                "id": image_id,
                "adapter": image_id,
                "enabled": settings.enabled,
                "model": settings.model,
                "base_url": settings.base_url,
                "size": settings.size,
                "has_key": key is not None,
                "key_source": secrets.key_source(image_id, api_key_env=settings.api_key_env),
                "ready": bool(settings.enabled and key),
            }
        )
    return entries


_BUILD123D_PROBE = "import importlib.metadata as m; print(m.version('build123d'))"


def _build123d_version(python: str | None) -> tuple[str | None, str]:
    """Return the build123d version in the CAD interpreter and where it was read.

    The default is the interpreter running Print3D; ``cad.python`` can point at a
    separate environment, in which case the version is read in a subprocess with
    the same scrubbed environment the worker will get.
    """

    if not python:
        try:
            version = importlib.metadata.version("build123d")
        except importlib.metadata.PackageNotFoundError:
            return None, "current interpreter"
        return version, "current interpreter"
    try:
        from .cad import clean_env

        completed = processes.run_capture(
            [python, "-I", "-c", _BUILD123D_PROBE],
            timeout=30,
            env=clean_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None, f"cad.python={python}"
    version = completed.stdout.strip() if completed.returncode == 0 else ""
    return (version or None), f"cad.python={python}"


def probe_cad(cfg: LoadedConfig) -> dict[str, Any]:
    backend = cfg.config.cad.backend
    if backend != "text2cad":
        return {
            "backend": backend,
            "available": False,
            "reason": f"CAD backend {backend!r} has no implementation yet",
            "hint": "Set cad.backend to 'text2cad'.",
        }
    python = cfg.config.cad.python
    version, source = _build123d_version(python)
    worker = Path(__file__).with_name("cad_worker.py")
    available = bool(version) and worker.is_file()
    reason = None
    if not version:
        reason = f"build123d is not installed in {source}"
    elif not worker.is_file():
        reason = f"cad_worker.py is missing next to {Path(__file__).name}"
    return {
        "backend": backend,
        "available": available,
        "interpreter": python or sys.executable,
        "checked_in": source,
        "build123d": version,
        "worker": str(worker) if worker.is_file() else None,
        "reason": reason,
        "hint": None
        if available
        else 'Install build123d in the CAD interpreter: `uv pip install "build123d>=0.9"`, '
        "or set cad.python to an interpreter that has it.",
    }


def probe_render(cfg: LoadedConfig) -> dict[str, Any]:
    """Report whether ``blender_run`` can start.

    ``blender_run`` always launches the Blender application (``find_blender``);
    ``render.backend`` is only a label kept for configuration compatibility, so
    availability is decided by the executable, not by the backend name.
    """

    from .blender import find_blender
    from .errors import InvalidArgument

    backend = cfg.config.render.backend
    try:
        found = find_blender(cfg.config.render.blender_path)
    except InvalidArgument as exc:
        found = None
        reason = str(exc)
        hint = exc.hint
    else:
        reason = None if found else "Blender application not found"
        hint = (
            None
            if found
            else "Install Blender, or set render.blender_path to the Blender executable."
        )
    return {
        "backend": backend,
        "available": found is not None,
        "executable": str(found) if found else None,
        "bpy_importable": importlib.util.find_spec("bpy") is not None,
        "reason": reason,
        "hint": hint,
    }


def probe_slicers(cfg: LoadedConfig, registry: Registry) -> list[dict[str, Any]]:
    slicers = []
    for target_name, target in sorted(cfg.config.targets.items()):
        adapter_known = registry.has("slicer", normalise_slicer(target.slicer))
        path_present = _exists(target.slicer_path) if target.slicer_path else None
        profiles_present = _exists(target.profiles_root) if target.profiles_root else None
        problems = []
        if not adapter_known:
            problems.append(f"no slicer adapter registered for {target.slicer!r}")
        if path_present is False:
            problems.append("slicer_path does not exist")
        if profiles_present is False:
            problems.append("profiles_root does not exist")
        slicers.append(
            {
                "target": target_name,
                "printer_model": target.printer_model,
                "slicer": target.slicer,
                "adapter_known": adapter_known,
                "slicer_path": target.slicer_path,
                "slicer_present": path_present,
                "profiles_root": target.profiles_root,
                "profiles_present": profiles_present,
                "available": not problems,
                "problems": problems,
            }
        )
    return slicers


def probe_printers(cfg: LoadedConfig, registry: Registry) -> list[dict[str, Any]]:
    printers = []
    for printer in cfg.config.printers:
        printers.append(
            {
                "id": printer.id,
                "model": printer.model,
                "adapter": printer.adapter,
                "base_url": printer.base_url,
                "toolheads": printer.toolheads,
                "adapter_known": registry.has("printer", printer.adapter.lower()),
                "configured": True,
            }
        )
    return printers


def build_capabilities(
    loaded: LoadedConfig,
    secrets: Secrets,
    registry: Registry,
) -> dict[str, Any]:
    """Full capability report.  Read-only: touches no network, writes nothing."""
    providers = probe_providers(loaded, secrets)
    images = probe_images(loaded, secrets)
    cad = probe_cad(loaded)
    render = probe_render(loaded)
    slicers = probe_slicers(loaded, registry)
    printers = probe_printers(loaded, registry)
    return {
        "ok": True,
        "writes_performed": False,
        "version": __version__,
        "providers": providers,
        "images": images,
        "cad": cad,
        "render": render,
        "slicers": slicers,
        "printers": printers,
        "adapters": registry.snapshot(),
        "sandbox": processes.describe_limits(),
        "summary": {
            "cloud_providers_usable": len(providers["usable"]),
            "cloud_providers_ready": sum(1 for item in providers["cloud"] if item["ready"]),
            "image_providers_ready": sum(1 for item in images if item["ready"]),
            "local_providers": len(providers["local"]),
            "local_providers_ready": sum(
                1 for item in providers["local"] if item["available"] and item["enabled"]
            ),
            "cad_available": cad["available"],
            "render_available": render["available"],
            "slicers_available": sum(1 for item in slicers if item["available"]),
            "printers_configured": len(printers),
        },
        "warnings": loaded.warnings,
    }


def build_system_status(
    loaded: LoadedConfig,
    secrets: Secrets,
    workspace: Workspace,
    store: JobStore,
    registry: Registry,
) -> dict[str, Any]:
    """Cheap health report used by hosts on connect.  Never writes."""
    counts = store.counts()
    return {
        "ok": True,
        "writes_performed": False,
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "workspace": {
            "root": str(workspace.root),
            "config_path": str(loaded.path) if loaded.path else None,
            "config_present": loaded.exists,
            "secrets_present": workspace.secrets_path.exists(),
        },
        "jobs": counts,
        "adapters": registry.snapshot(),
        "sandbox": processes.describe_limits(),
        "warnings": loaded.warnings,
    }
