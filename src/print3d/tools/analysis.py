"""MCP tools: analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
from mcp.server.fastmcp import FastMCP

from ..assets import AssetStore, validate_asset_id
from ..errors import (
    InvalidArgument,
    NotFound,
    Print3DError,
)
from ..printability import (
    THIN_WALL_MM,
    candidate_orientations,
    evaluate_orientation,
    mesh_from_stl,
    printability_report,
)
from ..runtime import Runtime, tool_error
from .common import (
    _bed_for_target,
)


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="printability_check",
        description=(
            "Read-only geometric printability report for a built revision: watertightness, "
            "boundary/non-manifold edges, overhang area, estimated support volume, bed "
            "contact, thin-wall estimate, bed fit, a 0-100 score and actionable issues. "
            "Pass target to include its bed size."
        ),
    )
    def printability_check(asset_id: str, target: str = "", revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then printability_check.",
                )
            bed = _bed_for_target(rt, target)
            triangles, normals, areas = mesh_from_stl(stl)
            report = printability_report(triangles, normals, areas, bed=bed)
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target or None,
                "thin_wall_mm": THIN_WALL_MM,
                **report,
                "hint": (
                    "Fix error/warning issues before slicing. If overhang dominates, "
                    "call orientation_plan for ranked build orientations."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)
        except ValueError as exc:
            return tool_error(InvalidArgument(f"Could not inspect the mesh: {exc}"))

    @server.tool(
        name="orientation_plan",
        description=(
            "Read-only build-orientation ranking: evaluates 24 axis-aligned rotations "
            "(6 down axes x 4 spins), compares overhang area, estimated support volume, "
            "height and bed contact, and returns the best rotations with metrics."
        ),
    )
    def orientation_plan(
        asset_id: str, target: str = "", revision: str = "", limit: int = 5
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            if not 1 <= int(limit) <= 24:
                raise InvalidArgument("limit must be between 1 and 24")
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            rev = revision or assets.latest_revision(manifest) or ""
            if not rev:
                raise NotFound(f"Asset {asset_id} has no revisions", hint="Call model_write first.")
            stl = assets.revision_dir(asset_id, rev) / "model.stl"
            if not stl.is_file():
                raise NotFound(
                    f"Revision {rev} has no built model yet",
                    hint="Call model_build first, then orientation_plan.",
                )
            bed = _bed_for_target(rt, target)
            triangles, normals, areas = mesh_from_stl(stl)
            current = evaluate_orientation(triangles, normals, areas, np.eye(3), bed=bed)
            rows: list[dict[str, Any]] = []
            for down, angle, rotation in candidate_orientations():
                evaluated = evaluate_orientation(triangles, normals, areas, rotation, bed=bed)
                rows.append(
                    {
                        "score": evaluated["score"],
                        "fits_bed": evaluated["fits_bed"],
                        "rotation": evaluated["rotation"],
                        "down_axis": down,
                        "spin_deg": angle,
                        "metrics": evaluated["metrics"],
                    }
                )
            rows.sort(key=lambda item: (-item["score"], item["metrics"]["bbox_mm"][2]))
            top = rows[: int(limit)]
            for index, row in enumerate(top, start=1):
                row["rank"] = index
            best = top[0]
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": rev,
                "target": target or None,
                "bed_mm": list(bed) if bed else None,
                "current": {
                    "score": current["score"],
                    "fits_bed": current["fits_bed"],
                    "metrics": current["metrics"],
                },
                "recommended": best,
                "candidates": top,
                "hint": (
                    "Rotate the part so the chosen down_axis becomes -Z (rotation is "
                    "applied as vertices @ rotation.T). Re-run printability_check after "
                    "reorienting; supports still need slicer settings."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)
        except ValueError as exc:
            return tool_error(InvalidArgument(f"Could not inspect the mesh: {exc}"))
