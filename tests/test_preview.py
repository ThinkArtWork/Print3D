"""Tests for the numpy/Pillow multi-view preview renderer."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image

from print3d.preview import DEFAULT_VIEWS, load_stl, render_views

CUBE = [
    ((0, 0, 0), (20, 0, 0), (20, 20, 0)),
    ((0, 0, 0), (20, 20, 0), (0, 20, 0)),
    ((0, 0, 20), (20, 20, 20), (20, 0, 20)),
    ((0, 0, 20), (0, 20, 20), (20, 20, 20)),
    ((0, 0, 0), (0, 20, 0), (0, 20, 20)),
    ((0, 0, 0), (0, 20, 20), (0, 0, 20)),
    ((20, 0, 0), (20, 20, 20), (20, 20, 0)),
    ((20, 0, 0), (20, 0, 20), (20, 20, 20)),
    ((0, 0, 0), (20, 0, 20), (20, 0, 20)),
    ((0, 0, 0), (20, 0, 20), (0, 0, 20)),
    ((0, 20, 0), (20, 20, 0), (20, 20, 20)),
    ((0, 20, 0), (20, 20, 20), (0, 20, 20)),
]


def ascii_stl(triangles) -> bytes:
    lines = ["solid test"]
    for a, b, c in triangles:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append("      vertex {:.6f} {:.6f} {:.6f}".format(*vertex))
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    return ("\n".join(lines) + "\n").encode()


def binary_stl(triangles) -> bytes:
    header = b"print3d test".ljust(80, b"\0")
    body = struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        body += struct.pack("<3f", 0, 0, 0)
        for vertex in (a, b, c):
            body += struct.pack("<3f", *vertex)
        body += struct.pack("<H", 0)
    return header + body


def png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def orange_pixels(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as image:
        pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    # The plastic colour is around (232, 120, 66); anything warm and saturated counts.
    return int(
        np.count_nonzero((pixels[..., 0] > 150) & (pixels[..., 1] < 170) & (pixels[..., 2] < 140))
    )


def test_load_stl_reads_ascii(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(ascii_stl(CUBE))
    triangles = load_stl(path)
    assert triangles.shape == (12, 3, 3)
    assert triangles.min() == 0
    assert triangles.max() == 20


def test_load_stl_reads_binary(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(binary_stl(CUBE))
    triangles = load_stl(path)
    assert triangles.shape == (12, 3, 3)
    assert triangles.max() == 20


def test_load_stl_rejects_empty_files(tmp_path) -> None:
    path = tmp_path / "empty.stl"
    path.write_bytes(b"solid nothing\nendsolid nothing\n")
    with pytest.raises(ValueError):
        load_stl(path)


def test_render_views_returns_a_labelled_png(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(binary_stl(CUBE))

    data = render_views(path, caption="cube 20 mm")

    assert png_size(data) == (900, 700)
    assert orange_pixels(data) > 20_000


def test_every_quadrant_has_geometry(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(binary_stl(CUBE))

    data = render_views(path, views=DEFAULT_VIEWS)
    with Image.open(io.BytesIO(data)) as image:
        pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    height, width, _ = pixels.shape
    mask = (pixels[..., 0] > 150) & (pixels[..., 1] < 170) & (pixels[..., 2] < 140)
    quadrants = [
        mask[: height // 2, : width // 2],
        mask[: height // 2, width // 2 :],
        mask[height // 2 :, : width // 2],
        mask[height // 2 :, width // 2 :],
    ]
    assert all(int(part.sum()) > 2_000 for part in quadrants)


def test_single_view_layout(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(binary_stl(CUBE))
    data = render_views(path, views=("front",), width=400, height=300)
    assert png_size(data) == (400, 300)


def test_unknown_view_is_rejected(tmp_path) -> None:
    path = tmp_path / "cube.stl"
    path.write_bytes(binary_stl(CUBE))
    with pytest.raises(ValueError, match="unknown view"):
        render_views(path, views=("inside-out",))


def test_zero_size_model_is_rejected(tmp_path) -> None:
    path = tmp_path / "flat.stl"
    path.write_bytes(binary_stl([((0, 0, 0), (0, 0, 0), (0, 0, 0))]))
    with pytest.raises(ValueError, match="zero size"):
        render_views(path)
