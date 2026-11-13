"""Tests for the bring-your-own-model MCP tools: model_write / model_build / model_show."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from xprint.assets import AssetStore
from xprint.server import Runtime, build_server

GOOD = """\
from build123d import *

PARAMS = {"size": 20.0}

def build_part():
    return Box(PARAMS["size"], PARAMS["size"], PARAMS["size"])

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


def _runtime(workspace) -> Runtime:
    return Runtime.create(workspace=workspace)


def call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = asyncio.run(scenario())
    return json.loads(result.content[0].text)


def test_model_write_creates_an_asset(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["revision"] == "v001"
    assert payload["checks"]["ok"] is True
    assert payload["name"] == "cube"
    script = workspace.root / payload["script_path"]
    assert script.is_file()
    assert "build_part" in script.read_text()
    assert [item["name"] for item in AssetStore(workspace).list()] == ["cube"]


def test_model_write_appends_a_revision(workspace) -> None:
    runtime = _runtime(workspace)
    first = call(runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"})
    asset_id = first["asset_id"]
    payload = call(
        runtime,
        "model_write",
        {
            "script": GOOD.replace("20.0", "25.0"),
            "asset_id": asset_id,
            "confirm": f"WRITE {asset_id}",
            "prompt": "taller",
        },
    )
    assert payload["ok"] is True
    assert payload["revision"] == "v002"
    assert payload["name"] == "cube"
    assert "25.0" in (workspace.root / payload["script_path"]).read_text()


def test_model_write_requires_exact_confirm(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "write cube"}
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert payload["error"]["details"]["expected"] == "WRITE cube"
    assert AssetStore(workspace).list() == []


def test_model_write_rejects_an_unsafe_script(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "model_write",
        {"script": "import subprocess\n" + GOOD, "name": "cube", "confirm": "WRITE cube"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "subprocess" in json.dumps(payload["error"]["details"])
    assert AssetStore(workspace).list() == []


def test_model_write_needs_a_name_for_a_new_asset(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(runtime, "model_write", {"script": GOOD, "confirm": "WRITE "})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_model_build_runs_and_records(workspace, monkeypatch, tmp_path: Path) -> None:
    from xprint.cad import RunResult

    def fake_run(script, *, workdir=None, timeout_s=0, python=None):
        out = Path(workdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model.stl").write_text("solid cube")
        (out / "model.step").write_text("ISO-10303")
        return RunResult(
            ok=True,
            exit_code=0,
            checks={"watertight": True, "bbox_mm": [20.0, 20.0, 20.0], "volume_mm3": 8000.0},
            artifacts={"stl": str(out / "model.stl"), "step": str(out / "model.step")},
        )

    monkeypatch.setattr("xprint.server.run_script", fake_run)
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    asset_id = written["asset_id"]

    payload = call(runtime, "model_build", {"asset_id": asset_id, "confirm": f"BUILD {asset_id}"})
    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["checks"]["watertight"] is True
    assert payload["artifacts"] == ["model.step", "model.stl"]

    revision = workspace.root / Path(written["script_path"]).parent
    assert (revision / "model.stl").is_file()
    params = json.loads((revision / "params.json").read_text())
    assert params["build"]["checks"]["volume_mm3"] == 8000.0
    assert "model.stl" in params["artifacts"]

    shown = call(runtime, "model_show", {"asset_id": asset_id})
    assert shown["ok"] is True
    assert shown["writes_performed"] is False
    assert shown["revision"] == "v001"
    assert "build_part" in shown["script"]
    assert shown["revisions"][0]["build_ok"] is True


def test_model_build_requires_exact_confirm(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(runtime, "model_build", {"asset_id": written["asset_id"], "confirm": "build it"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


def test_model_build_reports_a_failed_run(workspace, monkeypatch) -> None:
    from xprint.cad import RunResult

    def fake_run(script, *, workdir=None, timeout_s=0, python=None):
        return RunResult(
            ok=False,
            exit_code=1,
            error="ValueError: radius must be positive",
            stderr="Traceback ...\nValueError: radius must be positive",
        )

    monkeypatch.setattr("xprint.server.run_script", fake_run)
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(
        runtime,
        "model_build",
        {"asset_id": written["asset_id"], "confirm": f"BUILD {written['asset_id']}"},
    )
    assert payload["ok"] is False
    assert "radius" in payload["error"]
    assert "Build failed" in payload["hint"]


def test_model_build_unknown_asset(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "model_build",
        {"asset_id": "a_20260101T000000Z_ab12cd", "confirm": "BUILD a_20260101T000000Z_ab12cd"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


def test_model_show_without_revisions(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "model_build",
        {"asset_id": "a_20260101T000000Z_ab12cd", "confirm": "BUILD a_20260101T000000Z_ab12cd"},
    )
    assert payload["error"]["code"] == "not_found"


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 80, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def test_model_write_attaches_a_reference(workspace) -> None:
    runtime = _runtime(workspace)
    reference = workspace.root / "references" / "concept.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(_png_bytes())

    payload = call(
        runtime,
        "model_write",
        {
            "script": GOOD,
            "name": "cube",
            "confirm": "WRITE cube",
            "reference": "references/concept.png",
            "prompt": "a 20 mm cube",
        },
    )

    assert payload["ok"] is True
    assert payload["reference"]["file"] == "reference.png"
    revision = workspace.root / payload["script_path"]
    assert (revision.parent / "reference.png").is_file()
    params = json.loads((revision.parent / "params.json").read_text())
    assert params["reference"]["meta"]["source_path"] == "references/concept.png"
    assert params["reference"]["sha256"] == payload["reference"]["sha256"]


def test_model_write_rejects_a_non_image_reference(workspace) -> None:
    runtime = _runtime(workspace)
    bad = workspace.root / "references" / "concept.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not an image")
    payload = call(
        runtime,
        "model_write",
        {
            "script": GOOD,
            "name": "cube",
            "confirm": "WRITE cube",
            "reference": "references/concept.png",
        },
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_model_preview_returns_an_image(workspace, monkeypatch) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    revision = workspace.root / written["script_path"]
    (revision.parent / "model.stl").write_bytes(b"binary-stl-placeholder")

    png = _png_bytes()
    monkeypatch.setattr("xprint.server.render_views", lambda *a, **k: png)

    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(
                "model_preview", {"asset_id": written["asset_id"], "views": "front"}
            )

    result = asyncio.run(scenario())
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["revision"] == "v001"
    assert payload["views"] == ["front"]
    assert payload["writes_performed"] is False
    images = [item for item in result.content if item.type == "image"]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert images[0].data


def test_model_preview_requires_a_built_model(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(runtime, "model_preview", {"asset_id": written["asset_id"]})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "model_build" in payload["error"]["hint"]


def test_reference_import_stores_an_uploaded_image(workspace, tmp_path) -> None:
    runtime = _runtime(workspace)
    source = tmp_path / "sketch.png"
    source.write_bytes(_png_bytes())

    payload = call(
        runtime,
        "reference_import",
        {"path": str(source), "name": "bracket", "confirm": "IMPORT bracket"},
    )

    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["mime"] == "image/png"
    stored = workspace.root / payload["image_path"]
    assert stored.is_file()
    assert stored.read_bytes() == source.read_bytes()
    assert payload["image_path"].startswith("references/")


def test_reference_import_rejects_a_non_image(workspace, tmp_path) -> None:
    runtime = _runtime(workspace)
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    payload = call(
        runtime,
        "reference_import",
        {"path": str(source), "name": "bracket", "confirm": "IMPORT bracket"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_asset_export_copies_the_stl(workspace, tmp_path) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    stl = tmp_path / "built.stl"
    stl.write_bytes(b"solid fake\nendsolid fake\n")
    AssetStore(workspace).record_build(
        written["asset_id"], written["revision"], build={"ok": True}, artifacts={"model.stl": stl}
    )

    destination = tmp_path / "exports" / "cube.stl"
    payload = call(
        runtime,
        "asset_export",
        {
            "asset_id": written["asset_id"],
            "confirm": f"EXPORT {written['asset_id']}",
            "out_path": str(destination),
        },
    )

    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert destination.read_bytes() == b"solid fake\nendsolid fake\n"
    assert payload["sha256"] == payload["sha256"].lower()
    assert payload["artifact"] == "stl"


def test_asset_export_requires_a_build(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(
        runtime,
        "asset_export",
        {"asset_id": written["asset_id"], "confirm": f"EXPORT {written['asset_id']}"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "model_build" in payload["error"]["hint"]
