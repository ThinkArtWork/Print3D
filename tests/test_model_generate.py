"""End-to-end tests for the ``model_generate`` tool with a fake LLM modeler."""

from __future__ import annotations

import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session

from xprint.assets import AssetStore
from xprint.modelers import GeneratedScript
from xprint.registry import builtin_registry
from xprint.server import Runtime, build_server

GOOD = """\
from build123d import *

PARAMS = {"size": 20.0}

def build_part():
    return Box(PARAMS["size"], PARAMS["size"], PARAMS["size"])

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


class FakeModeler:
    def __init__(self, code: str = GOOD) -> None:
        self.code = code
        self.calls: list[dict] = []

    def generate(self, prompt, *, image=None, extra_system=None, job_context=None):
        self.calls.append({"prompt": prompt, "image": image})
        return GeneratedScript(
            code=self.code,
            provider="kimi",
            model="kimi-k3.1-pro",
            usage={"total_tokens": 321},
        )


def _runtime(workspace, modeler: FakeModeler) -> Runtime:
    registry = builtin_registry()
    registry.register("modeler", "kimi", lambda **kwargs: modeler, replace=True)
    return Runtime.create(workspace=workspace, registry=registry)


def run(coro):
    return asyncio.run(coro)


def call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = run(scenario())
    return json.loads(result.content[0].text)


def test_model_generate_stores_a_revision(workspace) -> None:
    modeler = FakeModeler()
    runtime = _runtime(workspace, modeler)
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "a 20 mm cube", "name": "cube", "confirm": "MODEL cube kimi"},
    )
    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["revision"] == "v001"
    assert payload["usage"] == {"total_tokens": 321}
    assert payload["checks"]["ok"] is True

    script = workspace.root / payload["script_path"]
    assert script.is_file()
    assert "build_part" in script.read_text()
    assert modeler.calls == [{"prompt": "a 20 mm cube", "image": None}]

    listing = call(runtime, "asset_list", {})
    assert [item["name"] for item in listing["assets"]] == ["cube"]
    assert listing["assets"][0]["revisions"] == 1


def test_model_generate_requires_exact_confirm(workspace) -> None:
    modeler = FakeModeler()
    runtime = _runtime(workspace, modeler)
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "cube", "name": "cube", "confirm": "model cube kimi"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert payload["error"]["details"]["expected"] == "MODEL cube kimi"
    assert modeler.calls == []
    assert AssetStore(workspace).list() == []


def test_model_generate_rejects_unsafe_script(workspace) -> None:
    bad = "import subprocess\n" + GOOD
    runtime = _runtime(workspace, FakeModeler(bad))
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "cube", "name": "cube", "confirm": "MODEL cube kimi"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert any("subprocess" in issue["message"] for issue in payload["error"]["details"]["issues"])
    assert AssetStore(workspace).list() == []


def test_model_generate_unknown_modeler(workspace) -> None:
    runtime = _runtime(workspace, FakeModeler())
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "cube", "name": "cube", "confirm": "MODEL cube nope", "modeler": "nope"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "kimi" in payload["error"]["hint"]


def test_model_generate_forwards_image(workspace) -> None:
    image = workspace.root / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    modeler = FakeModeler()
    runtime = _runtime(workspace, modeler)
    payload = call(
        runtime,
        "model_generate",
        {
            "prompt": "model this",
            "name": "from-photo",
            "confirm": "MODEL from-photo kimi",
            "image": "photo.png",
        },
    )
    assert payload["ok"] is True
    assert modeler.calls[0]["image"] == image
