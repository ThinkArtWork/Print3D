"""End-to-end tests for the ``model_generate`` tool with a fake LLM modeler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from xprint.assets import AssetStore
from xprint.modelers import ChatReply
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


def _reply(*tool_calls: tuple[str, dict]) -> ChatReply:
    return ChatReply(
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for index, (name, args) in enumerate(tool_calls)
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        finish_reason="tool_calls",
    )


class FakeModeler:
    """Scripted tool-calling model: write_script, then (optionally) run_cad, then finish."""

    def __init__(self, code: str = GOOD, *, execute: bool = False) -> None:
        self.code = code
        self.execute = execute
        self.calls: list[dict] = []
        self.round = 0

    def chat(self, messages, *, tools=None, tool_choice=None):
        self.calls.append(
            {"messages": json.loads(json.dumps(messages)), "tool_choice": tool_choice}
        )
        self.round += 1
        if self.round == 1:
            return _reply(("write_script", {"code": self.code}))
        if self.round == 2 and self.execute:
            return _reply(("run_cad", {}))
        return _reply(("finish", {"summary": "done"}))


def _runtime(workspace, modeler: FakeModeler) -> Runtime:
    registry = builtin_registry()
    registry.register("modeler", "kimi", lambda **kwargs: modeler, replace=True)
    return Runtime.create(workspace=workspace, registry=registry)


def call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = asyncio.run(scenario())
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
    assert payload["usage"]["total_tokens"] == 30
    assert payload["checks"]["ok"] is True
    assert payload["build"]["ok"] is False
    assert payload["build"]["finished"] is True
    assert len(modeler.calls) == 2
    assert modeler.calls[0]["tool_choice"] == "required"

    script = workspace.root / payload["script_path"]
    assert script.is_file()
    assert "build_part" in script.read_text()
    revision_dir = script.parent
    assert (revision_dir / "params.json").is_file()
    assert (revision_dir / "agent_trace.json").is_file()

    listing = call(runtime, "asset_list", {})
    assert [item["name"] for item in listing["assets"]] == ["cube"]
    assert listing["assets"][0]["revisions"] == 1


def test_model_generate_runs_the_script_and_stores_artifacts(
    workspace, monkeypatch, tmp_path: Path
) -> None:
    from xprint.cad import RunResult

    def fake_run(script, *, workdir=None, timeout_s=0, python=None):
        out = Path(workdir)
        (out / "model.stl").write_text("solid")
        return RunResult(
            ok=True,
            exit_code=0,
            checks={"watertight": True, "bbox_mm": [20.0, 20.0, 20.0]},
            artifacts={"stl": str(out / "model.stl")},
        )

    monkeypatch.setattr("xprint.agent.run_script", fake_run)
    modeler = FakeModeler(execute=True)
    runtime = _runtime(workspace, modeler)
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "cube", "name": "cube", "confirm": "MODEL cube kimi"},
    )
    assert payload["ok"] is True
    assert payload["build"]["ok"] is True
    assert payload["artifacts"] == ["model.stl"]
    revision = workspace.root / Path(payload["script_path"]).parent
    assert (revision / "model.stl").is_file()
    params = json.loads((revision / "params.json").read_text())
    assert params["build"]["checks"]["watertight"] is True


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
    assert "subprocess" in json.dumps(payload["error"]["details"])
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
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
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
    content = modeler.calls[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_model_generate_rounds_are_bounded(workspace) -> None:
    modeler = FakeModeler()
    runtime = _runtime(workspace, modeler)
    payload = call(
        runtime,
        "model_generate",
        {"prompt": "cube", "name": "cube", "confirm": "MODEL cube kimi", "rounds": 1},
    )
    assert payload["ok"] is True
    assert payload["build"]["rounds"] == 1
    assert payload["build"]["finished"] is False
    assert len(modeler.calls) == 1
