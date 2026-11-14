"""Geometric printability and build-orientation heuristics.

These checks are intentionally cheap: no FEM, no slicer run.  They answer the
questions a user asks before printing - does it fit, where are the overhangs,
how much support will it need, are there thin walls - and rank candidate
orientations by overhang/support/height/adhesion cost.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .preview import load_stl

OVERHANG_COS = math.cos(math.radians(45.0))
THIN_WALL_MM = 1.2
MAX_RAY_SAMPLES = 320
_EPS = 1e-6


def mesh_from_stl(path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (triangles Nx3x3, face normals Nx3, areas N)."""
    triangles = np.asarray(load_stl(path), dtype=np.float64).reshape(-1, 3, 3)
    if len(triangles) == 0:
        raise ValueError("the mesh contains no triangles")
    edges = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(edges, axis=1)
    areas = lengths / 2.0
    normals = np.zeros_like(edges)
    good = lengths > _EPS
    normals[good] = edges[good] / lengths[good, None]
    return triangles, normals, areas


def _ray_hits(origin: np.ndarray, direction: np.ndarray, triangles: np.ndarray) -> float | None:
    """Nearest positive hit distance for one ray (Möller-Trumbore, vectorised)."""
    v0 = triangles[:, 0]
    e1 = triangles[:, 1] - v0
    e2 = triangles[:, 2] - v0
    pvec = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, pvec)
    valid = np.abs(det) > 1e-10
    if not np.any(valid):
        return None
    inv = np.zeros_like(det)
    inv[valid] = 1.0 / det[valid]
    tvec = origin - v0
    u = np.einsum("ij,ij->i", tvec, pvec) * inv
    qvec = np.cross(tvec, e1)
    v = np.einsum("j,ij->i", direction, qvec) * inv
    t = np.einsum("ij,ij->i", e2, qvec) * inv
    good = valid & (u >= 0.0) & (v >= 0.0) & (u + v <= 1.0) & (t > 0.001)
    if not np.any(good):
        return None
    return float(t[good].min())


def estimate_wall_thickness(
    triangles: np.ndarray,
    normals: np.ndarray,
    areas: np.ndarray,
    *,
    samples: int = MAX_RAY_SAMPLES,
    threshold_mm: float = THIN_WALL_MM,
) -> dict[str, Any]:
    """Cast rays inward/outward from sampled faces to estimate wall thickness."""
    if len(triangles) == 0:
        return {"min_wall_mm": None, "thin_wall_fraction": None, "samples": 0}
    count = min(samples, len(triangles))
    rng = np.random.default_rng(0)
    weights = areas / areas.sum() if areas.sum() > 0 else None
    picks = rng.choice(len(triangles), size=count, replace=False, p=weights)
    centroids = triangles[picks].mean(axis=1)
    values: list[float] = []
    for index, origin in zip(picks, centroids, strict=True):
        normal = normals[index]
        hits: list[float] = []
        for direction in (normal, -normal):
            hit = _ray_hits(origin + direction * 0.002, direction, triangles)
            if hit is not None:
                hits.append(hit)
        if hits:
            values.append(min(hits))
    if not values:
        return {"min_wall_mm": None, "thin_wall_fraction": None, "samples": 0}
    data = np.asarray(values)
    return {
        "min_wall_mm": round(float(np.percentile(data, 5)), 2),
        "thin_wall_fraction": round(float((data < threshold_mm).mean()), 3),
        "samples": len(values),
    }


def mesh_metrics(triangles: np.ndarray, normals: np.ndarray, areas: np.ndarray) -> dict[str, Any]:
    """Orientation-independent metrics for the mesh as loaded."""
    points = triangles.reshape(-1, 3)
    bbox_min = points.min(axis=0)
    bbox_max = points.max(axis=0)
    bbox = bbox_max - bbox_min
    bottom = bbox_min[2]
    z = triangles[:, :, 2].mean(axis=1)

    total_area = float(areas.sum())
    down = normals[:, 2] < -OVERHANG_COS
    overhang = down & (z > bottom + 0.3)
    overhang_area = float(areas[overhang].sum())
    bed_touch = (normals[:, 2] < -0.9) & (z <= bottom + 0.3)
    contact_area = float(areas[bed_touch].sum())
    heights = np.clip(z[overhang] - bottom, 0.0, None)
    support_volume = float((areas[overhang] * heights * 0.25).sum())

    volume = float(
        np.einsum(
            "ij,ij->i",
            triangles[:, 0],
            np.cross(triangles[:, 1], triangles[:, 2]),
        ).sum()
        / 6.0
    )
    if volume < 0:
        volume = -volume
    return {
        "bbox_mm": [round(float(item), 3) for item in bbox],
        "bbox_min_mm": [round(float(item), 3) for item in bbox_min],
        "bbox_max_mm": [round(float(item), 3) for item in bbox_max],
        "volume_mm3": round(volume, 3),
        "surface_area_mm2": round(total_area, 2),
        "triangles": int(len(triangles)),
        "overhang_area_mm2": round(overhang_area, 2),
        "overhang_ratio": round(overhang_area / total_area, 4) if total_area else 0.0,
        "bed_contact_mm2": round(contact_area, 2),
        "support_estimate_mm3": round(support_volume, 1),
        "max_overhang_height_mm": round(float(heights.max()), 2) if len(heights) else 0.0,
    }


