"""Render a built STL to a labelled multi-view PNG.

The closed loop needs *eyes*: after ``model_build`` the client model should see
what the geometry actually looks like.  Blender is not a dependency of the core,
so this module ships a small software renderer: orthographic projection, a
z-buffer and flat Lambert shading, done with numpy and Pillow.

The output is one image with up to four labelled views (iso, front, top, right)
so a single tool result is enough to judge proportions and features.
"""

from __future__ import annotations

import io
import re
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_VIEWS = ("iso", "front", "top", "right")
VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "iso": (1.0, -1.0, 0.8),
    "front": (0.0, -1.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "right": (1.0, 0.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "back": (0.0, 1.0, 0.0),
}
MAX_RENDER_TRIANGLES = 150_000

BACKGROUND = (246, 247, 249)
INK = (72, 78, 90)
LABEL = (96, 104, 118)
PLASTIC = np.array([232.0, 120.0, 66.0])  # warm filament orange


def load_stl(path: str | Path) -> np.ndarray:
    """Load a binary or ASCII STL as an ``(n, 3, 3)`` float array of triangles."""

    path = Path(path)
    data = path.read_bytes()
    triangles = _parse_binary_stl(data)
    if triangles is None:
        triangles = _parse_ascii_stl(data)
    if triangles is None or not len(triangles):
        raise ValueError(f"{path.name} contains no triangles")
    return triangles


def _parse_binary_stl(data: bytes) -> np.ndarray | None:
    if len(data) < 84:
        return None
    count = struct.unpack_from("<I", data, 80)[0]
    if count == 0 or 84 + count * 50 > len(data):
        return None
    raw = np.frombuffer(data, dtype=np.uint8, offset=84, count=count * 50)
    values = raw.reshape(count, 50)[:, 12:48].copy().view("<f4")
    triangles = values.reshape(count, 3, 3).astype(np.float64)
    return triangles if np.isfinite(triangles).all() else None


_FLOAT_RE = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _parse_ascii_stl(data: bytes) -> np.ndarray | None:
    if not data.lstrip().lower().startswith(b"solid"):
        return None
    values = np.array([float(item) for item in _FLOAT_RE.findall(data)], dtype=np.float64)
    if values.size < 9 * 3:
        return None
    # Every facet contributes 12 floats (normal + 3 vertices); keep the vertices.
    facets = values.size // 12
    vertices = values[: facets * 12].reshape(facets, 12)[:, 3:].reshape(facets, 3, 3)
    return vertices


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _basis(direction: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward = _unit(np.array(direction, dtype=np.float64))
    up_hint = np.array([0.0, 0.0, 1.0])
    if abs(float(forward @ up_hint)) > 0.95:
        up_hint = np.array([0.0, 1.0, 0.0])
    right = _unit(np.cross(up_hint, forward))
    up = _unit(np.cross(forward, right))
    return right, up, forward


def _render_view(
    triangles: np.ndarray,
    center: np.ndarray,
    scale: float,
    direction: tuple[float, float, float],
    size: tuple[int, int],
) -> Image.Image:
    width, height = size
    right, up, forward = _basis(direction)
    rel = triangles - center
    camera = np.stack([rel @ right, rel @ up, rel @ forward], axis=-1)

    sx = camera[..., 0] * scale + width / 2.0
    sy = height / 2.0 - camera[..., 1] * scale
    depth = camera[..., 2]

    depth_buffer = np.full((height, width), -np.inf, dtype=np.float64)
    shade = np.zeros((height, width), dtype=np.float64)
    covered = np.zeros((height, width), dtype=bool)

    normals = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    light = _unit(np.array([-0.35, 0.55, 0.75]) @ np.stack([right, up, forward]))
    lengths = np.linalg.norm(normals, axis=1)
    safe = np.where(lengths == 0, 1.0, lengths)
    facing = np.abs((normals / safe[:, None]) @ light)
    brightness = 0.38 + 0.62 * facing

    for index in range(len(triangles)):
        x0, x1, x2 = sx[index]
        y0, y1, y2 = sy[index]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue
        min_x = max(int(np.floor(min(x0, x1, x2))), 0)
        max_x = min(int(np.ceil(max(x0, x1, x2))), width - 1)
        min_y = max(int(np.floor(min(y0, y1, y2))), 0)
        max_y = min(int(np.ceil(max(y0, y1, y2))), height - 1)
        if min_x > max_x or min_y > max_y:
            continue
        xs = np.arange(min_x, max_x + 1) + 0.5
        ys = np.arange(min_y, max_y + 1) + 0.5
        grid_x, grid_y = np.meshgrid(xs, ys)
        w0 = ((x1 - grid_x) * (y2 - grid_y) - (x2 - grid_x) * (y1 - grid_y)) / area
        w1 = ((x2 - grid_x) * (y0 - grid_y) - (x0 - grid_x) * (y2 - grid_y)) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        if not inside.any():
            continue
        z = w0 * depth[index, 0] + w1 * depth[index, 1] + w2 * depth[index, 2]
        window = depth_buffer[min_y : max_y + 1, min_x : max_x + 1]
        update = inside & (z > window)
        if not update.any():
            continue
        window[update] = z[update]
        shade[min_y : max_y + 1, min_x : max_x + 1][update] = brightness[index]
        covered[min_y : max_y + 1, min_x : max_x + 1] |= update

    pixels = np.empty((height, width, 3), dtype=np.float64)
    pixels[:] = BACKGROUND
    mask = covered
    pixels[mask] = PLASTIC[None, :] * shade[mask][:, None]
    # A one-pixel silhouette darkens the edge just enough to read the shape.
    edge = mask & ~_erode(mask)
    pixels[edge] = pixels[edge] * 0.55 + np.array(INK) * 0.45
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), mode="RGB")


