"""MCP tools: images."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..assets import validate_name
from ..errors import (
    AdapterUnavailable,
    ConfirmationMismatch,
    InvalidArgument,
    NotFound,
    Print3DError,
)
from ..images import MAX_IMAGE_BYTES, GeneratedImage, ReferenceStore, sniff_image_mime
from ..runtime import Runtime, tool_error
from .common import (
    _image_result,
    _image_settings,
)


def register(server: FastMCP, rt: Runtime) -> None:
    @server.tool(
        name="reference_import",
        description=(
            "Import an image the user already has (a sketch, a photo, a screenshot) "
            "into the workspace as a concept reference, so it can be attached to a "
            "model revision with model_write(reference=...). This is the image-input "
            "half of the text/image -> model path: the client model looks at the "
            "image and writes the script; 3DPrint keeps the image as provenance. "
            "WRITES files: confirm must be exactly 'IMPORT <name>'."
        ),
    )
    def reference_import(path: str, name: str, confirm: str) -> dict[str, Any]:
        try:
            name = validate_name(name)
            expected = f"IMPORT {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            source = Path(path).expanduser()
            if not source.is_absolute():
                source = Path.cwd() / source
            if not source.is_file():
                raise NotFound(
                    f"Image not found: {source}",
                    hint="Pass an absolute path, or a path relative to the server's cwd.",
                )
            data = source.read_bytes()
            if not data:
                raise InvalidArgument("The image file is empty")
            if len(data) > MAX_IMAGE_BYTES:
                raise InvalidArgument(
                    f"The image is too large ({len(data)} bytes, limit {MAX_IMAGE_BYTES})",
                    hint="Resize it first.",
                )
            mime = sniff_image_mime(data[:32])
            if mime is None:
                raise InvalidArgument(
                    "The file is not a PNG, JPEG or WebP image",
                    details={"path": str(source)},
                )

            image = GeneratedImage(
                data=data,
                provider="local",
                model="upload",
                size="",
                prompt=f"imported from {source.name}",
            )
            store = ReferenceStore(rt.workspace)
            meta = store.create(
                image,
                prompt=f"imported from {source.name}",
                provider="local",
                model="upload",
            )
            relative = rt.workspace.rel(store.directory(meta["reference_id"]) / meta["file"])
            rt.record_write(relative)
            return {
                "ok": True,
                "writes_performed": True,
                "reference_id": meta["reference_id"],
                "image_path": relative,
                "name": name,
                "source_path": str(source),
                "mime": mime,
                "bytes": meta["bytes"],
                "sha256": meta["sha256"],
                "hint": (
                    f"Now call model_write with reference={relative!r} and confirm='WRITE {name}'."
                ),
            }
        except Print3DError as exc:
            return tool_error(exc)

    @server.tool(
        name="image_generate",
        description=(
            "Generate a concept image from a text prompt and store it as a reference "
            "in the workspace (references/<id>/). This is step one of the "
            "text -> image -> model path: pass the returned image_path to model_write "
            "as `reference`, then build and preview the model. Any OpenAI-compatible "
            "images endpoint works (config.images.<id>). WRITES files and calls a paid "
            "service: confirm must be exactly 'IMAGE <name>'. The image is also "
            "returned inline so you can see it."
        ),
    )
    def image_generate(
        prompt: str,
        name: str,
        confirm: str,
        provider: str = "",
        size: str = "",
    ) -> Any:
        try:
            name = validate_name(name)
            expected = f"IMAGE {name}"
            if confirm != expected:
                raise ConfirmationMismatch(
                    f"confirm must be exactly {expected!r}",
                    details={"expected": expected},
                )
            if not isinstance(prompt, str) or not prompt.strip():
                raise InvalidArgument(
                    "prompt must not be empty",
                    hint="Describe the product, style, material and the key features.",
                )

            provider_id, settings = _image_settings(rt, provider)
            key = rt.secrets.provider_key(provider_id, api_key_env=settings.api_key_env)
            if not key:
                raise AdapterUnavailable(
                    f"No API key for image provider {provider_id!r}",
                    hint=(
                        f"Set {settings.api_key_env} (or images.{provider_id}.api_key_env), "
                        "or put the key in secrets.json under providers."
                    ),
                    details={"provider": provider_id, "env": settings.api_key_env},
                )

            adapter = settings.adapter or provider_id
            generator = rt.registry.create(
                "image",
                adapter,
                settings=settings.model_dump(),
                api_key=key,
                provider_id=provider_id,
            )
            image = generator.generate(prompt, size=size or settings.size)

            store = ReferenceStore(rt.workspace)
            meta = store.create(
                image,
                prompt=prompt.strip(),
                provider=provider_id,
                model=settings.model,
            )
            image_path = store.directory(meta["reference_id"]) / meta["file"]
            relative = rt.workspace.rel(image_path)
            rt.record_write(relative)

            payload = {
                "ok": True,
                "writes_performed": True,
                "reference_id": meta["reference_id"],
                "image_path": relative,
                "name": name,
                "provider": provider_id,
                "model": meta["model"],
                "size": meta["size"],
                "bytes": meta["bytes"],
                "sha256": meta["sha256"],
                "elapsed_s": meta["elapsed_s"],
                "prompt": meta["prompt"],
                "hint": (
                    f"Now write the build123d script and call model_write with "
                    f"reference={relative!r} and confirm='WRITE {name}'."
                ),
            }
            return _image_result(payload, [(image.data, image.mime, None)])
        except Print3DError as exc:
            return tool_error(exc)