def _rotation(down: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotation mapping ``down`` to -Z, then a spin around Z."""
    down = down / np.linalg.norm(down)
    target = np.array([0.0, 0.0, -1.0])
    axis = np.cross(down, target)
    sina = np.linalg.norm(axis)
    cosa = float(np.dot(down, target))
    if sina < 1e-9:
        align = np.eye(3) if cosa > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        axis = axis / sina
        kx, ky, kz = axis
        k = np.array([[0, -kz, ky], [kz, 0, -kx], [-ky, kx, 0]])
        align = np.eye(3) + sina * k + (1 - cosa) * (k @ k)
    angle = math.radians(angle_deg)
    spin = np.array(
        [
            [math.cos(angle), -math.sin(angle), 0.0],
            [math.sin(angle), math.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    return spin @ align


def candidate_orientations() -> list[tuple[list[float], float, np.ndarray]]:
    directions = (
        [0, 0, -1],
        [0, 0, 1],
        [0, -1, 0],
        [0, 1, 0],
        [-1, 0, 0],
        [1, 0, 0],
    )
    out = []
    for direction in directions:
        for angle in (0.0, 90.0, 180.0, 270.0):
            out.append((direction, angle, _rotation(np.asarray(direction, dtype=float), angle)))
    return out


def evaluate_orientation(
    triangles: np.ndarray,
    normals: np.ndarray,
    areas: np.ndarray,
    rotation: np.ndarray,
    *,
    bed: tuple[float, float] | None = None,
) -> dict[str, Any]:
    moved = triangles @ rotation.T
    rotated_normals = normals @ rotation.T
    metrics = mesh_metrics(moved, rotated_normals, areas)
    bbox = metrics["bbox_mm"]
    footprint = bbox[0] * bbox[1]
    contact_ratio = metrics["bed_contact_mm2"] / footprint if footprint else 0.0
    support_ratio = (
        metrics["support_estimate_mm3"] / metrics["volume_mm3"] if metrics["volume_mm3"] else 0.0
    )
    height_ratio = bbox[2] / max(bbox) if max(bbox) else 0.0
    fits = bed is None or (bbox[0] <= bed[0] + 1e-6 and bbox[1] <= bed[1] + 1e-6)
    cost = (
        1.4 * metrics["overhang_ratio"]
        + 0.35 * support_ratio
        + 0.20 * height_ratio
        + 0.25 * (1.0 - min(contact_ratio, 1.0))
    )
    score = max(0.0, min(100.0, 100.0 * (1.0 - cost / 2.5)))
    if not fits:
        score *= 0.5
    return {
        "score": round(score, 1),
        "fits_bed": fits,
        "rotation": [[round(float(value), 6) for value in row] for row in rotation],
        "metrics": metrics,
    }


def printability_report(
    triangles: np.ndarray,
    normals: np.ndarray,
    areas: np.ndarray,
    *,
    bed: tuple[float, float] | None = None,
    thin_wall_mm: float = THIN_WALL_MM,
) -> dict[str, Any]:
    """Run printability checks and return a score plus actionable issues."""
    metrics = mesh_metrics(triangles, normals, areas)
    wall = estimate_wall_thickness(triangles, normals, areas, threshold_mm=thin_wall_mm)
    bbox = metrics["bbox_mm"]
    issues: list[dict[str, str]] = []

    def issue(severity: str, code: str, message: str, hint: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message, "hint": hint})

    if not metrics["triangles"]:
        issue("error", "empty", "The mesh has no triangles.", "Rebuild the model.")
    if bed is not None and (bbox[0] > bed[0] + 1e-6 or bbox[1] > bed[1] + 1e-6):
        issue(
            "error",
            "bed_overflow",
            f"Footprint {bbox[0]:.1f}x{bbox[1]:.1f} mm exceeds the {bed[0]:.0f}x{bed[1]:.0f} mm bed.",
            "Rotate the part or split it; check orientation_plan.",
        )
    if metrics["overhang_ratio"] > 0.08:
        issue(
            "warning",
            "overhang",
            f"{metrics['overhang_ratio'] * 100:.1f}% of the surface is a >45° overhang.",
            "Run orientation_plan, add chamfers/teardrops, or plan supports on non-critical faces.",
        )
    if metrics["bed_contact_mm2"] < 100 and bbox[2] > 15:
        issue(
            "warning",
            "small_contact",
            f"Only {metrics['bed_contact_mm2']:.0f} mm² touches the bed for a {bbox[2]:.0f} mm tall part.",
            "Add a brim, widen the base, or lay the part down.",
        )
    if wall["thin_wall_fraction"] is not None and wall["thin_wall_fraction"] > 0.02:
        severity = "warning" if wall["thin_wall_fraction"] > 0.08 else "info"
        issue(
            severity,
            "thin_wall",
            f"About {wall['thin_wall_fraction'] * 100:.0f}% of sampled surfaces are thinner than {thin_wall_mm} mm.",
            "Thicken walls to at least 1.2 mm (3 x 0.4 mm lines) around load paths.",
        )
    if bbox[2] > 200:
        issue(
            "info",
            "tall",
            f"Part is {bbox[2]:.0f} mm tall; tall prints amplify warping and wobble.",
            "Consider splitting, slowing down, or reorienting.",
        )

    score = 100.0
    for item in issues:
        score -= {"error": 40, "warning": 15, "info": 4}[item["severity"]]
    if wall["thin_wall_fraction"]:
        score -= min(20.0, wall["thin_wall_fraction"] * 100)
    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "metrics": metrics,
        "wall": wall,
        "bed_mm": list(bed) if bed else None,
        "issues": issues,
        "suggestions": [item["hint"] for item in issues],
    }
