"""MCP tools: generating."""

from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..assets import AssetStore, validate_asset_id, validate_name
from ..blender import run_blender
from ..blender_policy import check_blender_script
from ..cloud import build_provider, download_model, to_stl
from ..errors import (
    AdapterUnavailable,
    ConfigError,
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    Print3DError,
)
from ..images import MAX_IMAGE_BYTES, sniff_image_mime
from ..local_mesh import DEFAULT_TIMEOUT_S as LOCAL_GENERATION_TIMEOUT
from ..local_mesh import run_local
from ..mesh import stats_for_stl
from ..mesh_fit import DEFAULT_MAX_TRIANGLES, SUPPORTED_SUFFIXES, fit_script, target_spec
from ..runner import JobRunner
from ..runtime import Runtime, tool_error
from ..script_policy import summarise
from ..workspace import atomic_write_bytes
from .common import (
    _asset_viewer,
)


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="blender_run",
        description=(
            "Turn a bpy Python script into a printable mesh by running Blender in "
            "background mode (blender -b --factory-startup --python scene.py), then "
            "exporting every mesh object to model.stl + model.glb, writing an "
            "interactive viewer.html and inspecting it. "
            "This is the Blender path for text-to-3D: the client model writes the "
            "scene (primitive_*, bmesh, solidify, remesh, ...); for image-to-3D it "
            "looks at the attached image itself and the script can read the same "
            "file from sys.argv after '--'. Pass asset_id to append a new revision "
            "to an existing Blender asset. WRITES files and executes code in "
            "Blender: confirm must be exactly 'BLENDER <name>' or 'BLENDER <asset_id>'."
        ),
    )
    def blender_run(
        script: str,
        confirm: str,
        name: str = "",
        asset_id: str = "",
        image: str = "",
        blender_path: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            token = validate_asset_id(asset_id) if asset_id else validate_name(name)
            expected = f"BLENDER {token}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )

            issues = check_blender_script(script)
            errors = [issue.as_dict() for issue in issues if issue.severity == "error"]
            if errors:
                raise InvalidArgument(
                    "The bpy script failed safety checks; nothing was executed",
                    details={"issues": errors},
                )

            assets = AssetStore(rt.workspace)
            manifest = assets.load(asset_id) if asset_id else assets.create(name, kind="blender")
            if manifest.get("kind") != "blender":
                raise InvalidArgument(
                    f"Asset {manifest['asset_id']} was built with {manifest.get('kind') or 'build123d'}",
                    hint="Use model_write/model_build for build123d assets, or pass name= to create a new Blender asset.",
                )

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Pass a PNG/JPEG/WebP path or omit image=.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )

            checks = summarise(issues)
            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt="",
                model="blender",
                modeler="blender",
                checks={"policy": checks},
                reference=image_path,
                reference_meta={"source_path": str(image_path)} if image_path else None,
                kind="blender",
            )
            revision = record["revision"]
            render = rt.loaded.config.render
            workdir = rt.workspace.safe_path(
                f"cache/blender/{manifest['asset_id']}_{revision}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_blender(
                script,
                workdir=workdir,
                image=image_path,
                blender_path=blender_path or render.blender_path,
                timeout_s=float(timeout_s or render.timeout_s),
            )

            stored: dict[str, Path] = {}
            for stored_name, value in result.artifacts.items():
                path = Path(value)
                if path.is_file():
                    stored[Path(stored_name).name] = path
            if stored:
                assets.record_build(
                    manifest["asset_id"],
                    revision,
                    build=result.to_dict(),
                    artifacts=stored,
                )
                rt.record_write(f"assets/{manifest['asset_id']}/{revision}/model.stl")

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=revision,
                )

            hint = (
                "Review checks, open viewer_file for an interactive look, or call "
                "model_preview. Fix scene.py and call blender_run with asset_id to "
                "add the next revision."
                if result.ok
                else "Read stderr_tail, fix scene.py, then call blender_run again with asset_id."
            )
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "revision": revision,
                "engine": "blender",
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(stored),
                "error": result.error,
                "stdout_tail": result.stdout[-1500:],
                "stderr_tail": result.stderr[-1500:],
                **viewer,
                "hint": hint,
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="mesh_fit",
        description=(
            "Fit an existing STL/GLB/OBJ mesh to a real millimetre size and turn "
            "it into a versioned Print3D asset. This is the finishing step for "
            "image-to-3D: keep the generated shape, repair it, scale it, seat it on "
            "z=0, centre it, decimate to a preview-friendly triangle budget, then "
            "export STL/GLB and a named viewer. Sizing is explicit: pass "
            "size_mm=[x,y,z] for an exact envelope (non-uniform scale), or "
            "max_dimension_mm=<longest side> to scale uniformly and preserve the "
            "image proportions. Pass source= for a file path, or asset_id= (plus "
            "optional revision=) to use an asset's stored model.stl. WRITES files "
            "and runs Blender: confirm must be exactly 'FIT <name>'."
        ),
    )
    def mesh_fit(
        name: str,
        confirm: str,
        size_mm: list[float] | None = None,
        max_dimension_mm: float = 0.0,
        source: str = "",
        asset_id: str = "",
        revision: str = "",
        max_triangles: int = DEFAULT_MAX_TRIANGLES,
        image: str = "",
        blender_path: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"FIT {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if bool(source) == bool(asset_id):
                raise InvalidArgument(
                    "Pass exactly one of source= or asset_id=",
                    hint="source= is a STL/GLB/OBJ path; asset_id= fits a stored asset revision.",
                )
            try:
                exact, uniform = target_spec(size_mm, max_dimension_mm)
            except ValueError as exc:
                raise InvalidArgument(
                    str(exc),
                    hint="Use size_mm=[x,y,z] for an exact envelope, or max_dimension_mm for uniform scaling.",
                ) from None
            if not 1_000 <= int(max_triangles) <= 400_000:
                raise InvalidArgument(
                    "max_triangles must be between 1000 and 400000",
                    details={"max_triangles": max_triangles},
                )

            assets = AssetStore(rt.workspace)
            origin: dict[str, Any]
            if asset_id:
                source_asset = assets.load(validate_asset_id(asset_id))
                chosen = revision or assets.latest_revision(source_asset)
                known = {item.get("revision") for item in source_asset.get("revisions", [])}
                if not chosen or chosen not in known:
                    raise NotFound(
                        f"Revision {revision!r} not found on {source_asset['asset_id']}",
                        details={"available": sorted(v for v in known if v)},
                    )
                source_path = assets.revision_dir(source_asset["asset_id"], chosen) / "model.stl"
                if not source_path.is_file():
                    raise NotFound(
                        f"Stored model.stl not found for {source_asset['asset_id']} {chosen}",
                        hint="Fit the source file directly with source= instead.",
                    )
                origin = {
                    "asset_id": source_asset["asset_id"],
                    "revision": chosen,
                    "name": source_asset.get("name", ""),
                }
            else:
                source_path = Path(source).expanduser().resolve()
                if not source_path.is_file():
                    raise NotFound(
                        f"Mesh not found: {source_path}",
                        hint="Pass an existing .stl, .glb, .gltf or .obj file.",
                    )
                if source_path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise InvalidArgument(
                        f"Unsupported mesh format: {source_path.suffix or '(none)'}",
                        hint="Supported: " + ", ".join(sorted(SUPPORTED_SUFFIXES)),
                    )
                origin = {"path": str(source_path)}

            script = fit_script(
                source_path,
                exact,
                int(max_triangles),
                max_dimension_mm=uniform,
            )
            issues = check_blender_script(script)
            errors = [issue.as_dict() for issue in issues if issue.severity == "error"]
            if errors:
                raise InvalidArgument(
                    "The generated fit script failed safety checks; nothing was executed",
                    details={"issues": errors},
                )

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Pass a PNG/JPEG/WebP path or omit image=.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )

            manifest = assets.create(name, kind="blender")
            record = assets.add_revision(
                manifest["asset_id"],
                script,
                prompt=(
                    f"Fit {source_path.name} to "
                    + (
                        f"{exact[0]:g} × {exact[1]:g} × {exact[2]:g} mm"
                        if exact
                        else f"a {uniform:g} mm longest side (uniform)"
                    )
                    + f" with at most {int(max_triangles)} triangles"
                ),
                model="mesh_fit",
                modeler="mesh_fit",
                checks={"policy": summarise(issues)},
                reference=image_path,
                reference_meta={"source_path": str(image_path)} if image_path else None,
                kind="blender",
            )
            revision = record["revision"]
            render_config = rt.loaded.config.render
            workdir = rt.workspace.safe_path(
                f"cache/mesh_fit/{manifest['asset_id']}_{revision}_{secrets.token_hex(4)}",
                create_parents=True,
            )
            result = run_blender(
                script,
                workdir=workdir,
                image=image_path,
                blender_path=blender_path or render_config.blender_path,
                timeout_s=float(timeout_s or render_config.timeout_s),
            )

            stored: dict[str, Path] = {}
            for stored_name, value in result.artifacts.items():
                path = Path(value)
                if path.is_file():
                    stored[Path(stored_name).name] = path
            if stored:
                assets.record_build(
                    manifest["asset_id"],
                    revision,
                    build=result.to_dict(),
                    artifacts=stored,
                )
                rt.record_write(f"assets/{manifest['asset_id']}/{revision}/model.stl")

            viewer: dict[str, Any] = {}
            if result.ok:
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=revision,
                )

            hint = (
                "Open viewer_file to inspect the fitted mesh, then call model_preview "
                "or printability_check."
                if result.ok
                else "Read stderr_tail, check the source mesh, then call mesh_fit again."
            )
            return {
                "ok": result.ok,
                "writes_performed": True,
                "asset_id": manifest["asset_id"],
                "revision": revision,
                "engine": "blender",
                "source": origin,
                "requested_size_mm": list(exact) if exact else None,
                "max_dimension_mm": uniform or None,
                "max_triangles": int(max_triangles),
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "checks": result.checks,
                "artifacts": sorted(stored),
                "error": result.error,
                "stdout_tail": result.stdout[-1500:],
                "stderr_tail": result.stderr[-1500:],
                **viewer,
                "hint": hint,
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="generate_cloud",
        description=(
            "Generate a 3D model with a cloud provider (tripo, hunyuan_openai or "
            "hunyuan TC3) in the background. Configure it in providers.cloud.<id> "
            "(adapter, key env, scale, timeout). Print3D submits the task, polls it, "
            "downloads the result, converts it to STL, records real geometry checks "
            "and stores a versioned asset. Pass optional size_mm=[x,y,z] or "
            "max_dimension_mm=<longest side> to run the same mesh fit after "
            "generation; the raw mesh is kept as source.stl. Returns a job_id: poll "
            "job_status until succeeded, then call model_preview. WRITES files and "
            "spends provider credits: confirm must be exactly 'CLOUD <name>'."
        ),
    )
    def generate_cloud(
        provider: str,
        name: str,
        confirm: str,
        prompt: str = "",
        image: str = "",
        timeout_s: int = 0,
        size_mm: list[float] | None = None,
        max_dimension_mm: float = 0.0,
        max_triangles: int = DEFAULT_MAX_TRIANGLES,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"CLOUD {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not prompt.strip() and not image:
                raise InvalidArgument(
                    "Provide prompt, image, or both",
                    hint="Cloud providers accept a text prompt, a reference image, or both.",
                )
            try:
                exact, uniform = (
                    target_spec(size_mm, max_dimension_mm)
                    if (size_mm or max_dimension_mm)
                    else (None, 0.0)
                )
            except ValueError as exc:
                raise InvalidArgument(
                    str(exc),
                    hint="Use size_mm=[x,y,z] for an exact envelope, or max_dimension_mm for uniform scaling.",
                ) from None
            if not 1_000 <= int(max_triangles) <= 400_000:
                raise InvalidArgument(
                    "max_triangles must be between 1000 and 400000",
                    details={"max_triangles": max_triangles},
                )
            fit_requested = bool(exact or uniform)

            entry = rt.loaded.config.providers.cloud.get(provider)
            if entry is None:
                raise InvalidArgument(
                    f"Unknown cloud provider: {provider}",
                    hint="Add providers.cloud.<id> to config.json; `print3d config` shows the file.",
                    details={"configured": sorted(rt.loaded.config.providers.cloud)},
                )
            if not entry.get("enabled", True):
                raise AdapterUnavailable(
                    f"Cloud provider {provider!r} is disabled",
                    hint="Set providers.cloud.<id>.enabled to true.",
                )
            adapter = str(entry.get("adapter") or provider).strip().lower().replace("-", "_")

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Call reference_import first or pass a valid path.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )
                if image_path.stat().st_size > MAX_IMAGE_BYTES:
                    raise InvalidArgument("The reference image is larger than 12 MB")

            api_key = rt.secrets.provider_key(provider, api_key_env=entry.get("api_key_env"))
            secret_id = os.environ.get(str(entry.get("secret_id_env") or "")) or (
                rt.secrets.provider_field(provider, "secret_id")
            )
            secret_key = os.environ.get(str(entry.get("secret_key_env") or "")) or (
                rt.secrets.provider_field(provider, "secret_key")
            )
            if adapter == "tripo" and not api_key:
                raise AdapterUnavailable(
                    "No Tripo API key",
                    hint=f"Set {entry.get('api_key_env') or 'TRIPO_API_KEY'}.",
                )
            if adapter == "hunyuan_openai" and not api_key:
                raise AdapterUnavailable(
                    "No API key for the Hunyuan OpenAI-compatible provider",
                    hint=f"Set {entry.get('api_key_env') or 'HUNYUAN3D_API_KEY'}.",
                )
            if adapter == "hunyuan" and not (secret_id and secret_key):
                raise AdapterUnavailable(
                    "No Tencent Cloud credentials for Hunyuan 3D",
                    hint="Set TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY.",
                )

            scale = float(entry.get("scale") or 1.0)
            if scale <= 0:
                raise ConfigError(
                    f"providers.cloud.{provider}.scale must be positive",
                    hint=(
                        "Cloud meshes often use metres: use 1000 for millimetres, "
                        "then set a smaller value if the generated model is oversized."
                    ),
                )
            budget = float(timeout_s or entry.get("timeout_s") or 600)
            poll_interval = float(entry.get("poll_interval_s") or 3.0)
            mode = "both" if prompt.strip() and image else ("image" if image else "text")

            def work(job_id: str) -> dict[str, Any]:
                client = build_provider(
                    provider,
                    entry,
                    api_key=api_key,
                    secret_id=secret_id,
                    secret_key=secret_key,
                )
                try:
                    task_id = client.submit(
                        prompt=(prompt.strip() or None),
                        image=(image_path.read_bytes() if image_path else None),
                    )
                    rt.store.set_progress(job_id, 0.05, message=f"{provider} task {task_id}")
                    deadline = time.monotonic() + budget
                    task = None
                    while True:
                        task = client.poll(task_id)
                        progress = max(5.0, min(float(task.progress or 5), 95.0)) / 100.0
                        rt.store.set_progress(
                            job_id, progress, message=f"{provider}: {task.status}"
                        )
                        if task.status == "success":
                            break
                        if task.status in {"failed", "cancelled"}:
                            raise AdapterUnavailable(
                                f"{provider} task {task.status}: {task.error or 'no detail'}",
                                hint="Check the provider dashboard and your credits.",
                                details={"task_id": task_id},
                            )
                        if time.monotonic() >= deadline:
                            raise AdapterUnavailable(
                                f"{provider} task did not finish within {int(budget)} s",
                                hint="Raise providers.cloud.<id>.timeout_s and retry.",
                                details={"task_id": task_id},
                            )
                        time.sleep(max(0.2, poll_interval))
                    if not task.model_url:
                        raise AdapterUnavailable(
                            f"{provider} finished without a model URL",
                            details={"task_id": task_id},
                        )
                    raw, suffix = download_model(task.model_url)
                    stl = to_stl(raw, suffix, scale=scale)
                    staging = rt.workspace.safe_path(f"cache/cloud/{job_id}", create_parents=True)
                    stl_path = staging / "model.stl"
                    atomic_write_bytes(stl_path, stl, mode=0o644)
                    artifacts = {"model.stl": stl_path}
                    if suffix != ".stl":
                        original = staging / f"model{suffix}"
                        atomic_write_bytes(original, raw, mode=0o644)
                        artifacts[f"model{suffix}"] = original

                    fit_info: dict[str, Any] = {}
                    if fit_requested:
                        raw_stl = staging / "source.stl"
                        shutil.copyfile(stl_path, raw_stl)
                        raw_bytes = stl_path.stat().st_size
                        fit_dir = staging / "fit"
                        fit_dir.mkdir(parents=True, exist_ok=True)
                        fit_result = run_blender(
                            fit_script(
                                stl_path,
                                exact,
                                int(max_triangles),
                                max_dimension_mm=uniform,
                            ),
                            workdir=fit_dir,
                            image=image_path,
                            blender_path=rt.loaded.config.render.blender_path,
                            timeout_s=float(rt.loaded.config.render.timeout_s),
                        )
                        fitted = fit_result.artifacts.get("model.stl")
                        if not fit_result.ok or not fitted or not Path(fitted).is_file():
                            raise AdapterUnavailable(
                                f"{provider} generated a mesh, but the size fit failed",
                                hint="The raw mesh is retained in the job cache; retry with mesh_fit.",
                                details={"stderr_tail": fit_result.stderr[-1200:]},
                            )
                        shutil.copyfile(fitted, stl_path)
                        artifacts["source.stl"] = raw_stl
                        fit_info = {
                            "mode": "uniform" if uniform else "exact",
                            "requested_size_mm": list(exact) if exact else None,
                            "max_dimension_mm": uniform or None,
                            "max_triangles": int(max_triangles),
                            "source_bytes": raw_bytes,
                            "checks": fit_result.checks,
                        }

                    checks = stats_for_stl(stl_path)
                    checks["engine"] = f"cloud:{provider}"
                    checks["provider_task"] = task_id
                    if fit_info:
                        checks["fit"] = fit_info
                    assets = AssetStore(rt.workspace)
                    manifest = assets.create(name, kind="cloud")
                    record = assets.add_revision(
                        manifest["asset_id"],
                        "# cloud generator: no script is produced\n",
                        prompt=prompt,
                        model=f"cloud:{provider}",
                        modeler="cloud",
                        usage={
                            "provider": provider,
                            "task_id": task_id,
                            "credits": task.credits,
                        },
                        checks=checks,
                        reference=image_path,
                        reference_meta=(
                            {"source_path": str(image_path), "mode": mode} if image_path else None
                        ),
                        artifacts=artifacts,
                        kind="cloud",
                    )
                    rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.stl")
                    viewer = _asset_viewer(
                        rt,
                        assets,
                        assets.load(manifest["asset_id"]),
                        selected=record["revision"],
                    )
                    return {
                        "asset_id": manifest["asset_id"],
                        "name": name,
                        "revision": record["revision"],
                        "provider": provider,
                        "mode": mode,
                        "task_id": task_id,
                        "checks": checks,
                        "artifacts": sorted(record.get("artifacts", {})),
                        "fit": fit_info or None,
                        **viewer,
                    }
                finally:
                    client.close()

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "cloud.generate",
                work,
                params={
                    "provider": provider,
                    "name": name,
                    "mode": mode,
                    "fit": fit_requested,
                },
                provider=provider,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "provider": provider,
                "adapter": adapter,
                "mode": mode,
                "fit_requested": fit_requested,
                "timeout_s": budget,
                "hint": "Poll job_status(job_id); on success open result.viewer_file or call model_preview(asset_id).",
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="generate_local",
        description=(
            "Run a user-installed local 3D generator in the background. The provider "
            "is configured in providers.local.<id> as a command + args template with "
            "{prompt}, {image}, {outdir}, {name} placeholders; it must write "
            '<outdir>/mesh.stl (or print {"mesh": path}). Print3D creates the asset '
            "from the produced STL, records real geometry checks and returns a "
            "job_id: poll job_status until succeeded, then call model_preview. "
            "No weights are downloaded and no cloud API is called. WRITES files and "
            "runs the provider command: confirm must be exactly 'LOCAL <name>'."
        ),
    )
    def generate_local(
        provider: str,
        name: str,
        confirm: str,
        prompt: str = "",
        image: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"LOCAL {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not prompt.strip() and not image:
                raise InvalidArgument(
                    "Provide prompt, image, or both",
                    hint="Local providers are text-to-3D, image-to-3D, or both.",
                )

            entry = rt.loaded.config.providers.local.get(provider)
            if entry is None:
                raise InvalidArgument(
                    f"Unknown local provider: {provider}",
                    hint=(
                        "Add providers.local.<id> to config.json with command/args; "
                        "run `print3d config` to see the current file."
                    ),
                    details={"configured": sorted(rt.loaded.config.providers.local)},
                )
            if not entry.get("enabled", True):
                raise AdapterUnavailable(
                    f"Local provider {provider!r} is disabled",
                    hint="Set providers.local.<id>.enabled to true.",
                )

            mode = "both" if prompt.strip() and image else ("image" if image else "text")
            declared = str(entry.get("mode") or "both")
            if declared not in {"text", "image", "both"}:
                raise ConfigError(
                    f"providers.local.{provider}.mode must be text, image or both",
                    hint="Fix config.json and retry.",
                )
            if declared != "both" and declared != mode:
                raise InvalidArgument(
                    f"Provider {provider!r} only supports {declared}-to-3D",
                    hint=f"Pass {'prompt' if declared == 'text' else 'image'} instead.",
                )

            image_path: Path | None = None
            if image:
                image_path = Path(image).expanduser().resolve()
                if not image_path.is_file():
                    raise NotFound(
                        f"Image not found: {image_path}",
                        hint="Import the picture first (reference_import) or pass a valid path.",
                    )
                if not sniff_image_mime(image_path.read_bytes()):
                    raise InvalidArgument(
                        f"Not a PNG/JPEG/WebP image: {image_path.name}",
                        hint="Convert the reference to PNG or JPEG first.",
                    )

            budget = float(timeout_s or entry.get("timeout_s") or LOCAL_GENERATION_TIMEOUT)
            if budget <= 0:
                raise InvalidArgument("timeout must be positive")

            def work(job_id: str) -> dict[str, Any]:
                outdir = rt.workspace.safe_path(f"cache/local/{job_id}", create_parents=True)
                result = run_local(
                    provider,
                    entry,
                    prompt=prompt,
                    image=image_path,
                    outdir=outdir,
                    name=name,
                    mode=mode,
                    timeout_s=budget,
                )
                if not result.ok or not result.mesh:
                    raise AdapterUnavailable(
                        result.error or f"Local provider {provider!r} failed",
                        hint=result.hint,
                        details={
                            "provider": provider,
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                            "stdout_tail": result.stdout[-1500:],
                            "stderr_tail": result.stderr[-1500:],
                        },
                    )
                checks = stats_for_stl(result.mesh)
                checks["engine"] = f"local:{provider}"
                assets = AssetStore(rt.workspace)
                manifest = assets.create(name, kind="local")
                record = assets.add_revision(
                    manifest["asset_id"],
                    "# local generator: no script is produced\n",
                    prompt=prompt,
                    model=f"local:{provider}",
                    modeler="local",
                    usage={"command": result.command, "duration_s": round(result.duration_s, 2)},
                    checks=checks,
                    reference=image_path,
                    reference_meta=(
                        {"source_path": str(image_path), "mode": mode} if image_path else None
                    ),
                    artifacts={"model.stl": Path(result.mesh)},
                    kind="local",
                )
                rt.record_write(f"assets/{manifest['asset_id']}/{record['revision']}/model.stl")
                viewer = _asset_viewer(
                    rt,
                    assets,
                    assets.load(manifest["asset_id"]),
                    selected=record["revision"],
                )
                return {
                    "asset_id": manifest["asset_id"],
                    "name": name,
                    "revision": record["revision"],
                    "provider": provider,
                    "mode": mode,
                    "duration_s": round(result.duration_s, 2),
                    "checks": checks,
                    "artifacts": sorted(record.get("artifacts", {})),
                    **viewer,
                }

            runner = rt.runner or JobRunner(rt.store)
            job = runner.submit(
                "local.generate",
                work,
                params={"provider": provider, "name": name, "mode": mode},
                provider=provider,
            )
            return {
                "ok": True,
                "writes_performed": True,
                "job_id": job.id,
                "provider": provider,
                "mode": mode,
                "timeout_s": budget,
                "hint": "Poll job_status(job_id); on success open result.viewer_file or call model_preview(asset_id).",
            }
        except Print3DError as exc:
            return tool_error(exc)
