"""Cloud image-to-3D / text-to-3D providers.

Three adapters are built in:

* ``tripo``          — Tripo V3 API (``https://openapi.tripo3d.com/v3``), Bearer key.
* ``hunyuan_openai`` — Tencent Hunyuan 3D OpenAI-compatible API
  (``https://api.ai3d.cloud.tencent.com``), plain API-key Authorization.
* ``hunyuan``        — Tencent Cloud Hunyuan 3D Pro (``ai3d.tencentcloudapi.com``,
  TC3-HMAC-SHA256 signed with SecretId/SecretKey).

All are asynchronous: :meth:`submit` returns a provider task id and
:meth:`poll` reports status/progress/result URLs.  Polling and downloading
belong to the caller (the MCP server drives them inside a job).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .errors import AdapterUnavailable, AdapterUnknown, InvalidArgument

MAX_MODEL_BYTES = 150 * 1024 * 1024
MAX_REDIRECTS = 3
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
SUCCESS = "success"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = {SUCCESS, FAILED, CANCELLED}


@dataclass(slots=True)
class CloudTask:
    """Provider-neutral snapshot of an asynchronous generation task."""

    task_id: str
    status: str
    progress: int = 0
    model_url: str | None = None
    preview_url: str | None = None
    error: str | None = None
    credits: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "model_url": self.model_url,
            "preview_url": self.preview_url,
            "error": self.error,
        }


def _http_error(provider: str, response: httpx.Response) -> AdapterUnavailable:
    if response.status_code in (401, 403):
        message = f"{provider} rejected the API key"
        hint = "Check the provider key (capabilities shows only whether it is set)."
    elif response.status_code == 429:
        message = f"{provider} rate limit reached"
        hint = "Wait and retry, or lower the concurrency of generation jobs."
    else:
        message = f"{provider} returned HTTP {response.status_code}"
        hint = "See details for the provider response body."
    body = response.text[:800]
    return AdapterUnavailable(message, hint=hint, details={"body": body})


def envelope_data(payload: Any, *, provider: str) -> dict[str, Any]:
    """Unwrap a ``{"code": 0, "data": {...}}`` response (Tripo style)."""

    if not isinstance(payload, dict):
        raise AdapterUnavailable(f"{provider} returned an unexpected response")
    code = payload.get("code")
    if code not in (0, None):
        raise AdapterUnavailable(
            f"{provider} refused the request (code {code})",
            hint="Check the request parameters and the provider dashboard.",
            details={"body": payload},
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AdapterUnavailable(f"{provider} returned no data object")
    return data


class TripoCloud:
    """Tripo V3: upload → /generation/{text,image}-to-model → /tasks/{id}."""

    id = "tripo"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://openapi.tripo3d.com/v3",
        model: str = "v3.1-20260211",
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise AdapterUnavailable(
                "No API key for the Tripo provider",
                hint="Set TRIPO_API_KEY (or providers.cloud.tripo.api_key_env).",
            )
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.post(path, **kwargs)
        if response.status_code >= 400:
            raise _http_error("Tripo", response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterUnavailable("Tripo returned non-JSON output") from exc
        return envelope_data(payload, provider="Tripo")

    def upload(self, data: bytes, mime: str = "image/png") -> str:
        suffix = {".png": "png", ".jpg": "jpg", ".webp": "webp"}.get(_suffix_for_mime(mime), "png")
        files = {"file": (f"image.{suffix}", data, mime)}
        data_obj = self._post("/files", files=files)
        token = data_obj.get("file_token")
        if not isinstance(token, str) or not token:
            raise AdapterUnavailable("Tripo upload returned no file token")
        return token

    def submit(self, *, prompt: str | None = None, image: bytes | None = None) -> str:
        payload: dict[str, Any] = {"model": self.model}
        if image is not None:
            payload["input"] = self.upload(image)
            path = "/generation/image-to-model"
        elif prompt:
            payload["prompt"] = prompt
            path = "/generation/text-to-model"
        else:
            raise InvalidArgument("Tripo needs a prompt or an image")
        data = self._post(path, json=payload)
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AdapterUnavailable("Tripo did not return a task_id")
        return task_id

    def poll(self, task_id: str) -> CloudTask:
        response = self._client.get(f"/tasks/{task_id}")
        if response.status_code >= 400:
            raise _http_error("Tripo", response)
        data = envelope_data(response.json(), provider="Tripo")
        status = str(data.get("status") or "unknown")
        output = data.get("output") or {}
        task = CloudTask(
            task_id=task_id,
            status=status,
            progress=int(data.get("progress") or 0),
            model_url=output.get("model_url"),
            preview_url=output.get("rendered_image_url"),
            error=data.get("error_message") or None,
            credits=data.get("credits_consumed"),
            raw=data,
        )
        if status == "failed" and not task.error:
            task.error = f"Tripo task failed (code {data.get('error_code')})"
        return task


def _suffix_for_mime(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(mime, ".png")


class HunyuanCloud:
    """Tencent Cloud Hunyuan 3D Pro, signed with TC3-HMAC-SHA256."""

    id = "hunyuan"
    host = "ai3d.tencentcloudapi.com"
    version = "2025-05-13"

    def __init__(
        self,
        secret_id: str | None,
        secret_key: str | None,
        *,
        region: str = "ap-guangzhou",
        model: str = "3.0",
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not secret_id or not secret_key:
            raise AdapterUnavailable(
                "No Tencent Cloud credentials for the Hunyuan provider",
                hint=(
                    "Set TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY "
                    "(or providers.cloud.hunyuan.secret_id_env / secret_key_env)."
                ),
            )
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.model = model
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _signed_headers(self, action: str, body: str) -> dict[str, str]:
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
        content_type = "application/json; charset=utf-8"
        canonical_headers = (
            f"content-type:{content_type}\nhost:{self.host}\nx-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
        )

        scope = f"{date}/ai3d/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        def _hmac(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = _hmac(secret_date, "ai3d")
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "Content-Type": content_type,
            "Host": self.host,
            "X-TC-Action": action,
            "X-TC-Version": self.version,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": self.region,
        }

    def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        response = self._client.post(
            f"https://{self.host}", content=body, headers=self._signed_headers(action, body)
        )
        if response.status_code >= 400:
            raise _http_error("Hunyuan 3D", response)
        try:
            envelope = response.json()
        except ValueError as exc:
            raise AdapterUnavailable("Hunyuan 3D returned non-JSON output") from exc
        data = envelope.get("Response") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise AdapterUnavailable("Hunyuan 3D returned an unexpected response")
        error = data.get("Error")
        if isinstance(error, dict):
            raise AdapterUnavailable(
                f"Hunyuan 3D error: {error.get('Message') or error.get('Code')}",
                hint="Check the Tencent Cloud credentials, region and quota.",
                details={"code": error.get("Code")},
            )
        return data

    def submit(
        self,
        *,
        prompt: str | None = None,
        image: bytes | None = None,
        timeout_hint: str = "",
    ) -> str:
        payload: dict[str, Any] = {
            "Model": self.model,
            "ResultFormat": "STL",
            "GenerateType": "Normal",
        }
        if image is not None:
            payload["ImageBase64"] = base64.b64encode(image).decode("ascii")
        elif prompt:
            payload["Prompt"] = prompt
        else:
            raise InvalidArgument("Hunyuan 3D needs a prompt or an image")
        data = self._call("SubmitHunyuanTo3DProJob", payload)
        job_id = data.get("JobId")
        if not isinstance(job_id, str) or not job_id:
            raise AdapterUnavailable("Hunyuan 3D did not return a JobId")
        return job_id

    def poll(self, task_id: str) -> CloudTask:
        data = self._call("QueryHunyuanTo3DProJob", {"JobId": task_id})
        status_raw = str(data.get("Status") or "WAIT").upper()
        status = {"WAIT": "queued", "RUN": "running", "DONE": "success", "FAIL": "failed"}.get(
            status_raw, status_raw.lower()
        )
        files = data.get("ResultFile3Ds") or []
        model_url = None
        preview_url = None
        for item in files:
            if not isinstance(item, dict):
                continue
            if item.get("PreviewImageUrl") and not preview_url:
                preview_url = item["PreviewImageUrl"]
            url = item.get("Url")
            kind = str(item.get("Type") or "").upper()
            if url and kind in {"STL", "OBJ", "GLB", "FBX"} and not model_url:
                model_url = url
        return CloudTask(
            task_id=task_id,
            status=status,
            progress=100 if status == SUCCESS else 50 if status == "running" else 0,
            model_url=model_url,
            preview_url=preview_url,
            error=data.get("ErrorMessage") or None,
            raw=data,
        )


def _hunyuan_openai_data(payload: Any) -> dict[str, Any]:
    """Unwrap the Hunyuan OpenAI-compatible response and surface API errors."""

    if not isinstance(payload, dict):
        raise AdapterUnavailable("Hunyuan 3D returned an unexpected response")
    nested = payload.get("Response")
    data = nested if isinstance(nested, dict) else payload
    error = data.get("Error")
    if isinstance(error, dict):
        message = error.get("Message") or error.get("Code") or "no detail"
        raise AdapterUnavailable(
            f"Hunyuan 3D error: {message}",
            hint="Check the API key, request parameters and the provider quota.",
            details={"code": error.get("Code")},
        )
    if isinstance(error, str) and error:
        raise AdapterUnavailable(f"Hunyuan 3D error: {error}")
    # The endpoint also reports application errors as {Type, Code, Msg} inside
    # a 2xx response.  Successful submit/query bodies carry JobId or Status.
    code = data.get("Code")
    kind = data.get("Type")
    if kind not in (None, 0, "0") or (
        code not in (None, 0, "0") and not data.get("JobId") and not data.get("Status")
    ):
        message = data.get("Msg") or data.get("Message") or "no detail"
        raise AdapterUnavailable(
            f"Hunyuan 3D error {code or kind}: {message}",
            hint="Check the API key, request parameters and the provider quota.",
            details={"code": code, "type": kind},
        )
    return data


class HunyuanOpenAICloud:
    """Hunyuan 3D through Tencent's OpenAI-compatible submit/query endpoints."""

    id = "hunyuan_openai"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://api.ai3d.cloud.tencent.com",
        model: str = "3.0",
        face_count: int | None = None,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise AdapterUnavailable(
                "No API key for the Hunyuan OpenAI-compatible provider",
                hint="Set HUNYUAN3D_API_KEY (or providers.cloud.hunyuan.api_key_env).",
            )
        key = api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if face_count is not None and not 3000 <= face_count <= 1_500_000:
            raise InvalidArgument("Hunyuan FaceCount must be between 3000 and 1500000")
        self.model = model
        self.face_count = face_count
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": key},
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=payload)
        if response.status_code >= 400:
            raise _http_error("Hunyuan 3D", response)
        try:
            return _hunyuan_openai_data(response.json())
        except ValueError as exc:
            raise AdapterUnavailable("Hunyuan 3D returned non-JSON output") from exc

    def submit(self, *, prompt: str | None = None, image: bytes | None = None) -> str:
        payload: dict[str, Any] = {
            "Model": self.model,
            "GenerateType": "Normal",
            "ResultFormat": "STL",
        }
        if image is not None:
            payload["ImageBase64"] = base64.b64encode(image).decode("ascii")
        elif prompt:
            payload["Prompt"] = prompt
        else:
            raise InvalidArgument("Hunyuan 3D needs a prompt or an image")
        if self.face_count is not None:
            payload["FaceCount"] = self.face_count
        data = self._post("/v1/ai3d/submit", payload)
        job_id = data.get("JobId") or data.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise AdapterUnavailable("Hunyuan 3D did not return a JobId")
        return job_id

    def poll(self, task_id: str) -> CloudTask:
        data = self._post("/v1/ai3d/query", {"JobId": task_id})
        status_raw = str(data.get("Status") or "WAIT").upper()
        status = {"WAIT": "queued", "RUN": "running", "DONE": "success", "FAIL": "failed"}.get(
            status_raw, status_raw.lower()
        )
        files = data.get("ResultFile3Ds") or []
        best: tuple[int, str, str] | None = None
        preview_url = None
        preference = {"STL": 0, "GLB": 1, "OBJ": 2, "FBX": 3}
        for item in files:
            if not isinstance(item, dict):
                continue
            if item.get("PreviewImageUrl") and not preview_url:
                preview_url = item["PreviewImageUrl"]
            url = item.get("Url")
            kind = str(item.get("Type") or "").upper()
            if not url or kind not in preference:
                continue
            candidate = (preference[kind], str(url), kind)
            if best is None or candidate[0] < best[0]:
                best = candidate
        credits = data.get("ResultCreditConsumed")
        return CloudTask(
            task_id=task_id,
            status=status,
            progress=100 if status == SUCCESS else 50 if status == "running" else 0,
            model_url=best[1] if best else None,
            preview_url=preview_url,
            error=data.get("ErrorMessage") or data.get("ErrorMsg") or None,
            credits=float(credits) if isinstance(credits, (int, float)) else None,
            raw=data,
        )


