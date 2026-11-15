#!/usr/bin/env python3
"""Project a STEP file into a third-angle SVG + DXF sheet (child process).

Invoked by :func:`print3d.drawing.run_drawing` as::

    python -I -B drawing_worker.py model.step outdir --views front,top,right,iso
        --scale 1.0 [--no-hidden] [--no-labels] [--cpu-seconds 65]

Like ``cad_worker.py`` this is a single file importing nothing from print3d: it
runs in a scrubbed interpreter with rlimits, reads one STEP file and writes
``result.json``, ``drawing.svg`` and ``drawing.dxf`` into *outdir*.

Layout is third-angle: the front view at the origin, the top view above it, the
right view to its right (front face adjacent), the isometric view at the top
right.  Hidden lines are dashed and belong to the orthographic views; the
isometric view is drawn without them.  Projections are 1:1 by default, so a printed sheet measures real
millimetres.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# (look_from, look_up) per view, chosen so every projection is "straight up".
VIEW_ANGLES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}
VIEW_ORDER = ("front", "top", "right", "iso")

GAP_MM = 14.0
LABEL_GAP_MM = 3.0
LABEL_SIZE_MM = 3.5
MARGIN_MM = 6.0
FILES = ("drawing.svg", "drawing.dxf")


def _apply_limits(cpu_seconds: int) -> None:
    """Cap the damage a pathological shape can do (mirrors cad_worker).

    Windows has no ``resource`` module, so this is a no-op there: the parent's
    wall-clock timeout and the job-object kill are the only bounds, which is
    exactly what ``capabilities`` reports.
    """

    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return
    for limit, value in (
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
    ):
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(limit, (value, value))


def _force_utf8_stdio() -> None:
    """Decode our own output as UTF-8 whatever the console code page says.

    The worker runs with ``-I``, which makes the interpreter ignore
    ``PYTHONIOENCODING``; the parent decodes the pipe as UTF-8, so the child has
    to agree or a non-ASCII path in a progress line becomes mojibake.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def _layout(sizes: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """Return the (left, bottom) of each view in mm, third-angle rules."""

    placed: dict[str, tuple[float, float]] = {}
    anchor = next((name for name in ("front", "right", "top", "iso") if name in sizes), None)
    if anchor is None:
        return placed
    placed[anchor] = (0.0, 0.0)

    if anchor == "front":
        if "top" in sizes:
            placed["top"] = (
                (sizes["front"][0] - sizes["top"][0]) / 2,
                sizes["front"][1] + GAP_MM,
            )
        if "right" in sizes:
            placed["right"] = (
                sizes["front"][0] + GAP_MM,
                sizes["front"][1] - sizes["right"][1],
            )
    elif anchor == "top" and "right" in sizes:
        placed["right"] = (sizes["top"][0] + GAP_MM, 0.0)

    if "iso" in sizes and anchor != "iso":
        if "top" in placed:
            placed["iso"] = (
                placed["top"][0] + sizes["top"][0] + GAP_MM,
                placed["top"][1],
            )
        elif "right" in placed:
            placed["iso"] = (placed["right"][0], placed["right"][1] + sizes["right"][1] + GAP_MM)
        else:
            placed["iso"] = (
                placed[anchor][0] + sizes[anchor][0] + GAP_MM,
                placed[anchor][1],
            )
    return placed


def _edge_count(shape: Any) -> int:
    try:
        return len(shape.edges())
    except Exception:  # noqa: BLE001 - an empty HLR compound raises in OCP
        return 0


def _bounds(shapes: list[Any]) -> tuple[float, float, float, float]:
    boxes = [shape.bounding_box() for shape in shapes]
    return (
        min(box.min.X for box in boxes),
        min(box.min.Y for box in boxes),
        max(box.max.X for box in boxes),
        max(box.max.Y for box in boxes),
    )


def _run(
    step: Path,
    outdir: Path,
    *,
    views: list[str],
    scale: float,
    hidden: bool,
    labels: bool,
) -> dict[str, Any]:
    from build123d import Location, Text, import_step
    from build123d.exporters import Drawing, ExportDXF, ExportSVG, LineType, Unit

    shape = import_step(str(step))
    if scale != 1.0:
        shape = shape.scale(scale)

    warnings: list[str] = []
    visible_views: dict[str, Any] = {}
    hidden_views: dict[str, Any] = {}
    sizes: dict[str, tuple[float, float]] = {}
    for name in views:
        look_from, look_up = VIEW_ANGLES[name]
        # An isometric view is read as a picture, so its hidden lines only bury
        # the shape: the engineering convention is to omit them.
        wants_hidden = hidden and name != "iso"
        drawing = Drawing(shape, look_from=look_from, look_up=look_up, with_hidden=wants_hidden)
        visible = drawing.visible_lines
        if not _edge_count(visible):
            warnings.append(f"{name}: no visible edges, view skipped")
            continue
        box = visible.bounding_box()
        visible_views[name] = visible
        if wants_hidden:
            hidden_lines = drawing.hidden_lines
            if _edge_count(hidden_lines):
                hidden_views[name] = hidden_lines
        sizes[name] = (box.size.X, box.size.Y)
    if not visible_views:
        raise RuntimeError("the STEP file produced no visible edges in the requested views")

    placed = _layout(sizes)
    moved: list[Any] = []
    visible_out: dict[str, Any] = {}
    hidden_out: dict[str, Any] = {}
    for name, visible in visible_views.items():
        box = visible.bounding_box()
        here = placed.get(name, (0.0, 0.0))
        shift = (here[0] - box.min.X, here[1] - box.min.Y, 0.0)
        visible_out[name] = visible.moved(Location(shift))
        moved.append(visible_out[name])
        if name in hidden_views:
            hidden_out[name] = hidden_views[name].moved(Location(shift))
            moved.append(hidden_out[name])

    label_out: dict[str, Any] = {}
    labels_rendered = False
    if labels:
        for name, visible in visible_out.items():
            try:
                text = Text(name.upper(), font_size=LABEL_SIZE_MM)
            except Exception as exc:  # noqa: BLE001 - fontconfig may be missing
                warnings.append(f"labels disabled: {type(exc).__name__}: {exc}")
                label_out.clear()
                break
            tbox = text.bounding_box()
            vbox = visible.bounding_box()
            target = (
                (vbox.min.X + vbox.max.X) / 2 - (tbox.min.X + tbox.max.X) / 2,
                vbox.min.Y - LABEL_GAP_MM - tbox.max.Y,
            )
            label_out[name] = text.moved(Location((target[0], target[1], 0)))
            moved.append(label_out[name])
        labels_rendered = bool(label_out)

    if moved:
        min_x, min_y, _, _ = _bounds(moved)
        normalize = Location((MARGIN_MM - min_x, MARGIN_MM - min_y, 0.0))
        visible_out = {name: shape.moved(normalize) for name, shape in visible_out.items()}
        hidden_out = {name: shape.moved(normalize) for name, shape in hidden_out.items()}
        label_out = {name: shape.moved(normalize) for name, shape in label_out.items()}

    sheet = (0.0, 0.0)
    everything = [*visible_out.values(), *hidden_out.values(), *label_out.values()]
    if everything:
        min_x, min_y, max_x, max_y = _bounds(everything)
        sheet = (max_x - min_x, max_y - min_y)

    svg = ExportSVG(unit=Unit.MM, margin=0.0, line_weight=0.35)
    svg.add_layer("visible", line_weight=0.35)
    svg.add_layer("hidden", line_weight=0.18, line_type=LineType.DASHED)
    svg.add_layer("labels", line_weight=0.25, fill_color="black")
    for view in visible_out.values():
        svg.add_shape(view, layer="visible")
    for view in hidden_out.values():
        svg.add_shape(view, layer="hidden")
    for text in label_out.values():
        svg.add_shape(text, layer="labels")
    svg_path = outdir / "drawing.svg"
    svg.write(str(svg_path))

    dxf = ExportDXF(unit=Unit.MM)
    dxf.add_layer("visible")
    dxf.add_layer("hidden", line_type=LineType.DASHED)
    dxf.add_layer("labels")
    for view in visible_out.values():
        dxf.add_shape(view, layer="visible")
    for view in hidden_out.values():
        dxf.add_shape(view, layer="hidden")
    for text in label_out.values():
        dxf.add_shape(text, layer="labels")
    dxf_path = outdir / "drawing.dxf"
    dxf.write(str(dxf_path))

    report: list[dict[str, Any]] = []
    for name in visible_out:
        vbox = visible_out[name].bounding_box()
        report.append(
            {
                "name": name,
                "visible_edges": _edge_count(visible_out[name]),
                "hidden_edges": _edge_count(hidden_out[name]) if name in hidden_out else 0,
                "width_mm": round(vbox.size.X, 2),
                "height_mm": round(vbox.size.Y, 2),
                "at_mm": [round(vbox.min.X, 2), round(vbox.min.Y, 2)],
            }
        )
    checks: dict[str, Any] = {
        "source_step": step.name,
        "scale": scale,
        "hidden": hidden,
        "labels": labels_rendered,
        "sheet_mm": [round(sheet[0], 2), round(sheet[1], 2)],
        "views": report,
        "edges": {
            "visible": sum(item["visible_edges"] for item in report),
            "hidden": sum(item["hidden_edges"] for item in report),
            "labels": sum(_edge_count(text) for text in label_out.values()),
        },
    }
    files = {name: str(outdir / name) for name in FILES}
    return {
        "ok": True,
        "checks": checks,
        "files": files,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Render a STEP file as an SVG/DXF sheet.")
    parser.add_argument("step")
    parser.add_argument("outdir")
    parser.add_argument("--views", default="front,top,right,iso")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--no-hidden", action="store_true")
    parser.add_argument("--no-labels", action="store_true")
    parser.add_argument("--cpu-seconds", type=int, default=65)
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    result_path = outdir / "result.json"
    _apply_limits(max(1, args.cpu_seconds))

    wanted = [item.strip().lower() for item in args.views.split(",") if item.strip()]
    result: dict[str, Any]
    try:
        unknown = [item for item in wanted if item not in VIEW_ANGLES]
        if unknown:
            raise ValueError(f"unknown views {unknown}; pick from {sorted(VIEW_ANGLES)}")
        if not wanted:
            raise ValueError("no views requested")
        if not 0.05 <= args.scale <= 20.0:
            raise ValueError("scale must be between 0.05 and 20")
        result = _run(
            Path(args.step),
            outdir,
            views=[name for name in VIEW_ORDER if name in wanted],
            scale=args.scale,
            hidden=not args.no_hidden,
            labels=not args.no_labels,
        )
    except Exception as exc:  # noqa: BLE001 - the parent reads this JSON, not a traceback
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", "utf-8")
    if not result.get("ok"):
        print(result.get("error") or "drawing failed", file=sys.stderr)
        return 1
    checks = result.get("checks", {})
    print(
        "sheet {w}x{h} mm, views {v}, edges {e}".format(
            w=checks.get("sheet_mm", [0, 0])[0],
            h=checks.get("sheet_mm", [0, 0])[1],
            v=",".join(item["name"] for item in checks.get("views", [])),
            e=checks.get("edges", {}).get("visible", 0),
        )
    )
    for warning in result.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
