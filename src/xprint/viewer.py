"""Self-contained interactive 3D viewer for built revisions.

The output is a single HTML file with the geometry embedded as base64 float
arrays, so it works from ``file://`` without a server, CDN or any third-party
JavaScript. It is a viewing aid only: all modelling and printing stays behind
the MCP tools.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import numpy as np

from .preview import load_stl

MAX_VIEW_TRIANGLES = 400_000
VIEWER_FILE = "viewer.html"
_TEMPLATE = Path(__file__).with_name("viewer_template.html").read_text(encoding="utf-8")


def _encode(values: np.ndarray) -> str:
    array = np.ascontiguousarray(values, dtype="<f4")
    return base64.b64encode(array.tobytes()).decode("ascii")


def _face_normals(triangles: np.ndarray) -> np.ndarray:
    edges1 = triangles[:, 1] - triangles[:, 0]
    edges2 = triangles[:, 2] - triangles[:, 0]
    normals = np.cross(edges1, edges2)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    safe = np.where(lengths > 1e-12, lengths, 1.0)
    normals = normals / safe
    degenerate = (lengths <= 1e-12).reshape(-1)
    if degenerate.any():
        normals[degenerate] = (0.0, 0.0, 1.0)
    return normals


def _mesh_volume(triangles: np.ndarray) -> float:
    """Signed-tetrahedron volume, matching the build checks' convention."""

    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    return float(abs(np.sum(a * np.cross(b, c))) / 6.0)


def viewer_html(
    triangles: np.ndarray,
    *,
    title: str,
    subtitle: str,
    revision: str,
    name: str = "",
    volume_mm3: float | None = None,
) -> str:
    """Render one revision to a standalone HTML document."""

    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or not len(triangles):
        raise ValueError("the mesh contains no triangles")
    if len(triangles) > MAX_VIEW_TRIANGLES:
        raise ValueError(
            f"the mesh has {len(triangles):,} triangles; the interactive viewer "
            f"supports at most {MAX_VIEW_TRIANGLES:,}"
        )

    vertices = triangles.reshape(-1, 3)
    low = vertices.min(axis=0)
    high = vertices.max(axis=0)
    extent = np.maximum(high - low, 1e-9)
    center = (low + high) / 2.0
    normals = np.repeat(_face_normals(triangles), 3, axis=0)
    volume = float(volume_mm3) if volume_mm3 is not None else _mesh_volume(triangles)
    meta = {
        "name": name or title,
        "revision": revision,
        "triangles": int(len(triangles)),
        "bbox_mm": [round(float(value), 3) for value in extent],
        "center_mm": [round(float(value), 4) for value in center],
        "volume_cm3": round(volume / 1000.0, 3),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
    }

    replacements = {
        "__TITLE__": escape(title),
        "__SUBTITLE__": escape(subtitle),
        "__META_JSON__": json.dumps(meta, ensure_ascii=False).replace("<", "\\u003c"),
        "__POSITIONS__": _encode(triangles.reshape(-1)),
        "__NORMALS__": _encode(normals.reshape(-1)),
    }
    html = _TEMPLATE
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def write_viewer(
    stl_path: str | Path,
    *,
    out_path: str | Path | None = None,
    title: str,
    subtitle: str,
    revision: str,
    name: str = "",
    volume_mm3: float | None = None,
) -> dict:
    """Write the viewer next to the STL (or to ``out_path``) and describe it."""

    stl_path = Path(stl_path)
    triangles = load_stl(stl_path)
    html = viewer_html(
        triangles,
        title=title,
        subtitle=subtitle,
        revision=revision,
        name=name,
        volume_mm3=volume_mm3,
    )
    target = Path(out_path) if out_path else stl_path.with_name(VIEWER_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    target.chmod(0o644)
    return {
        "path": target,
        "bytes": len(html.encode("utf-8")),
        "triangles": int(len(triangles)),
    }