def build_provider(
    provider_id: str,
    entry: dict[str, Any],
    *,
    api_key: str | None = None,
    secret_id: str | None = None,
    secret_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
):
    """Build a cloud adapter from a ``providers.cloud.<id>`` config entry."""

    adapter = str(entry.get("adapter") or provider_id).strip().lower().replace("-", "_")
    timeout_s = float(entry.get("timeout_s") or 120.0)
    if adapter == "tripo":
        return TripoCloud(
            api_key,
            base_url=str(entry.get("base_url") or "https://openapi.tripo3d.com/v3"),
            model=str(entry.get("model") or "v3.1-20260211"),
            timeout_s=timeout_s,
            transport=transport,
        )
    if adapter == "hunyuan_openai":
        raw_faces = entry.get("face_count")
        try:
            face_count = int(raw_faces) if raw_faces not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise InvalidArgument(
                f"providers.cloud.{provider_id}.face_count must be an integer"
            ) from exc
        return HunyuanOpenAICloud(
            api_key,
            base_url=str(entry.get("base_url") or "https://api.ai3d.cloud.tencent.com"),
            model=str(entry.get("model") or "3.0"),
            face_count=face_count,
            timeout_s=timeout_s,
            transport=transport,
        )
    if adapter == "hunyuan":
        return HunyuanCloud(
            secret_id,
            secret_key,
            region=str(entry.get("region") or "ap-guangzhou"),
            model=str(entry.get("model") or "3.0"),
            timeout_s=timeout_s,
            transport=transport,
        )
    raise AdapterUnknown(
        f"Unknown cloud adapter: {adapter}",
        hint=(
            "Use adapter tripo, hunyuan_openai (API key) or hunyuan "
            "(Tencent Cloud TC3 signature) in providers.cloud.<id>."
        ),
        details={"provider": provider_id},
    )


