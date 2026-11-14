"""Tests for the self-contained interactive 3D viewer."""

from __future__ import annotations

import json

import numpy as np
import pytest
from tests.stl_helpers import TETRA, write_stl

from xprint.assets import AssetStore
from xprint.viewer import MAX_VIEW_TRIANGLES, VIEWER_FILE, viewer_html, write_viewer


def _triangles():
    return np.array(TETRA, dtype=np.float64)


def test_viewer_html_embeds_geometry_and_meta() -> None:
    html = viewer_html(
        _triangles(),
        title="tetra <demo>",
        subtitle="revision v001",
        revision="v001",
        name="tetra",
    )
    assert html.startswith("<!doctype html>")
    assert "tetra &lt;demo&gt;" in html
    assert "__POSITIONS__" not in html
    assert "__NORMALS__" not in html
    assert "const META = " in html
    start = html.index("const META = ") + len("const META = ")
    end = html.index(";\n", start)
    meta = json.loads(html[start:end])
    assert meta["triangles"] == 4
    assert meta["bbox_mm"] == [10.0, 10.0, 10.0]
    assert meta["volume_cm3"] > 0
    assert meta["revision"] == "v001"


def test_viewer_rejects_empty_and_oversized_meshes(monkeypatch) -> None:
    with pytest.raises(ValueError, match="no triangles"):
        viewer_html(np.zeros((0, 3, 3)), title="x", subtitle="y", revision="v001")

    monkeypatch.setattr("xprint.viewer.MAX_VIEW_TRIANGLES", 2)
    with pytest.raises(ValueError, match="supports at most"):
        viewer_html(_triangles(), title="x", subtitle="y", revision="v001")


def test_write_viewer_creates_the_html_next_to_the_stl(tmp_path) -> None:
    stl = tmp_path / "model.stl"
    write_stl(stl)

    result = write_viewer(
        stl,
        title="tetra",
        subtitle="revision v001 · interactive 3D",
        revision="v001",
        name="tetra",
    )
    target = tmp_path / VIEWER_FILE
    assert result["path"] == target
    assert target.is_file()
    assert result["triangles"] == 4
    assert result["bytes"] == target.stat().st_size
    assert target.stat().st_mode & 0o777 == 0o644


def test_viewer_tool_requires_a_build_and_the_exact_confirm(workspace, tmp_path) -> None:
    from tests.test_model_tools import GOOD, _runtime, call

    runtime = _runtime(workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    asset_id = written["asset_id"]

    missing = call(runtime, "model_view", {"asset_id": asset_id, "confirm": f"VIEW {asset_id}"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "invalid_argument"
    assert "model_build" in missing["error"]["hint"]

    wrong = call(runtime, "model_view", {"asset_id": asset_id, "confirm": "VIEW nope"})
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "confirmation_mismatch"

    store = AssetStore(workspace)
    mesh = tmp_path / "mesh.stl"
    write_stl(mesh)
    store.record_build(asset_id, "v001", build={"ok": True}, artifacts={"model.stl": mesh})

    payload = call(runtime, "model_view", {"asset_id": asset_id, "confirm": f"VIEW {asset_id}"})
    assert payload["ok"] is True
    assert payload["writes_performed"] is True
    assert payload["revision"] == "v001"
    assert payload["triangles"] == 4
    assert payload["viewer_path"].endswith(f"v001/{VIEWER_FILE}")
    assert (workspace.root / payload["viewer_path"]).is_file()
    assert "present" in payload["hint"]


def test_max_is_high_enough_for_normal_parts() -> None:
    assert MAX_VIEW_TRIANGLES >= 150_000
