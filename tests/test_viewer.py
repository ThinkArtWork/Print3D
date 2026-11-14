"""Tests for the self-contained interactive 3D viewer."""

from __future__ import annotations

import json

import numpy as np
import pytest
from tests.stl_helpers import TETRA, write_stl

from print3d.assets import AssetStore
from print3d.viewer import (
    MAX_VIEW_TRIANGLES,
    VIEWER_FILE,
    viewer_alias_name,
    viewer_html,
    write_viewer,
)


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

    monkeypatch.setattr("print3d.viewer.MAX_VIEW_TRIANGLES", 2)
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
    assert payload["viewer_path"].endswith(f"{asset_id}/cube-viewer.html")
    assert payload["viewer_revision"] == "v001"
    assert payload["viewer_revisions"] == 1
    html = (workspace.root / payload["viewer_path"]).read_text(encoding="utf-8")
    assert '"revisions"' in html
    assert '"v001"' in html
    assert (workspace.root / payload["viewer_path"]).is_file()
    assert "present" in payload["hint"]


def test_max_is_high_enough_for_normal_parts() -> None:
    assert MAX_VIEW_TRIANGLES >= 150_000


def test_collection_viewer_embeds_every_revision_in_one_file(tmp_path) -> None:
    from print3d.viewer import collection_html, entry_from_stl, write_collection_viewer

    first = tmp_path / "v001.stl"
    second = tmp_path / "v002.stl"
    write_stl(first)
    write_stl(second)
    entries = [
        entry_from_stl(first, revision="v001", created_at="2026-11-14T01:00:00Z"),
        entry_from_stl(second, revision="v002", created_at="2026-11-14T02:00:00Z"),
    ]

    html = collection_html(
        entries,
        title="plate",
        subtitle="2 revisions",
        name="plate",
        selected="v001",
    )
    assert 'id="revSel"' in html
    assert html.count('"revision": "v00') == 3
    assert '"revision": "v001"' in html  # selected revision wins the default camera

    written = write_collection_viewer(
        tmp_path / "asset",
        entries,
        title="plate",
        name="plate",
        selected="v002",
    )
    assert written["revisions"] == ["v001", "v002"]
    assert written["triangles"] == 8
    target = tmp_path / "asset" / "viewer.html"
    assert written["path"] == target
    assert target.is_file()
    assert written["alias_path"].name == "plate-viewer.html"
    assert written["alias_path"].read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
    body = target.read_text(encoding="utf-8")
    assert '"revision": "v002"' in body
    assert '"stl": "v002/model.stl"' in body


def test_viewer_alias_name_is_readable_and_safe() -> None:
    assert viewer_alias_name("TourBox Neo") == "TourBox-Neo-viewer.html"
    assert viewer_alias_name("产品 模型/01") == "产品-模型-01-viewer.html"
    assert viewer_alias_name("") == "model-viewer.html"


def test_legacy_per_revision_viewer_redirects_to_the_asset_viewer(tmp_path) -> None:
    from print3d.viewer import write_legacy_redirect

    target = write_legacy_redirect(tmp_path / "v001", title="plate")
    text = target.read_text(encoding="utf-8")
    assert target.name == "viewer.html"
    assert 'http-equiv="refresh"' in text
    assert "url=../viewer.html" in text
    assert "plate" in text
