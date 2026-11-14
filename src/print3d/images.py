"""Concept-image generation and storage.

The ``text -> image -> model`` path starts with a concept image produced by any
OpenAI-compatible ``POST /images/generations`` endpoint.  Generated images live
in the workspace as *references*::

    references/<reference_id>/image.png
    references/<reference_id>/meta.json

A reference is never a model: it is visual context that ``model_write`` copies
next to the script, and that ``model_preview`` can return beside the render so
the client model can compare intent with the real geometry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import AdapterUnavailable, InvalidArgument, NotFound
from .workspace import Workspace, atomic_write_bytes, atomic_write_text

MAX_IMAGE_BYTES = 12 * 1024 * 1024
REFERENCE_ID_RE = "r_[A-Za-z0-9_]{6,64}"

MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
SUFFIX_MIME = {suffix: mime for mime, suffix in MIME_SUFFIX.items()}


def sniff_image_mime(data: bytes) -> str | None:
    """Return the image MIME type from magic bytes, or ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(slots=True)
class GeneratedImage:
    """One image returned by a provider."""

    data: bytes
    provider: str
    model: str
    size: str
    prompt: str
    elapsed_s: float = 0.0
    revised_prompt: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def mime(self) -> str:
        return sniff_image_mime(self.data) or "image/png"


