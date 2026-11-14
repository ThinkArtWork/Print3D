"""Subprocess entry point that runs one model script and measures the result.

This file is executed as a plain script (not imported) by :mod:`xprint.cad`::

    python -I -B cad_worker.py model.py /path/to/workdir [--input part.step]

It imports the user's script, calls ``build_part()``, writes ``model.stl`` and
``model.step`` into the work directory, and drops ``result.json`` there for the
parent process.  An optional ``--input`` STEP file is exposed to the script as
the string globals ``INPUT_STEP`` (absolute path) and ``INPUT_NAME``
(basename); they are plain strings so the script cannot walk the filesystem
with them, and ``open`` is already banned by the script policy.  Keeping it dependency-light on purpose: the worker must run in
whatever interpreter has ``build123d`` installed, which is not necessarily the
interpreter running the MCP server.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import struct
import sys
import traceback
from pathlib import Path
from typing import Any

TRACE_LIMIT = 4000
MAX_STL_TRIANGLES = 400_000


def _load_module(path: Path, input_step: Path | None = None) -> Any:
    spec = importlib.util.spec_from_file_location("xprint_user_model", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Set the globals before executing the script so module-level code sees them.
    module.__dict__["INPUT_STEP"] = str(input_step) if input_step else ""
    module.__dict__["INPUT_NAME"] = input_step.name if input_step else ""
    sys.modules["xprint_user_model"] = module
    spec.loader.exec_module(module)
    return module


def _stl_mesh_stats(path: Path) -> dict[str, Any] | None:
    """Watertightness from the exported binary STL, without extra dependencies.

    A printable mesh has every edge shared by exactly two triangles.  We quantise
    vertices to 0.1 µm before counting, which removes float noise from the CAD
    kernel without merging distinct features.
    """

    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 84:
        return None
    triangles = struct.unpack_from("<I", data, 80)[0]
    if triangles == 0 or triangles > MAX_STL_TRIANGLES:
        return None
    if len(data) != 84 + triangles * 50:  # ASCII STL or truncated file
        return None

    edges: dict[tuple[tuple[float, float, float], tuple[float, float, float]], int] = {}
    for index in range(triangles):
        base = 84 + index * 50 + 12
        points = []
        for corner in range(3):
            x, y, z = struct.unpack_from("<3f", data, base + corner * 12)
            points.append((round(x, 4), round(y, 4), round(z, 4)))
        for first, second in ((0, 1), (1, 2), (2, 0)):
            if points[first] <= points[second]:
                edge = (points[first], points[second])
            else:
                edge = (points[second], points[first])
            edges[edge] = edges.get(edge, 0) + 1

    boundary = sum(1 for count in edges.values() if count == 1)
    nonmanifold = sum(1 for count in edges.values() if count > 2)
    return {
        "triangles": triangles,
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "watertight": boundary == 0 and nonmanifold == 0,
    }


def _num(value: Any) -> float | None:
    try:
        # Add 0.0 so a rounded -0.0 becomes 0.0: JSON cannot round-trip the
        # sign of negative zero losslessly.
        return round(float(value), 3) + 0.0
    except (TypeError, ValueError):
        return None


def _measure(part: Any) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    bbox = None
    try:
        bbox = part.bounding_box()
        size = bbox.size
        checks["bbox_mm"] = [_num(size.X), _num(size.Y), _num(size.Z)]
        checks["bbox_min_mm"] = [_num(bbox.min.X), _num(bbox.min.Y), _num(bbox.min.Z)]
        checks["bbox_max_mm"] = [_num(bbox.max.X), _num(bbox.max.Y), _num(bbox.max.Z)]
    except Exception as exc:  # pragma: no cover - defensive against kernel quirks
        checks["bbox_error"] = f"{type(exc).__name__}: {exc}"

    try:
        checks["volume_mm3"] = _num(part.volume)
        if checks["volume_mm3"] is not None:
            checks["volume_cm3"] = _num(checks["volume_mm3"] / 1000.0)
    except Exception as exc:  # pragma: no cover
        checks["volume_error"] = f"{type(exc).__name__}: {exc}"

    try:
        checks["solids"] = len(part.solids())
        checks["faces"] = len(part.faces())
    except Exception as exc:  # pragma: no cover
        checks["topology_error"] = f"{type(exc).__name__}: {exc}"

    try:
        is_valid = part.is_valid
        checks["is_valid"] = bool(is_valid() if callable(is_valid) else is_valid)
    except Exception:  # pragma: no cover
        checks["is_valid"] = None

    return checks


def _run(script: Path, outdir: Path, input_step: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "checks": {},
        "artifacts": {},
        "error": None,
        "script": str(script),
        "input": str(input_step) if input_step else "",
    }
    sys.path.insert(0, str(script.parent))
    try:
        module = _load_module(script, input_step)
        build_part = getattr(module, "build_part", None)
        if not callable(build_part):
            raise RuntimeError("model.py must define build_part()")
        part = build_part()
        if part is None:
            raise RuntimeError("build_part() returned None")

        from build123d import export_step, export_stl

        result["checks"] = _measure(part)

        stl_path = outdir / "model.stl"
        export_stl(part, str(stl_path))
        artifacts = {"stl": str(stl_path)}
        try:
            step_path = outdir / "model.step"
            export_step(part, str(step_path))
            artifacts["step"] = str(step_path)
        except Exception as exc:  # STEP is nice-to-have; STL is what prints
            result["step_warning"] = f"{type(exc).__name__}: {exc}"

        mesh = _stl_mesh_stats(stl_path)
        if mesh:
            result["checks"].update(mesh)
        result["artifacts"] = artifacts
        result["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - report everything to the caller
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-TRACE_LIMIT:]
    return result


def _apply_limits(cpu_seconds: int) -> None:
    """Bound CPU time, file size and descriptors before importing user code."""

    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return
    for name, value in (
        ("RLIMIT_CPU", (cpu_seconds, cpu_seconds + 5)),
        ("RLIMIT_CORE", (0, 0)),
        ("RLIMIT_NOFILE", (256, 256)),
        ("RLIMIT_FSIZE", (512 * 1024 * 1024, 512 * 1024 * 1024)),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(limit, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xprint-cad-worker")
    parser.add_argument("script")
    parser.add_argument("outdir")
    parser.add_argument("--cpu-seconds", type=int, default=0)
    parser.add_argument("--input", default="", help="STEP file exposed as INPUT_STEP")
    args = parser.parse_args(argv)

    if args.cpu_seconds > 0:
        _apply_limits(args.cpu_seconds)

    script = Path(args.script).resolve()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    input_step = Path(args.input).resolve() if args.input else None
    if input_step is not None and not input_step.is_file():
        (outdir / "result.json").write_text(
            json.dumps({"ok": False, "error": f"input STEP not found: {input_step}"}),
            encoding="utf-8",
        )
        return 1

    result = _run(script, outdir, input_step)
    (outdir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
