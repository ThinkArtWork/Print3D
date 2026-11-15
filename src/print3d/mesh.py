"""Mesh inspection shared by Blender exports and local generators.

Both paths produce a triangle soup (STL); the checks below are the same ones
the build123d worker reports so a client model can compare results no matter
which engine produced them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .preview import load_stl


def _num(value) -> float:
    """Round a float and normalise -0.0, which JSON cannot round-trip."""
    return round(float(value), 3) + 0.0


def mesh_stats(triangles: np.ndarray) -> dict[str, Any]:
    """Summarise a triangle soup: bbox, volume, area and watertightness.

    Zero-area faces are counted separately and left out of the edge tally: they
    are a tessellation artefact where a fillet meets its neighbours, and their
    collapsed edges would otherwise read as a hole.  ``mesh_stats`` and the
    build123d worker report the same numbers on purpose.
    """

    tris = np.asarray(triangles, dtype=np.float64)
    if tris.ndim != 3 or tris.shape[1] != 3 or tris.shape[2] != 3:
        raise ValueError("triangles must have shape (n, 3, 3)")
    if not tris.size:
        raise ValueError("mesh contains no triangles")
    if not np.isfinite(tris).all():
        raise ValueError("mesh contains NaN or infinite coordinates")

    vertices = tris.reshape(-1, 3)
    bbox_min = vertices.min(axis=0)
    bbox_max = vertices.max(axis=0)
    size = bbox_max - bbox_min

    # Signed volume of the tetrahedra spanned by each triangle and the origin.
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    volume = abs(float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum()) / 6.0)
    area = float(np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1).sum() / 2.0)

    # Manifoldness: merge vertices at 0.1 µm, count undirected edge uses.
    quantised = np.round(vertices, 4)
    _, inverse = np.unique(quantised, axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3).astype(np.int64)
    # Drop faces whose vertices collapse onto fewer than three points; keeping
    # them would add phantom boundary or non-manifold edges.
    distinct = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
    )
    degenerate = int((~distinct).sum())
    faces = faces[distinct]
    if faces.size:
        edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0)
        edges = np.sort(edges, axis=1)
        n_vertices = int(inverse.max()) + 1 if inverse.size else 0
        keys = edges[:, 0] * max(n_vertices, 1) + edges[:, 1]
        _, counts = np.unique(keys, return_counts=True)
        boundary = int((counts == 1).sum())
        nonmanifold = int((counts > 2).sum())
    else:
        boundary = nonmanifold = 0

    return {
        "triangles": int(len(tris)),
        "vertices": int(len(np.unique(quantised, axis=0))),
        "bbox_mm": [_num(value) for value in size],
        "bbox_min_mm": [_num(value) for value in bbox_min],
        "bbox_max_mm": [_num(value) for value in bbox_max],
        "volume_mm3": _num(volume),
        "volume_cm3": _num(volume / 1000.0),
        "area_mm2": _num(area),
        "degenerate_triangles": degenerate,
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "watertight": bool(len(faces)) and boundary == 0 and nonmanifold == 0,
        "is_valid": True,
    }


def stats_for_stl(path: str | Path) -> dict[str, Any]:
    return mesh_stats(load_stl(path))