def _checked_url(url: str) -> str:
    """Reject anything that is not a plain http(s) URL with a host."""

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise InvalidArgument(
            "The provider returned a model URL that is not http(s)",
            details={"scheme": parsed.scheme},
        )
    return url


def download_model(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    max_bytes: int = MAX_MODEL_BYTES,
) -> tuple[bytes, str]:
    """Download a generated model; returns ``(bytes, suffix)``.

    Redirects are followed by hand (max :data:`MAX_REDIRECTS`), each hop is
    re-validated as http(s) with a host, and the body is read in chunks so a
    hostile or broken provider cannot make 3DPrint buffer gigabytes.
    """

    if not url:
        raise InvalidArgument("The provider did not return a model URL")
    current = _checked_url(url)
    content_type = ""
    data = b""
    with httpx.Client(timeout=120.0, transport=transport, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "")
                    if not location:
                        raise AdapterUnavailable(
                            "The provider redirect did not include a Location header"
                        )
                    current = _checked_url(urljoin(str(response.url), location))
                    continue
                if response.status_code >= 400:
                    raise AdapterUnavailable(
                        f"Could not download the generated model (HTTP {response.status_code})"
                    )
                content_type = response.headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise AdapterUnavailable(
                            f"The generated model is larger than {max_bytes} bytes"
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                break
        else:
            raise AdapterUnavailable("The model URL redirected too many times")
    if not data:
        raise AdapterUnavailable("The provider returned an empty model file")
    return data, _model_suffix(current, content_type)


def _model_suffix(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in (".stl", ".glb", ".gltf", ".obj", ".fbx", ".usdz"):
        if path.endswith(suffix):
            return suffix
    if "stl" in content_type:
        return ".stl"
    if "gltf" in content_type or "octet" in content_type:
        return ".glb"
    return ".glb"


def to_stl(data: bytes, suffix: str, *, scale: float = 1.0) -> bytes:
    """Convert a provider mesh to binary STL, applying ``scale`` in all cases."""

    suffix = suffix.lower()
    if scale <= 0:
        raise InvalidArgument("scale must be positive")
    if suffix == ".stl" and scale == 1.0:
        return data
    try:
        import trimesh
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AdapterUnavailable(
            "trimesh is required to convert non-STL models",
            hint="pip install trimesh, or request STL output from the provider.",
        ) from exc

    loaded = trimesh.load(io.BytesIO(data), file_type=suffix.lstrip("."))
    mesh = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    if mesh is None or getattr(mesh, "vertices", None) is None or len(mesh.vertices) == 0:
        raise AdapterUnavailable("The generated file contains no mesh data")
    mesh = mesh.copy()
    if scale != 1.0:
        mesh.apply_scale(scale)
    exported = trimesh.exchange.stl.export_stl(mesh)
    return exported if isinstance(exported, bytes) else bytes(exported)