class ImageGenerator:
    """Minimal client for OpenAI-compatible image generation endpoints."""

    adapter = "openai"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        timeout_s: float = 120.0,
        provider_id: str = "openai",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise AdapterUnavailable(
                "No API key for the image provider",
                hint=(
                    "Set OPENAI_API_KEY (or the variable named by images.<id>.api_key_env), "
                    "or add the key to secrets.json under providers."
                ),
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or "https://api.openai.com/v1"
        self.model = model
        self.size = size
        self.timeout_s = timeout_s
        self.provider_id = provider_id
        self.transport = transport

    # -- HTTP ------------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout_s, connect=min(15.0, self.timeout_s)),
            transport=self.transport,
            follow_redirects=True,
        )

    def generate(self, prompt: str, *, size: str | None = None) -> GeneratedImage:
        if not isinstance(prompt, str) or not prompt.strip():
            raise InvalidArgument(
                "prompt must not be empty",
                hint="Describe the product, its style and the material in a sentence or two.",
            )
        wanted_size = (size or self.size or "").strip() or "1024x1024"
        payload = {
            "model": self.model,
            "prompt": prompt.strip(),
            "size": wanted_size,
            "n": 1,
            "response_format": "b64_json",
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        started = time.monotonic()
        with self._client() as client:
            try:
                response = client.post(
                    f"{self.base_url}/images/generations",
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException:
                raise AdapterUnavailable(
                    f"Image generation timed out after {self.timeout_s:g}s",
                    hint="Retry, or raise images.<id>.timeout_s in config.json.",
                ) from None
            except httpx.HTTPError as exc:
                raise AdapterUnavailable(
                    "Could not reach the image provider",
                    details={"error": str(exc)},
                ) from None

            data = self._decode(response)
            items = data.get("data")
            if not isinstance(items, list) or not items:
                raise AdapterUnavailable(
                    "The image provider returned no images",
                    details={"response": response.text[:600]},
                )
            item = items[0] if isinstance(items[0], dict) else {}
            raw = self._fetch_bytes(client, item)

        if not raw:
            raise AdapterUnavailable("The image provider returned an empty image")
        if len(raw) > MAX_IMAGE_BYTES:
            raise AdapterUnavailable(
                f"The generated image is too large ({len(raw)} bytes)",
                hint="Ask for a smaller size, e.g. size='1024x1024'.",
            )
        if sniff_image_mime(raw) is None:
            raise AdapterUnavailable(
                "The image provider returned data that is not PNG/JPEG/WebP",
                details={"first_bytes": raw[:16].hex()},
            )
        return GeneratedImage(
            data=raw,
            provider=self.provider_id,
            model=str(data.get("model") or self.model),
            size=wanted_size,
            prompt=prompt.strip(),
            elapsed_s=round(time.monotonic() - started, 2),
            revised_prompt=item.get("revised_prompt"),
        )

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in (401, 403):
            raise AdapterUnavailable(
                "The image provider rejected the API key",
                hint="Check images.<id>.api_key_env and the key value; nothing was stored.",
                details={"status": response.status_code},
            )
        if response.status_code == 429:
            raise AdapterUnavailable(
                "The image provider rate limit was reached",
                hint="Wait a moment and retry.",
                details={"status": 429},
            )
        if response.status_code >= 400:
            raise AdapterUnavailable(
                f"Image generation failed (HTTP {response.status_code})",
                details={"status": response.status_code, "body": response.text[:600]},
            )
        try:
            data = response.json()
        except ValueError:
            raise AdapterUnavailable(
                "The image provider returned non-JSON output",
                details={"body": response.text[:600]},
            ) from None
        if not isinstance(data, dict):
            raise AdapterUnavailable(
                "The image provider returned an unexpected payload",
                details={"body": response.text[:600]},
            )
        return data

    def _fetch_bytes(self, client: httpx.Client, item: dict[str, Any]) -> bytes:
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded.strip():
            try:
                return base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError):
                raise AdapterUnavailable(
                    "The image provider returned invalid base64 data"
                ) from None
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            parsed = urlparse(url.strip())
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise AdapterUnavailable(
                    "The image provider returned a non-HTTP image URL",
                    details={"scheme": parsed.scheme},
                )
            # Never forward the provider key to a third-party CDN.
            chunks: list[bytes] = []
            try:
                with client.stream("GET", url.strip()) as download:
                    if download.status_code >= 400:
                        raise AdapterUnavailable(
                            f"Could not download the generated image (HTTP {download.status_code})"
                        )
                    total = 0
                    for chunk in download.iter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise AdapterUnavailable(
                                f"The generated image is larger than {MAX_IMAGE_BYTES} bytes"
                            )
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise AdapterUnavailable(
                    "Could not download the generated image",
                    details={"error": str(exc)},
                ) from None
            return b"".join(chunks)
        raise AdapterUnavailable(
            "The image provider returned neither b64_json nor url",
            details={"keys": sorted(item)},
        )


def generator_from_config(
    settings: dict[str, Any],
    *,
    api_key: str | None,
    provider_id: str = "openai",
    transport: httpx.BaseTransport | None = None,
) -> ImageGenerator:
    """Registry factory: build an :class:`ImageGenerator` from config."""

    return ImageGenerator(
        api_key,
        base_url=str(settings.get("base_url") or "https://api.openai.com/v1"),
        model=str(settings.get("model") or "gpt-image-1"),
        size=str(settings.get("size") or "1024x1024"),
        timeout_s=float(settings.get("timeout_s") or 120.0),
        provider_id=provider_id,
        transport=transport,
    )


def new_reference_id(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"r_{moment}_{secrets.token_hex(3)}"


def validate_reference_id(reference_id: str) -> str:
    if not isinstance(reference_id, str) or not re.match(f"^{REFERENCE_ID_RE}$", reference_id):
        raise InvalidArgument(
            f"Invalid reference id: {reference_id!r}",
            hint="Reference ids look like r_20260101T000000Z_ab12cd.",
        )
    return reference_id


class ReferenceStore:
    """Versioned concept images inside the workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def directory(self, reference_id: str) -> Path:
        return self.workspace.references_dir / validate_reference_id(reference_id)

    def create(
        self,
        image: GeneratedImage,
        *,
        prompt: str,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        reference_id = new_reference_id()
        directory = self.directory(reference_id)
        directory.mkdir(parents=True, exist_ok=False)
        suffix = MIME_SUFFIX.get(image.mime, ".png")
        file_name = f"image{suffix}"
        atomic_write_bytes(directory / file_name, image.data, mode=0o644)
        meta = {
            "reference_id": reference_id,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "prompt": prompt,
            "provider": provider,
            "model": model,
            "size": image.size,
            "file": file_name,
            "bytes": len(image.data),
            "sha256": image.sha256,
            "elapsed_s": image.elapsed_s,
            "revised_prompt": image.revised_prompt,
        }
        atomic_write_text(
            directory / "meta.json",
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            mode=0o644,
        )
        return meta

    def load(self, reference_id: str) -> dict[str, Any]:
        path = self.directory(reference_id) / "meta.json"
        if not path.is_file():
            raise NotFound(
                f"Unknown reference image: {reference_id}",
                hint="Call image_generate first, or pass a path under references/.",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def image_path(self, reference_id: str) -> Path:
        meta = self.load(reference_id)
        path = self.directory(reference_id) / str(meta.get("file") or "image.png")
        if not path.is_file():
            raise NotFound(
                f"Reference {reference_id} has no image file",
                hint="Regenerate it with image_generate.",
            )
        return path


__all__ = [
    "GeneratedImage",
    "ImageGenerator",
    "MAX_IMAGE_BYTES",
    "MIME_SUFFIX",
    "ReferenceStore",
    "SUFFIX_MIME",
    "generator_from_config",
    "new_reference_id",
    "sniff_image_mime",
    "validate_reference_id",
]
