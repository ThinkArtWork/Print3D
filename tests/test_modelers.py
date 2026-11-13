from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from xprint.errors import AdapterUnavailable, InvalidArgument
from xprint.modelers import KimiModeler, extract_code, kimi_from_config

SCRIPT = """\
from build123d import *

PARAMS = {"size": 20.0}

def build_part():
    return Box(PARAMS["size"], PARAMS["size"], PARAMS["size"])
"""

REPLY = f"Here you go:\n\n```python\n{SCRIPT}```\nThis prints without supports."


def _reply(content: str = REPLY, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }


def test_generate_happy_path() -> None:
    modeler = KimiModeler("test-key", reasoning_effort="high")
    with respx.mock(base_url="https://api.moonshot.cn/v1") as mock:
        route = mock.post("/chat/completions").mock(return_value=httpx.Response(200, json=_reply()))
        result = modeler.generate("a 20 mm cube")

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "kimi-k3.1-pro"
    assert body["reasoning_effort"] == "high"
    assert body["messages"][0]["role"] == "system"
    assert "build123d" in body["messages"][0]["content"]

    assert result.code.startswith("from build123d import *")
    assert result.usage["total_tokens"] == 300
    assert result.usage["prompt_tokens"] == 100
    assert result.finish_reason == "stop"


def test_image_is_sent_inline(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    modeler = KimiModeler("k")
    with respx.mock(base_url="https://api.moonshot.cn/v1") as mock:
        route = mock.post("/chat/completions").mock(return_value=httpx.Response(200, json=_reply()))
        modeler.generate("model this", image=image)

    content = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "model this"}


def test_rejects_unknown_image_type(tmp_path: Path) -> None:
    image = tmp_path / "photo.gif"
    image.write_bytes(b"GIF89a")
    with pytest.raises(InvalidArgument):
        KimiModeler("k").generate("x", image=image)


def test_auth_error_is_actionable() -> None:
    modeler = KimiModeler("bad-key")
    with respx.mock(base_url="https://api.moonshot.cn/v1") as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "bad key"})
        )
        with pytest.raises(AdapterUnavailable) as info:
            modeler.generate("cube")
    assert "API key" in str(info.value)
    assert "bad-key" not in str(info.value)


def test_rate_limit() -> None:
    with respx.mock(base_url="https://api.moonshot.cn/v1") as mock:
        mock.post("/chat/completions").mock(return_value=httpx.Response(429))
        with pytest.raises(AdapterUnavailable) as info:
            KimiModeler("k").generate("cube")
    assert info.value.to_dict()["code"] == "adapter_unavailable"


def test_empty_content() -> None:
    with respx.mock(base_url="https://api.moonshot.cn/v1") as mock:
        mock.post("/chat/completions").mock(return_value=httpx.Response(200, json=_reply("")))
        with pytest.raises(AdapterUnavailable):
            KimiModeler("k").generate("cube")


def test_requires_key() -> None:
    with pytest.raises(AdapterUnavailable):
        KimiModeler("")
    with pytest.raises(AdapterUnavailable):
        kimi_from_config({}, api_key=None)


def test_extract_code_variants() -> None:
    assert extract_code("```python\nA\n```").strip() == "A"
    assert extract_code("text\n```\nB\n```").strip() == "B"
    two = "```python\nprint('x')\n```\n```python\nfrom build123d import *\n```"
    assert "build123d" in extract_code(two)
    assert extract_code("from build123d import *\n") == "from build123d import *\n"
