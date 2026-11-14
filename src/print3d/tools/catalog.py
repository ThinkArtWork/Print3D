"""MCP tools: catalog."""

from __future__ import annotations

import json
import shutil
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..assets import AssetStore, validate_asset_id
from ..errors import (
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    Print3DError,
)
from ..jobs import sha256_file
from ..runtime import Runtime, job_payload, tool_error
from ..viewer import (
    viewer_alias_path,
)


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="asset_export",
        description=(
            "Copy a built artifact to a workspace path the user can open. artifact "
            "is 'stl', 'step', 'script', 'svg' or 'dxf' (the last two come from "
            "model_drawing). Defaults to the workspace exports/ directory; out_path "
            "must resolve inside the workspace. WRITES the destination file: confirm "
            "must be exactly 'EXPORT <asset_id>'."
        ),
    )
    def asset_export(
        asset_id: str,
        confirm: str,
        revision: str = "",
        artifact: str = "stl",
        out_path: str = "",
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"EXPORT {asset_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(f"Asset {asset_id} has no revisions")
            record = next(
                (item for item in manifest.get("revisions", []) if item.get("revision") == target),
                {},
            )
            directory = assets.revision_dir(asset_id, target)
            name = str(manifest.get("name") or asset_id)

            if artifact == "script":
                source = directory / "model.py"
                suffix = ".py"
            elif artifact in {"stl", "step"}:
                stored = f"model.{artifact}"
                if stored not in (record.get("artifacts") or {}):
                    raise NotFound(
                        f"Revision {target} has no {artifact} artifact",
                        hint=f"Call model_build with asset_id {asset_id!r} first.",
                    )
                source = directory / stored
                suffix = f".{artifact}"
            elif artifact in {"svg", "dxf"}:
                stored = f"drawing.{artifact}"
                if stored not in ((record.get("drawing") or {}).get("files") or {}):
                    raise NotFound(
                        f"Revision {target} has no {stored}",
                        hint=f"Call model_drawing with asset_id {asset_id!r} first.",
                    )
                source = directory / stored
                suffix = f".{artifact}"
            else:
                raise InvalidArgument(
                    "artifact must be 'stl', 'step', 'script', 'svg' or 'dxf'",
                    details={"artifact": artifact},
                )
            if not source.is_file():
                raise NotFound(f"Artifact missing on disk: {source}")

            if out_path:
                # Exports stay inside the workspace: an LLM-supplied path must not
                # be able to overwrite files elsewhere on the host.
                destination = rt.workspace.safe_path(out_path, create_parents=True)
            else:
                destination = rt.workspace.root / "exports" / f"{name}-{target}{suffix}"
                destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "artifact": artifact,
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
                "hint": (
                    "Open the sheet in a browser (svg) or a CAD/drawing tool (dxf)."
                    if artifact in {"svg", "dxf"}
                    else "Open the file in a slicer, or import it into the printer's web UI."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_show",
        description=(
            "Return one asset's manifest, its revision history (prompt, model, "
            "geometry summary, STEP input, drawing files, viewer availability) and "
            "the source of a stored "
            "revision (latest by default), so a client model can read, explain, "
            "compare or repair it. Read-only."
        ),
    )
    def model_show(asset_id: str, revision: str = "") -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            script = ""
            params: dict[str, Any] = {}
            if target:
                _, script = assets.read_script(asset_id, target)
                params_path = assets.revision_dir(asset_id, target) / "params.json"
                if params_path.is_file():
                    params = json.loads(params_path.read_text(encoding="utf-8"))
            viewer_path = viewer_alias_path(
                assets.asset_dir(asset_id),
                name=str(manifest.get("name") or ""),
                asset_id=asset_id,
            )
            viewer_rel = rt.workspace.rel(viewer_path) if viewer_path.is_file() else None
            return {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "name": manifest.get("name"),
                "revision": target,
                "viewer": viewer_rel,
                "revisions": [
                    {
                        "revision": item.get("revision"),
                        "created_at": item.get("created_at"),
                        "checks": item.get("checks", {}),
                        "build_ok": (item.get("build") or {}).get("ok"),
                        "built": bool((item.get("build") or {}).get("ok")),
                        "bbox_mm": ((item.get("build") or {}).get("checks") or {}).get("bbox_mm"),
                        "volume_mm3": ((item.get("build") or {}).get("checks") or {}).get(
                            "volume_mm3"
                        ),
                        "volume_cm3": ((item.get("build") or {}).get("checks") or {}).get(
                            "volume_cm3"
                        ),
                        "artifacts": sorted(item.get("artifacts", {})),
                        "drawing": sorted((item.get("drawing") or {}).get("files") or {}),
                        "input": (item.get("build") or {}).get("input"),
                        "reference": (item.get("reference") or {}).get("file"),
                        "prompt": item.get("prompt"),
                        "model": item.get("model"),
                        "modeler": item.get("modeler"),
                        "viewer": viewer_rel,
                    }
                    for item in manifest.get("revisions", [])
                ],
                "script": script,
                "params": params,
                "reference": (params.get("reference") or {}).get("file"),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="asset_list",
        description="List generated model assets with their revision counts. Read-only.",
    )
    def asset_list(limit: int = 20) -> dict[str, Any]:
        try:
            if not 1 <= int(limit) <= 200:
                raise InvalidArgument("limit must be between 1 and 200")
            return {
                "ok": True,
                "writes_performed": False,
                "assets": AssetStore(rt.workspace).list(limit=int(limit)),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="job_status",
        description=(
            "Look up one asynchronous job by id (j_<timestamp>_<hex>). Returns state, "
            "progress, timestamps, result and artifacts. Read-only."
        ),
    )
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            if not isinstance(job_id, str) or not job_id.strip():
                raise InvalidArgument(
                    "job_id must be a non-empty string",
                    hint="Use the id returned when the job was created.",
                )
            job = rt.store.load(job_id.strip())
            return {
                "ok": True,
                "writes_performed": False,
                "job": job_payload(job),
            }
        except Print3DError as exc:
            return tool_error(exc)