def _erode(mask: np.ndarray) -> np.ndarray:
    eroded = mask.copy()
    eroded[1:, :] &= mask[:-1, :]
    eroded[:-1, :] &= mask[1:, :]
    eroded[:, 1:] &= mask[:, :-1]
    eroded[:, :-1] &= mask[:, 1:]
    return eroded


def _layout(views: tuple[str, ...]) -> tuple[int, int]:
    if len(views) <= 1:
        return 1, 1
    if len(views) == 2:
        return 1, 2
    return 2, 2


def render_views(
    path: str | Path,
    *,
    views: tuple[str, ...] = DEFAULT_VIEWS,
    width: int = 900,
    height: int = 700,
    supersample: int = 2,
    caption: str | None = None,
) -> bytes:
    """Render ``path`` (an STL) to PNG bytes with labelled orthogonal views."""

    triangles = load_stl(path)
    if len(triangles) > MAX_RENDER_TRIANGLES:
        step = int(np.ceil(len(triangles) / MAX_RENDER_TRIANGLES))
        triangles = triangles[::step]

    low = triangles.reshape(-1, 3).min(axis=0)
    high = triangles.reshape(-1, 3).max(axis=0)
    center = (low + high) / 2.0
    extent = float(np.max(high - low))
    if extent <= 0:
        raise ValueError("the model has zero size")

    views = tuple(views) or DEFAULT_VIEWS
    unknown = [view for view in views if view not in VIEW_DIRECTIONS]
    if unknown:
        raise ValueError(f"unknown view(s): {', '.join(unknown)}")

    columns, rows = _layout(views)
    tile_w = max(width // columns, 160)
    tile_h = max(height // rows, 120)
    margin = max(10, int(min(tile_w, tile_h) * 0.06))
    scale = min(tile_w - 2 * margin, tile_h - 2 * margin) * supersample / extent

    canvas = Image.new("RGB", (tile_w * columns, tile_h * rows), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    for position, view in enumerate(views):
        tile = _render_view(
            triangles,
            center,
            scale,
            VIEW_DIRECTIONS[view],
            (tile_w * supersample, tile_h * supersample),
        )
        if supersample > 1:
            tile = tile.resize((tile_w, tile_h), Image.LANCZOS)
        column = position % columns
        row = position // columns
        canvas.paste(tile, (column * tile_w, row * tile_h))
        draw.text(
            (column * tile_w + margin // 2, row * tile_h + tile_h - margin // 2 - 8),
            view.upper(),
            fill=LABEL,
        )

    if caption:
        draw.text((margin // 2, 6), caption, fill=LABEL)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


__all__ = [
    "DEFAULT_VIEWS",
    "MAX_RENDER_TRIANGLES",
    "VIEW_DIRECTIONS",
    "load_stl",
    "render_views",
]
