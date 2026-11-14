"""Tests for the bring-your-own-model MCP tools: model_write / model_build / model_show."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session
from tests.stl_helpers import binary_stl_bytes

from print3d.assets import AssetStore
from print3d.server import Runtime, build_server

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
    from print3d.cad import RunResult

    def fake_run(script, *, workdir=None, input_step=None, timeout_s=0, python=None):
        out = Path(workdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model.stl").write_bytes(binary_stl_bytes())
        (out / "model.step").write_text("ISO-10303")
        return RunResult(
            ok=True,
            exit_code=0,
            checks={"watertight": True, "bbox_mm": [20.0, 20.0, 20.0], "volume_mm3": 8000.0},
            artifacts={"stl": str(out / "model.stl"), "step": str(out / "model.step")},
        )

    monkeypatch.setattr("print3d.server.run_script", fake_run)
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
    assert payload["viewer_file"].endswith("viewer.html")
    assert Path(payload["viewer_file"]).is_file()
    assert payload["viewer_path"].startswith("assets/")
    listed = call(runtime, "asset_list", {})
    assert listed["assets"][0]["latest_revision"] == "v001"
    assert payload["viewer_path"].endswith("/cube-viewer.html")

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
    assert shown["revisions"][0]["built"] is True
    assert shown["viewer"].endswith(f"{asset_id}/cube-viewer.html")
    assert shown["revisions"][0]["viewer"] == shown["viewer"]
    assert (workspace.root / shown["viewer"]).is_file()
    assert shown["revisions"][0]["volume_mm3"] == 8000.0


def test_model_build_requires_exact_confirm(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(runtime, "model_build", {"asset_id": written["asset_id"], "confirm": "build it"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


def test_model_build_reports_a_failed_run(workspace, monkeypatch) -> None:
    from print3d.cad import RunResult

    def fake_run(script, *, workdir=None, input_step=None, timeout_s=0, python=None):
        return RunResult(
            ok=False,
            exit_code=1,
            error="ValueError: radius must be positive",
            stderr="Traceback ...\nValueError: radius must be positive",
        )

    monkeypatch.setattr("print3d.server.run_script", fake_run)
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


STEP_STUB = (
    "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('stub'),'2;1');\nENDSEC;\n"
    "DATA;\nENDSEC;\nEND-ISO-10303-21;\n"
)


def test_model_build_stores_and_exposes_a_step_input(
    workspace, monkeypatch, tmp_path: Path
) -> None:
    from print3d.cad import RunResult

    seen: dict[str, Path | None] = {"input_step": None}

    def fake_run(script, *, workdir=None, input_step=None, timeout_s=0, python=None):
        seen["input_step"] = Path(input_step) if input_step else None
        out = Path(workdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model.stl").write_bytes(binary_stl_bytes())
        (out / "model.step").write_text("ISO-10303")
        return RunResult(
            ok=True,
            exit_code=0,
            checks={"watertight": True, "bbox_mm": [20.0, 10.0, 6.0]},
            artifacts={"stl": str(out / "model.stl"), "step": str(out / "model.step")},
        )

    monkeypatch.setattr("print3d.server.run_script", fake_run)
    source = tmp_path / "bracket.step"
    source.write_text(STEP_STUB, encoding="utf-8")
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "bracket", "confirm": "WRITE bracket"}
    )
    asset_id = written["asset_id"]

    payload = call(
        runtime,
        "model_build",
        {"asset_id": asset_id, "input": str(source), "confirm": f"BUILD {asset_id}"},
    )
    assert payload["ok"] is True
    assert payload["input"]["file"] == "input.step"
    assert payload["input"]["original_name"] == "bracket.step"
    assert payload["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert payload["input"]["bytes"] == len(source.read_bytes())

    revision = workspace.root / Path(written["script_path"]).parent
    assert (revision / "input.step").read_text(encoding="utf-8") == STEP_STUB
    assert seen["input_step"] == revision / "input.step"
    params = json.loads((revision / "params.json").read_text(encoding="utf-8"))
    assert params["build"]["input"]["original_name"] == "bracket.step"

    shown = call(runtime, "model_show", {"asset_id": asset_id})
    assert shown["revisions"][0]["input"]["original_name"] == "bracket.step"


def test_model_build_rejects_bad_step_inputs(workspace, tmp_path: Path) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    asset_id = written["asset_id"]

    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    payload = call(
        runtime,
        "model_build",
        {"asset_id": asset_id, "input": str(notes), "confirm": f"BUILD {asset_id}"},
    )
    assert payload["error"]["code"] == "invalid_argument"

    payload = call(
        runtime,
        "model_build",
        {
            "asset_id": asset_id,
            "input": str(tmp_path / "missing.step"),
            "confirm": f"BUILD {asset_id}",
        },
    )
    assert payload["error"]["code"] == "not_found"

    fake = tmp_path / "fake.step"
    fake.write_text("definitely not a step file", encoding="utf-8")
    payload = call(
        runtime,
        "model_build",
        {"asset_id": asset_id, "input": str(fake), "confirm": f"BUILD {asset_id}"},
    )
    assert payload["error"]["code"] == "invalid_argument"
    assert "STEP" in payload["error"]["message"]


def test_model_drawing_records_a_sheet(workspace, monkeypatch, tmp_path: Path) -> None:
    from print3d.cad import RunResult
    from print3d.drawing import DrawingResult

    def fake_run(script, *, workdir=None, input_step=None, timeout_s=0, python=None):
        out = Path(workdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model.stl").write_bytes(binary_stl_bytes())
        (out / "model.step").write_text("ISO-10303")
        return RunResult(
            ok=True,
            exit_code=0,
            checks={"watertight": True},
            artifacts={"stl": str(out / "model.stl"), "step": str(out / "model.step")},
        )

    def fake_drawing(
        step, *, workdir=None, views="", scale=1.0, hidden=True, timeout_s=0, python=None
    ):
        assert Path(step).name == "model.step"
        out = Path(workdir)
        out.mkdir(parents=True, exist_ok=True)
        svg = out / "drawing.svg"
        svg.write_text("<svg></svg>", encoding="utf-8")
        dxf = out / "drawing.dxf"
        dxf.write_text("0\nEOF\n", encoding="utf-8")
        return DrawingResult(
            ok=True,
            exit_code=0,
            checks={
                "scale": scale,
                "hidden": hidden,
                "sheet_mm": [55.21, 42.74],
                "views": [{"name": "front", "visible_edges": 4, "hidden_edges": 0}],
                "edges": {"visible": 21, "hidden": 15, "labels": 261},
            },
            files={"drawing.svg": str(svg), "drawing.dxf": str(dxf)},
        )

    monkeypatch.setattr("print3d.server.run_script", fake_run)
    monkeypatch.setattr("print3d.server.run_drawing", fake_drawing)
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    asset_id = written["asset_id"]
    call(runtime, "model_build", {"asset_id": asset_id, "confirm": f"BUILD {asset_id}"})

    payload = call(
        runtime, "model_drawing", {"asset_id": asset_id, "confirm": f"DRAWING {asset_id}"}
    )
    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["sheet_mm"] == [55.21, 42.74]
    assert payload["files"] == ["drawing.dxf", "drawing.svg"]
    assert payload["edges"]["labels"] == 261

    revision = workspace.root / Path(written["script_path"]).parent
    assert (revision / "drawing.svg").read_text(encoding="utf-8") == "<svg></svg>"
    assert (revision / "drawing.dxf").is_file()

    shown = call(runtime, "model_show", {"asset_id": asset_id})
    assert shown["revisions"][0]["drawing"] == ["drawing.dxf", "drawing.svg"]

    exported = call(
        runtime,
        "asset_export",
        {
            "asset_id": asset_id,
            "artifact": "svg",
            "confirm": f"EXPORT {asset_id}",
            "out_path": str(tmp_path / "sheet.svg"),
        },
    )
    assert exported["ok"] is True
    assert (tmp_path / "sheet.svg").read_text(encoding="utf-8") == "<svg></svg>"


def test_model_drawing_requires_a_built_step(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    asset_id = written["asset_id"]
    payload = call(
        runtime, "model_drawing", {"asset_id": asset_id, "confirm": f"DRAWING {asset_id}"}
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "model_build" in payload["error"]["hint"]

    exported = call(
        runtime,
        "asset_export",
        {"asset_id": asset_id, "artifact": "dxf", "confirm": f"EXPORT {asset_id}"},
    )
    assert exported["error"]["code"] == "not_found"


def test_model_drawing_requires_exact_confirm(workspace) -> None:
    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(
        runtime, "model_drawing", {"asset_id": written["asset_id"], "confirm": "draw it"}
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


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
    monkeypatch.setattr("print3d.server.render_views", lambda *a, **k: png)

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


def test_blender_run_rejects_a_non_bpy_script(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "blender_run",
        {"script": "print('hi')\n", "name": "scene", "confirm": "BLENDER scene"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert not list(workspace.assets_dir.glob("*")) if workspace.assets_dir.exists() else True


def test_blender_run_requires_exact_confirm(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "blender_run",
        {
            "script": "import bpy\nbpy.ops.mesh.primitive_cube_add()\n",
            "name": "scene",
            "confirm": "BLENDER",
        },
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


def test_generate_local_rejects_unknown_provider(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "generate_local",
        {"provider": "nope", "name": "cube", "confirm": "LOCAL cube", "prompt": "a cube"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "providers.local" in (payload["error"]["hint"] or "")


def test_generate_local_runs_a_provider_and_creates_an_asset(workspace, tmp_path) -> None:
    import sys

    provider = tmp_path / "provider.py"
    provider.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path.cwd() / 'tests')!r})\n"
        "from stl_helpers import binary_stl_bytes\n"
        "Path(os.environ['PRINT3D_LOCAL_OUT'], 'mesh.stl').write_bytes(binary_stl_bytes())\n",
        encoding="utf-8",
    )
    provider.chmod(0o755)

    config = {
        "providers": {
            "local": {
                "fake": {
                    "adapter": "fake",
                    "mode": "both",
                    "command": [sys.executable, str(provider)],
                    "args": ["--prompt", "{prompt}", "--out", "{outdir}"],
                    "timeout_s": 30,
                }
            }
        }
    }
    workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
    runtime = _runtime(workspace)

    submitted = call(
        runtime,
        "generate_local",
        {
            "provider": "fake",
            "name": "tetra",
            "confirm": "LOCAL tetra",
            "prompt": "a tetrahedron",
        },
    )
    assert submitted["ok"] is True
    job_id = submitted["job_id"]

    import time

    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = call(runtime, "job_status", {"job_id": job_id})
        if status["job"]["state"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.05)

    assert status is not None
    assert status["job"]["state"] == "succeeded", status
    assert status["job"]["result"]["checks"]["triangles"] == 4
    assert Path(status["job"]["result"]["viewer_file"]).is_file()
    assets = call(runtime, "asset_list", {})
    assert any(item["name"] == "tetra" for item in assets["assets"])


def test_generate_local_requires_prompt_or_image(workspace) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "generate_local",
        {"provider": "fake", "name": "cube", "confirm": "LOCAL cube"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_generate_cloud_creates_an_asset(workspace, monkeypatch) -> None:
    from tests.stl_helpers import binary_stl_bytes

    from print3d.cloud import CloudTask

    class FakeCloud:
        def submit(self, *, prompt=None, image=None):
            assert prompt == "a bracket"
            return "task-42"

        def poll(self, task_id):
            return CloudTask(
                task_id=task_id,
                status="success",
                progress=100,
                model_url="https://cdn.test/model.stl",
                credits=10.0,
            )

        def close(self):
            pass

    monkeypatch.setattr("print3d.server.build_provider", lambda *a, **k: FakeCloud())
    monkeypatch.setattr(
        "print3d.server.download_model",
        lambda url, **k: (binary_stl_bytes(), ".stl"),
    )

    workspace.config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "cloud": {"tripo": {"adapter": "tripo", "api_key_env": "TRIPO_API_KEY"}}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRIPO_API_KEY", "test-key")
    runtime = _runtime(workspace)

    submitted = call(
        runtime,
        "generate_cloud",
        {
            "provider": "tripo",
            "name": "cloud-tetra",
            "confirm": "CLOUD cloud-tetra",
            "prompt": "a bracket",
        },
    )
    assert submitted["ok"] is True, submitted
    job_id = submitted["job_id"]

    import time

    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = call(runtime, "job_status", {"job_id": job_id})
        if status["job"]["state"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.05)

    assert status is not None
    assert status["job"]["state"] == "succeeded", status
    result = status["job"]["result"]
    assert result["checks"]["triangles"] == 4
    assert result["checks"]["engine"] == "cloud:tripo"
    assert result["task_id"] == "task-42"
    assets = call(runtime, "asset_list", {})
    assert any(item["name"] == "cloud-tetra" for item in assets["assets"])
    assert Path(status["job"]["result"]["viewer_file"]).is_file()


def test_generate_cloud_rejects_confirm_and_unknown_provider(workspace, monkeypatch) -> None:
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "generate_cloud",
        {"provider": "tripo", "name": "cube", "confirm": "LOCAL cube", "prompt": "x"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"

    payload = call(
        runtime,
        "generate_cloud",
        {"provider": "nope", "name": "cube", "confirm": "CLOUD cube", "prompt": "x"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert "providers.cloud" in (payload["error"]["hint"] or "")

    workspace.config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "cloud": {
                        "hunyuan": {
                            "adapter": "hunyuan_openai",
                            "api_key_env": "PRINT3D_TEST_MISSING_KEY",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PRINT3D_TEST_MISSING_KEY", raising=False)
    runtime = _runtime(workspace)
    payload = call(
        runtime,
        "generate_cloud",
        {"provider": "hunyuan", "name": "cube", "confirm": "CLOUD cube", "prompt": "x"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "adapter_unavailable"
    assert "PRINT3D_TEST_MISSING_KEY" in (payload["error"]["hint"] or "")


def test_generate_cloud_can_fit_generated_mesh_to_size(workspace, monkeypatch) -> None:
    from tests.stl_helpers import binary_stl_bytes

    from print3d.blender import BlenderResult
    from print3d.cloud import CloudTask

    class FakeCloud:
        def submit(self, *, prompt=None, image=None):
            return "task-fit"

        def poll(self, task_id):
            return CloudTask(
                task_id=task_id,
                status="success",
                progress=100,
                model_url="https://cdn.test/model.stl",
                credits=3.0,
            )

        def close(self):
            pass

    def fake_blender(script, *, workdir, **kwargs):
        model = Path(workdir) / "model.stl"
        model.parent.mkdir(parents=True, exist_ok=True)
        import trimesh

        trimesh.creation.box((10.0, 10.0, 10.0)).export(model)
        return BlenderResult(
            ok=True,
            exit_code=0,
            duration_s=0.01,
            stdout="FIT_MODE uniform",
            checks={
                "triangles": 12,
                "vertices": 8,
                "bbox_mm": [10.0, 10.0, 10.0],
                "watertight": True,
                "boundary_edges": 0,
                "nonmanifold_edges": 0,
                "is_valid": True,
            },
            artifacts={"model.stl": str(model)},
        )

    monkeypatch.setattr("print3d.server.build_provider", lambda *a, **k: FakeCloud())
    monkeypatch.setattr(
        "print3d.server.download_model",
        lambda url, **k: (binary_stl_bytes(), ".stl"),
    )
    monkeypatch.setattr("print3d.server.run_blender", fake_blender)

    workspace.config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "cloud": {"tripo": {"adapter": "tripo", "api_key_env": "TRIPO_API_KEY"}}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRIPO_API_KEY", "test-key")
    runtime = _runtime(workspace)
    submitted = call(
        runtime,
        "generate_cloud",
        {
            "provider": "tripo",
            "name": "cloud-fitted",
            "confirm": "CLOUD cloud-fitted",
            "prompt": "a bracket",
            "max_dimension_mm": 120,
        },
    )
    assert submitted["ok"] is True, submitted
    assert submitted["fit_requested"] is True

    import time

    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = call(runtime, "job_status", {"job_id": submitted["job_id"]})
        if status["job"]["state"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.05)

    assert status is not None
    assert status["job"]["state"] == "succeeded", status
    result = status["job"]["result"]
    assert result["fit"]["mode"] == "uniform"
    assert result["fit"]["max_dimension_mm"] == 120.0
    assert result["checks"]["fit"]["mode"] == "uniform"
    assert result["checks"]["triangles"] == 12
    assert any(name == "source.stl" for name in result["artifacts"])
