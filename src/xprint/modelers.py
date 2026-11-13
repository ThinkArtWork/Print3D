"""LLM "modelers": text (and optionally an image) in, a CAD script out.

Design note: providers in :mod:`xprint.providers` return meshes; modelers return
*parametric source*.  Kimi K3.1 Pro models 3D by writing Blender/CAD scripts, so
it plugs in here, on the same build123d path as ``model_write`` — XPrint keeps
the script, the version history and the build, instead of a mesh blob nobody can
edit.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .errors import AdapterUnavailable, InvalidArgument
from .modelers_policy import SYSTEM_PROMPT  # re-exported below

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class GeneratedScript:
    code: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    elapsed_s: float = 0.0
    raw_chars: int = 0


@dataclass
class ChatReply:
    """One assistant turn, including any tool calls, as returned by the model."""

    message: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    elapsed_s: float = 0.0

    @property
    def content(self) -> str:
        return str(self.message.get("content") or "")

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        calls = self.message.get("tool_calls") or []
        return [call for call in calls if isinstance(call, dict)]


def extract_code(content: str) -> str:
    """Pull the CAD script out of a chat reply.

    Kimi is asked for a single fenced block, but models occasionally add prose or
    two blocks.  Prefer the fenced block that actually imports build123d, then
    the longest fenced block, then the whole message.
    """

    blocks = [match.group(1) for match in FENCE_RE.finditer(content or "")]
    for block in blocks:
        if "build123d" in block:
            return block.strip("\n") + "\n"
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip("\n") + ("\n" if text else "")


def _image_part(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if not mime:
        raise InvalidArgument(
            f"Unsupported image type: {suffix or '(none)'}",
            hint="Use PNG, JPEG or WebP.",
        )
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise InvalidArgument(
            f"Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
            hint="Downscale the photo before sending it to the modeler.",
        )
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


class KimiModeler:
    """Kimi (Moonshot) chat completion specialised for CAD script output."""

    id = "kimi"
    provider = "kimi"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.moonshot.cn/v1",
        model: str = "kimi-k3.1-pro",
        reasoning_effort: str | None = "high",
        timeout_s: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise AdapterUnavailable(
                "Kimi API key is not configured",
                hint=(
                    "Export MOONSHOT_API_KEY, or put it under providers.kimi in "
                    "~/.xprint/secrets.json (chmod 600)."
                ),
                details={"provider": "kimi"},
            )
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self._transport = transport

    # -- http ------------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_s, connect=10.0),
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        started = time.monotonic()
        try:
            with self._client() as client:
                response = client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise AdapterUnavailable(
                f"Kimi timed out after {self.timeout_s:.0f}s",
                hint="Retry, or lower reasoning_effort in config.modelers.kimi.",
                details={"provider": "kimi"},
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterUnavailable(
                f"Could not reach Kimi at {self.base_url}: {exc.__class__.__name__}",
                hint="Check network access and modelers.kimi.base_url.",
                details={"provider": "kimi"},
            ) from exc

        elapsed = time.monotonic() - started
        if response.status_code in {401, 403}:
            raise AdapterUnavailable(
                "Kimi rejected the API key",
                hint="Create a new key at platform.kimi.com and update MOONSHOT_API_KEY.",
                details={"provider": "kimi", "status": response.status_code},
            )
        if response.status_code == 429:
            raise AdapterUnavailable(
                "Kimi rate limit reached",
                hint="Wait a moment and retry, or use kimi-k3.1-pro-highspeed.",
                details={"provider": "kimi", "status": 429},
            )
        if response.status_code >= 400:
            raise AdapterUnavailable(
                f"Kimi returned HTTP {response.status_code}",
                hint="Inspect modelers.kimi.model and base_url; the response body is included in details.",
                details={
                    "provider": "kimi",
                    "status": response.status_code,
                    "body": response.text[:400],
                },
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterUnavailable(
                "Kimi returned a non-JSON response",
                hint="Retry; if it persists, check modelers.kimi.base_url.",
                details={"provider": "kimi", "body": response.text[:200]},
            ) from exc
        if not isinstance(data, dict):
            raise AdapterUnavailable(
                "Kimi returned an unexpected response shape",
                hint="Retry; if it persists, the API contract may have changed.",
                details={"provider": "kimi"},
            )
        return data, elapsed

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatReply:
        """One completion request, returning the raw assistant message.

        The raw message is what must be appended to the conversation verbatim
        when the assistant called tools; reconstructing it loses ``tool_calls``
        and ``reasoning_content`` and breaks the follow-up turn.
        """

        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        data, elapsed = self._post(payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterUnavailable(
                "Kimi returned an unexpected response shape",
                hint="Retry; if it persists, the API contract may have changed.",
                details={"provider": "kimi"},
            ) from exc
        if not isinstance(message, dict):
            raise AdapterUnavailable(
                "Kimi returned an unexpected response shape",
                hint="Retry; if it persists, the API contract may have changed.",
                details={"provider": "kimi"},
            )

        usage = {
            "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
            "completion_tokens": data.get("usage", {}).get("completion_tokens"),
            "total_tokens": data.get("usage", {}).get("total_tokens"),
            "elapsed_s": round(elapsed, 2),
        }
        reply = ChatReply(
            message=message,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            elapsed_s=round(elapsed, 2),
        )
        if not reply.content.strip() and not reply.tool_calls:
            raise AdapterUnavailable(
                "Kimi returned an empty message",
                hint="Retry with a more specific prompt.",
                details={"provider": "kimi", "usage": usage},
            )
        return reply

    def complete(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any], str | None]:
        reply = self.chat(messages)
        return reply.content, reply.usage, reply.finish_reason

    # -- public ----------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        image: Path | None = None,
        extra_system: str | None = None,
        job_context: dict[str, Any] | None = None,
    ) -> GeneratedScript:
        if not isinstance(prompt, str) or not prompt.strip():
            raise InvalidArgument("prompt must be a non-empty string")

        system = SYSTEM_PROMPT
        if extra_system:
            system = f"{system}\n{extra_system.strip()}"
        if job_context:
            system = f"{system}\n\nMachine context:\n{_render_context(job_context)}"

        user_content: Any = prompt.strip()
        if image is not None:
            user_content = [_image_part(image), {"type": "text", "text": prompt.strip()}]

        content, usage, finish_reason = self.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
        )
        return GeneratedScript(
            code=extract_code(content),
            provider=self.provider,
            model=self.model,
            usage=usage,
            finish_reason=finish_reason,
            elapsed_s=float(usage.get("elapsed_s") or 0.0),
            raw_chars=len(content),
        )


def _render_context(context: dict[str, Any]) -> str:
    lines = []
    for key in ("printer_model", "build_volume_mm", "material", "process"):
        value = context.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) or "- none"


def kimi_from_config(
    settings: dict[str, Any],
    *,
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> KimiModeler:
    return KimiModeler(
        api_key or "",
        base_url=str(settings.get("base_url") or "https://api.moonshot.cn/v1"),
        model=str(settings.get("model") or "kimi-k3.1-pro"),
        reasoning_effort=str(settings.get("reasoning_effort") or "high"),
        timeout_s=float(settings.get("timeout_s") or 180.0),
        transport=transport,
    )


__all__ = [
    "GeneratedScript",
    "KimiModeler",
    "SYSTEM_PROMPT",
    "extract_code",
    "kimi_from_config",
]
