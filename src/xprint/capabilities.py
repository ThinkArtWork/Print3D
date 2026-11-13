"""Capability discovery.

``capabilities`` tells an agent what this machine can actually do — it must
never claim an adapter works when the tool is missing.  Each unavailable entry
carries a ``reason`` and a ``hint``, so the agent can tell the user what to
install instead of guessing.
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import LoadedConfig, Secrets
from .jobs import JobStore
from .registry import Registry
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
        adapter = str(entry.get("adapter", provider_id))
        cloud.append(
            {
                "id": provider_id,
                "adapter": adapter,
                "base_url": entry.get("base_url"),
                "has_key": secrets.has_key(provider_id, api_key_env=entry.get("api_key_env")),
                "key_source": secrets.key_source(provider_id, api_key_env=entry.get("api_key_env")),
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
        "usable": [item["id"] for item in cloud if item["has_key"]],
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


def probe_cad(cfg: LoadedConfig) -> dict[str, Any]:
    backend = cfg.config.cad.backend
    if backend != "text2cad":
        return {
            "backend": backend,
            "available": False,
            "reason": f"CAD backend {backend!r} has no implementation yet",
            "hint": "Set cad.backend to 'text2cad'.",
        }
    cad_cli = _which("cad")
    return {
        "backend": backend,
        "available": cad_cli is not None,
        "executable": cad_cli,
        "expected_version": cfg.config.cad.cad_server_version,
        "reason": None if cad_cli else "The `cad` CLI (text-to-cad) is not on PATH",
        "hint": None
        if cad_cli
        else "Install text-to-cad in its own environment: `pip install cad-server` "
        "(see docs/ASSETS.md); XPrint only calls it as an external process.",
    }


def probe_render(cfg: LoadedConfig) -> dict[str, Any]:
    backend = cfg.config.render.backend
    blender_path = cfg.config.render.blender_path
    bpy_spec = importlib.util.find_spec("bpy") is not None

    if backend == "blender_bpy":
        cad_cli = _which("cad")
        available = bpy_spec or cad_cli is not None
        reason = None
        if not available:
            reason = "bpy is not importable and the `cad` CLI is missing"
        return {
            "backend": backend,
            "available": available,
            "bpy_importable": bpy_spec,
            "cad_cli": cad_cli,
            "reason": reason,
            "hint": None
            if available
            else "Install text-to-cad (provides bpy) or set render.backend to "
            "'blender_app' with render.blender_path.",
        }
    if backend == "blender_app":
        path = blender_path or _which("blender")
        available = bool(path) and _exists(path)
        return {
            "backend": backend,
            "available": available,
            "executable": path,
            "reason": None if available else "Blender application not found",
            "hint": None
            if available
            else "Set render.blender_path to the Blender executable, or install Blender.",
        }
    return {
        "backend": backend,
        "available": False,
        "reason": f"Render backend {backend!r} has no implementation yet",
        "hint": "Use 'blender_bpy' or 'blender_app'.",
    }


def probe_slicers(cfg: LoadedConfig, registry: Registry) -> list[dict[str, Any]]:
    slicers = []
    for target_name, target in sorted(cfg.config.targets.items()):
        adapter_known = registry.has("slicer", target.slicer)
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
                "adapter_known": registry.has("printer", printer.adapter),
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
        "summary": {
            "cloud_providers_usable": len(providers["usable"]),
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
        "warnings": loaded.warnings,
    }
