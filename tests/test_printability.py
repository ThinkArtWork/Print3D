"""Tests for the geometric printability and orientation helpers."""

from __future__ import annotations

import numpy as np

from xprint.printability import (
    candidate_orientations,
    evaluate_orientation,
    mesh_metrics,
    printability_report,
)


def _box(width: float, depth: float, height: float, origin=(0.0, 0.0, 0.0)) -> np.ndarray:
    x, y, z = origin
    vertices = np.asarray(
        [
            (x, y, z),
            (x + width, y, z),
            (x + width, y + depth, z),
            (x, y + depth, z),
            (x, y, z + height),
            (x + width, y, z + height),
            (x + width, y + depth, z + height),
            (x, y + depth, z + height),
        ],
        dtype=float,
    )
    faces = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (2, 3, 7),
        (2, 7, 6),
        (1, 2, 6),
        (1, 6, 5),
        (3, 0, 4),
        (3, 4, 7),
    )
    return vertices[np.asarray(faces)]


def _arrays(triangles: np.ndarray):
    edges = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(edges, axis=1)
    normals = edges / lengths[:, None]
    return triangles, normals, lengths / 2.0


def test_box_metrics_and_score() -> None:
    triangles, normals, areas = _arrays(_box(20.0, 10.0, 5.0))
    report = printability_report(triangles, normals, areas)

    metrics = report["metrics"]
    assert metrics["bbox_mm"] == [20.0, 10.0, 5.0]
    assert metrics["volume_mm3"] == 1000.0
    assert metrics["overhang_ratio"] == 0.0
    assert metrics["bed_contact_mm2"] == 200.0
    assert report["score"] == 100.0
    assert report["issues"] == []
    assert report["wall"]["min_wall_mm"] == 5.0


def test_thin_wall_is_reported() -> None:
    triangles, normals, areas = _arrays(_box(20.0, 10.0, 0.8))
    report = printability_report(triangles, normals, areas)

    assert report["wall"]["min_wall_mm"] <= 1.0
    assert report["wall"]["thin_wall_fraction"] > 0.2
    assert any(item["code"] == "thin_wall" for item in report["issues"])
    assert report["score"] < 100.0


def test_overhang_and_support_estimate() -> None:
    base = _box(10.0, 10.0, 2.0, (5.0, 5.0, 0.0))
    cap = _box(20.0, 20.0, 2.0, (0.0, 0.0, 2.0))
    triangles, normals, areas = _arrays(np.concatenate([base, cap]))

    metrics = mesh_metrics(triangles, normals, areas)
    assert metrics["bbox_mm"] == [20.0, 20.0, 4.0]
    assert metrics["overhang_area_mm2"] > 300.0
    assert metrics["support_estimate_mm3"] > 0.0
    assert metrics["max_overhang_height_mm"] > 0.0


def test_bed_overflow_is_an_error() -> None:
    triangles, normals, areas = _arrays(_box(300.0, 50.0, 5.0))
    report = printability_report(triangles, normals, areas, bed=(270.0, 270.0))

    assert report["bed_mm"] == [270.0, 270.0]
    assert any(item["code"] == "bed_overflow" for item in report["issues"])
    assert report["score"] <= 60.0


def test_orientation_ranks_the_flat_side_up() -> None:
    triangles, normals, areas = _arrays(_box(10.0, 10.0, 80.0))
    ranked = []
    for _down, _angle, rotation in candidate_orientations():
        ranked.append(evaluate_orientation(triangles, normals, areas, rotation))
    ranked.sort(key=lambda item: item["score"], reverse=True)

    assert len(ranked) == 24
    assert ranked[0]["metrics"]["bbox_mm"][2] <= 10.001
    assert ranked[0]["score"] > ranked[-1]["score"]


def test_rotations_are_proper() -> None:
    for _down, _angle, rotation in candidate_orientations():
        assert np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9)
        assert np.isclose(np.linalg.det(rotation), 1.0, atol=1e-9)
