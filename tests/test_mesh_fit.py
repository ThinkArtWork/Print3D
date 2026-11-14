from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import trimesh
from mcp.shared.memory import create_connected_server_and_client_session

from print3d.blender import BlenderResult
from print3d.mesh_fit import fit_script
from print3d.server import Runtime, build_server


def _call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    result = asyncio.run(scenario())
    return json.loads(result.content[0].text)


def _cube(path: Path, size: float = 20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    trimesh.creation.box((size, size, size)).export(path)
    return path


def _fake_blender(monkeypatch: pytest.MonkeyPatch, workspace) -> None:
    def fake_run(script, *, workdir, **kwargs):
        model = Path(workdir) / "model.stl"
        _cube(model, 10.0)
        return BlenderResult(
            ok=True,
            exit_code=0,
            duration_s=0.01,
            stdout="FIT_BBOX (10.0, 10.0, 10.0)",
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

    monkeypatch.setattr("print3d.server.run_blender", fake_run)


def test_fit_script_contains_exact_envelope_and_budget(tmp_path: Path) -> None:
    source = _cube(tmp_path / "source.stl")
    script = fit_script(source, (116.0, 101.0, 44.0), 145_000)
    assert repr(str(source)) in script
    assert "TARGET = Vector((116.0, 101.0, 44.0))" in script
    assert "MAX_TRIS = 145000" in script
    assert "DECIMATE" in script
    assert "holes_fill" in script


def test_fit_script_rejects_bad_arguments(tmp_path: Path) -> None:
    source = _cube(tmp_path / "source.stl")
    with pytest.raises(ValueError):
        fit_script(source, (0.0, 10.0, 10.0), 145_000)
    with pytest.raises(ValueError):
        fit_script(source, (10.0, 10.0, 10.0), 10)


def test_mesh_fit_requires_exact_confirm(workspace) -> None:
    runtime = Runtime.create(workspace=workspace)
    source = _cube(workspace.root / "source.stl")
    payload = _call(
        runtime,
        "mesh_fit",
        {"name": "fitted", "size_mm": [10, 10, 10], "source": str(source), "confirm": "FIT"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_mismatch"


def test_mesh_fit_requires_one_source(workspace) -> None:
    runtime = Runtime.create(workspace=workspace)
    payload = _call(
        runtime,
        "mesh_fit",
        {"name": "fitted", "size_mm": [10, 10, 10], "confirm": "FIT fitted"},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_mesh_fit_builds_an_asset_and_reuses_it(workspace, monkeypatch) -> None:
    _fake_blender(monkeypatch, workspace)
    runtime = Runtime.create(workspace=workspace)
    source = _cube(workspace.root / "source.stl")

    first = _call(
        runtime,
        "mesh_fit",
        {
            "name": "fitted",
            "size_mm": [10, 20, 30],
            "source": str(source),
            "confirm": "FIT fitted",
            "max_triangles": 400_000,
        },
    )
    assert first["ok"] is True
    assert first["asset_id"].startswith("a_")
    assert first["requested_size_mm"] == [10.0, 20.0, 30.0]
    assert first["source"]["path"] == str(source)
    assert first["viewer_file"].endswith("fitted-viewer.html")
    assert (workspace.root / first["viewer_file"]).is_file()

    second = _call(
        runtime,
        "mesh_fit",
        {
            "name": "fitted-copy",
            "size_mm": [5, 5, 5],
            "asset_id": first["asset_id"],
            "confirm": "FIT fitted-copy",
        },
    )
    assert second["ok"] is True
    assert second["source"]["asset_id"] == first["asset_id"]
    assert second["source"]["revision"] == "v001"


def test_fit_script_supports_uniform_longest_side(tmp_path: Path) -> None:
    source = _cube(tmp_path / "source.stl")
    script = fit_script(source, None, 145_000, max_dimension_mm=120.0)
    assert "UNIFORM_MM = 120.0" in script
    assert "TARGET = Vector((0.0, 0.0, 0.0))" in script
    assert "max(extent.x, extent.y, extent.z)" in script
    with pytest.raises(ValueError):
        fit_script(source, (10, 20, 30), 145_000, max_dimension_mm=100.0)
    with pytest.raises(ValueError):
        fit_script(source, None, 145_000)


def test_mesh_fit_uniform_mode_builds_asset(workspace, monkeypatch) -> None:
    _fake_blender(monkeypatch, workspace)
    runtime = Runtime.create(workspace=workspace)
    source = _cube(workspace.root / "source.stl")
    payload = _call(
        runtime,
        "mesh_fit",
        {
            "name": "uniform-fit",
            "max_dimension_mm": 120,
            "source": str(source),
            "confirm": "FIT uniform-fit",
        },
    )
    assert payload["ok"] is True
    assert payload["requested_size_mm"] is None
    assert payload["max_dimension_mm"] == 120.0
    assert payload["viewer_file"].endswith("uniform-fit-viewer.html")


def test_generate_cloud_rejects_both_sizing_modes(workspace) -> None:
    runtime = Runtime.create(workspace=workspace)
    payload = _call(
        runtime,
        "generate_cloud",
        {
            "provider": "hunyuan",
            "name": "bad-sizing",
            "confirm": "CLOUD bad-sizing",
            "prompt": "a cup",
            "size_mm": [10, 20, 30],
            "max_dimension_mm": 100,
        },
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
