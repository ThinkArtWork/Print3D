"""MCP tools: models."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..assets import INPUT_STEP, AssetStore, validate_asset_id, validate_name
from ..cad import run_script
from ..drawing import DEFAULT_VIEWS as DRAWING_VIEWS
from ..drawing import run_drawing
from ..errors import (
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    Print3DError,
)
from ..images import sniff_image_mime
from ..jobs import sha256_file
from ..preview import DEFAULT_VIEWS, render_views
from ..runtime import Runtime, tool_error
from ..script_policy import check_script, summarise
from ..viewer import (
    viewer_alias_path,
)
from ..workspace import atomic_write_bytes
from .common import (
    _asset_viewer,
    _image_result,
    _reference_meta,
)


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="model_write",
        description=(
            "Store a build123d script as a new asset revision, without calling any "
            "LLM. The script must follow Print3D's contract (from build123d import *, "
            "a build_part() returning one solid, PARAMS in mm, base on z=0) and passes "
            "the static safety checks; nothing is executed here. Use asset_id to "
            "append to an existing asset, or name to create one. Optional `reference` "
            "attaches a concept image (the image_path returned by image_generate) to "
            "this revision so the model has a visual record of the request. WRITES "
            "files: confirm must be exactly 'WRITE <name>' for a new asset or "
            "'WRITE <asset_id>' to append. Then call model_build."
        ),
    )
    def model_write(
        script: str,
        confirm: str,
        name: str = "",
        asset_id: str = "",
        prompt: str = "",
        reference: str = "",
    ) -> dict[str, Any]:
        try:
            target = asset_id or name
            expected = f"WRITE {target}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not isinstance(script, str) or not script.strip():
                raise InvalidArgument(
                    "script must not be empty",
                    hint="Send the complete Python source of model.py.",
                )

            assets = AssetStore(rt.workspace)
            if asset_id:
                validate_asset_id(asset_id)
                manifest = assets.load(asset_id)
            else:
                name = validate_name(name)
                manifest = None

            checks = summarise(check_script(script))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The script failed Print3D's safety checks; nothing was stored",
                    hint="See details.issues for line numbers, then call model_write again.",
                    details={"issues": checks["errors"]},
                )

            reference_path: Path | None = None
            reference_meta: dict[str, Any] = {}
            if reference:
                reference_path = rt.workspace.safe_path(reference, must_exist=True)
                if reference_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise InvalidArgument(
                        "reference must be a PNG, JPEG or WebP image",
                        hint="Use the image_path returned by image_generate.",
                        details={"path": reference},
                    )
                head = reference_path.read_bytes()[:32]
                if sniff_image_mime(head) is None:
                    raise InvalidArgument(
                        "reference is not a valid image file",
                        hint="Use the image_path returned by image_generate.",
                        details={"path": reference},
                    )
                reference_meta = _reference_meta(rt, reference_path)
                if not prompt.strip():
                    prompt = str(reference_meta.get("prompt") or "")

            if manifest is None:
                manifest = assets.create(name)

            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt=prompt.strip() or "(written by an MCP client)",
                model="external",
                modeler="mcp-client",
                checks=checks,
                reference=reference_path,
                reference_meta=reference_meta,
            )
            rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.py")
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "name": manifest["name"],
                "revision": record["revision"],
                "script_path": rt.workspace.rel(
                    assets.revision_dir(manifest["asset_id"], record["revision"]) / "model.py"
                ),
                "sha256": record["sha256"],
                "checks": checks,
                "reference": record["reference"],
                "hint": (
                    f"Call model_build with asset_id {manifest['asset_id']!r} and "
                    f"confirm 'BUILD {manifest['asset_id']}'."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_build",
        description=(
            "Execute a stored asset revision in Print3D's sandboxed build123d "
            "interpreter (CPU/time/file limits, no API keys in the environment) and "
            "attach the result to that revision. Returns real geometry checks "
            "(watertight, boundary/non-manifold edges, bbox, volume) and stores "
            "model.stl/model.step plus a self-contained interactive viewer.html. "
            "Pass `input` with a .step/.stp path to import an existing part: the "
            "file is copied into the revision as input.step and its absolute path "
            "is exposed to the script as the module global INPUT_STEP, so a script "
            "can do `part = import_step(INPUT_STEP)` and edit or re-validate it "
            "without redrawing geometry. This is the feedback step of the closed "
            "loop: read the checks, fix the script, call model_write again, and call "
            "`present` with viewer_file so the user can open the 3D preview. WRITES "
            "files: confirm must be exactly 'BUILD <asset_id>'."
        ),
    )
    def model_build(
        asset_id: str,
        confirm: str,
        revision: str = "",
        input: str = "",
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"BUILD {asset_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            if manifest.get("kind", "build123d") != "build123d":
                raise InvalidArgument(
                    f"Asset {asset_id} was produced by {manifest.get('kind')} and has no build123d script",
                    hint="Use model_preview or asset_export for this asset, then blender_run/generate_local to add a revision.",
                )
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {asset_id} has no revisions",
                    hint="Call model_write first.",
                )
            script_path, script = assets.read_script(asset_id, target)
            checks = summarise(check_script(script))
            if not checks["ok"]:
                raise InvalidArgument(
                    "The stored script failed safety checks; refusing to execute it",
                    details={"issues": checks["errors"]},
                )

            input_record: dict[str, Any] = {}
            input_step: Path | None = None
            if input:
                input_record = assets.store_input(asset_id, target, input)
                input_step = assets.revision_dir(asset_id, target) / INPUT_STEP

            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/builds/{asset_id}_{target}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_script(
                script_path,
                workdir=workdir,
                input_step=input_step,
                timeout_s=cad.timeout_s,
                python=cad.python,
            )

            stored: dict[str, Path] = {}
            for value in result.artifacts.values():
                path = Path(value)
                if path.is_file():
                    stored[f"model{path.suffix or '.stl'}"] = path
            build = result.to_dict()
            if input_record:
                build["input"] = input_record
                rt.record_write(f"assets/{asset_id}/{target}/{INPUT_STEP}")
            record = assets.record_build(
                asset_id,
                target,
                build=build,
                artifacts=stored,
            )
            rt.record_write(f"assets/{asset_id}/{target}/model.stl")

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(rt, assets, assets.load(asset_id), selected=target)

            hint = (
                "Review the checks, open viewer_file for an interactive look, then slice. "
                "If a check failed, fix the script and call model_write followed by model_build."
            )
            if input_record:
                hint = (
                    "STEP input stored as input.step; the script reads it through the "
                    "INPUT_STEP global. " + hint
                )
            if not result.ok:
                hint = (
                    "Build failed; read error/stderr_tail above, fix model.py and "
                    "call model_write, then model_build again."
                )
            stored_input = assets.stored_input(asset_id, target)
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(record["artifacts"]),
                **({"input": stored_input} if stored_input else {}),
                "error": result.error,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
                **viewer,
                "hint": hint,
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_drawing",
        description=(
            "Project a built revision's model.step into an engineering drawing: one "
            "sheet with "
            + ", ".join(DRAWING_VIEWS)
            + " views laid out third-angle, dashed hidden lines (orthographic views) and "
            "view labels, "
            "written as drawing.svg and drawing.dxf inside the revision. Use it "
            "after model_build when a printed sheet is wanted, or to hand the part "
            "to someone without a slicer. Returns the sheet size and per-view edge "
            "counts; export the files with asset_export(artifact='svg' or 'dxf'). "
            "WRITES files: confirm must be exactly 'DRAWING <asset_id>'."
        ),
    )
    def model_drawing(
        asset_id: str,
        confirm: str,
        revision: str = "",
        views: str = "",
        scale: float = 1.0,
        hidden: bool = True,
    ) -> dict[str, Any]:
        try:
            validate_asset_id(asset_id)
            expected = f"DRAWING {asset_id}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {asset_id} has no revisions",
                    hint="Call model_write first.",
                )
            record = next(
                (item for item in manifest.get("revisions", []) if item.get("revision") == target),
                {},
            )
            if "model.step" not in (record.get("artifacts") or {}):
                raise NotFound(
                    f"Revision {target} has no STEP artifact to draw",
                    hint=(
                        f"Call model_build with asset_id {asset_id!r} first; "
                        "drawings are projected from model.step."
                    ),
                )
            step_path = assets.revision_dir(asset_id, target) / "model.step"
            cad = rt.loaded.config.cad
            workdir = rt.workspace.safe_path(
                f"cache/drawings/{asset_id}_{target}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_drawing(
                step_path,
                workdir=workdir,
                views=views,
                scale=scale,
                hidden=hidden,
                timeout_s=cad.timeout_s,
                python=cad.python,
            )
            if not result.ok:
                return {
                    "ok": False,
                    "writes_performed": False,
                    "asset_id": asset_id,
                    "revision": target,
                    "error": result.error,
                    "timed_out": result.timed_out,
                    "stdout_tail": result.stdout[-2000:],
                    "stderr_tail": result.stderr[-2000:],
                    "hint": result.hint
                    or "Fix the geometry, rebuild, then call model_drawing again.",
                }
            files = {key: Path(value) for key, value in result.files.items()}
            record_out = assets.record_output(
                asset_id,
                target,
                key="drawing",
                meta={
                    "views": [item["name"] for item in (result.checks.get("views") or [])],
                    "scale": result.checks.get("scale"),
                    "hidden": result.checks.get("hidden"),
                    "sheet_mm": result.checks.get("sheet_mm"),
                    "warnings": result.warnings,
                },
                files=files,
            )
            for name in sorted(files):
                rt.record_write(f"assets/{asset_id}/{target}/{name}")
            drawing = record_out.get("drawing") or {}
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": asset_id,
                "revision": target,
                "duration_s": round(result.duration_s, 2),
                "sheet_mm": result.checks.get("sheet_mm"),
                "views": result.checks.get("views"),
                "edges": result.checks.get("edges"),
                "files": sorted(drawing.get("files") or {}),
                "warnings": result.warnings,
                "hint": (
                    "Open drawing.svg in a browser or drawing.dxf in a CAD tool; "
                    "copy them out with asset_export(artifact='svg') or ('dxf')."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_preview",
        description=(
            "Render a built revision's model.stl to a PNG with labelled orthographic "
            "views (iso/front/top/right by default) and return it inline, together "
            "with the concept reference when the revision has one. Use this after "
            "model_build to see the real geometry and compare it with the request; "
            "then fix the script and call model_write/model_build again. Read-only "
            "for assets: the PNG is cached under cache/previews/ and nothing in the "
            "revision changes."
        ),
    )
    def model_preview(
        asset_id: str,
        revision: str = "",
        views: str = "",
        width: int = 900,
        height: int = 700,
    ) -> Any:
        try:
            validate_asset_id(asset_id)
            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {asset_id} has no revisions",
                    hint="Call model_write first.",
                )
            directory = assets.revision_dir(asset_id, target)
            stl = directory / "model.stl"
            if not stl.is_file():
                raise InvalidArgument(
                    f"Revision {target} has no built model yet",
                    hint=(
                        f"Call model_build with asset_id {asset_id!r} and "
                        f"confirm 'BUILD {asset_id}' first."
                    ),
                )
            if not 320 <= int(width) <= 1600:
                raise InvalidArgument("width must be between 320 and 1600 pixels")
            if not 240 <= int(height) <= 1200:
                raise InvalidArgument("height must be between 240 and 1200 pixels")

            wanted = (
                tuple(item.strip().lower() for item in (views or "").split(",") if item.strip())
                or DEFAULT_VIEWS
            )
            digest = sha256_file(stl)[:8]
            cache = rt.workspace.safe_path(
                f"cache/previews/{asset_id}_{target}_{digest}_{int(width)}x{int(height)}.png",
                create_parents=True,
            )
            cached = cache.is_file()
            if cached:
                png = cache.read_bytes()
            else:
                caption = f"{manifest.get('name') or asset_id} {target}"
                try:
                    png = render_views(
                        stl,
                        views=wanted,
                        width=int(width),
                        height=int(height),
                        caption=caption,
                    )
                except ValueError as exc:
                    raise InvalidArgument(
                        f"Cannot render the model: {exc}",
                        hint="Check the views names or rebuild the model.",
                        details={"views": list(wanted)},
                    ) from None
                atomic_write_bytes(cache, png, mode=0o644)

            images: list[tuple[bytes, str, str | None]] = []
            reference_file = ""
            params_path = directory / "params.json"
            if params_path.is_file():
                try:
                    params = json.loads(params_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    params = {}
                reference_file = str((params.get("reference") or {}).get("file") or "")
            if not reference_file:
                candidates = sorted(directory.glob("reference.*"))
                reference_file = candidates[0].name if candidates else ""
            if reference_file and (directory / reference_file).is_file():
                ref_path = directory / reference_file
                ref_mime = sniff_image_mime(ref_path.read_bytes()[:32]) or "image/png"
                images.append(
                    (ref_path.read_bytes(), ref_mime, "Concept reference (what was asked for):")
                )

            images.append(
                (
                    png,
                    "image/png",
                    f"Rendered model, revision {target} (views: {', '.join(wanted)}):",
                )
            )
            viewer_file = viewer_alias_path(
                assets.asset_dir(asset_id),
                name=str(manifest.get("name") or ""),
                asset_id=asset_id,
            )
            payload = {
                "ok": True,
                "writes_performed": False,
                "asset_id": asset_id,
                "revision": target,
                "image_path": rt.workspace.rel(cache),
                "viewer_path": rt.workspace.rel(viewer_file) if viewer_file.is_file() else None,
                "viewer_file": str(viewer_file) if viewer_file.is_file() else None,
                "cached": cached,
                "views": list(wanted),
                "width": int(width),
                "height": int(height),
                "bytes": len(png),
                "reference": rt.workspace.rel(directory / reference_file)
                if reference_file
                else None,
                "hint": (
                    "Compare the render with the request and the concept reference. "
                    "If proportions or features are wrong, edit model.py and call "
                    "model_write (new revision) then model_build again."
                ),
            }
            return _image_result(payload, images)
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="model_view",
        description=(
            "Write one self-contained interactive 3D viewer (HTML, orbit / pan / "
            "zoom, no server or CDN) per asset. The file embeds every built "
            "revision and has a revision dropdown, so only one file is ever handed "
            "to the user. Open it in a browser or pass it to `present`. WRITES one "
            "file in the asset directory: confirm must be exactly 'VIEW <asset_id>'."
        ),
    )
    def model_view(asset_id: str, confirm: str, revision: str = "") -> dict[str, Any]:
        try:
            token = validate_asset_id(asset_id)
            expected = f"VIEW {token}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )

            assets = AssetStore(rt.workspace)
            manifest = assets.load(token)
            target = revision or assets.latest_revision(manifest)
            if not target:
                raise InvalidArgument(
                    f"Asset {token} has no revisions",
                    hint="Call model_write first.",
                )
            directory = assets.revision_dir(token, target)
            stl = directory / "model.stl"
            if not stl.is_file():
                raise InvalidArgument(
                    f"Revision {target} has no built model yet",
                    hint=(
                        f"Call model_build with asset_id {token!r} and "
                        f"confirm 'BUILD {token}' first."
                    ),
                )

            viewer = _asset_viewer(rt, assets, manifest, selected=target)
            if "viewer_error" in viewer:
                raise InvalidArgument(
                    f"Cannot build the 3D viewer: {viewer['viewer_error']}",
                    hint="Reduce the triangle count or inspect the STL.",
                )
            return {
                "ok": True,
                "writes_performed": True,
                "asset_id": token,
                "revision": target,
                "viewer_path": viewer["viewer_path"],
                "viewer_file": viewer["viewer_file"],
                "viewer_revision": viewer["viewer_revision"],
                "viewer_revisions": viewer["viewer_revisions"],
                "file": Path(viewer["viewer_file"]).name,
                "triangles": viewer["viewer_triangles"],
                "bytes": viewer["viewer_bytes"],
                "hint": (
                    "Open the HTML in a browser to switch revisions, orbit, pan, "
                    "zoom, spin and save a PNG. Call `present` with viewer_file to "
                    "give the user an open action in DSH."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)
