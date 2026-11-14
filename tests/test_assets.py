from __future__ import annotations

import json

import pytest

from xprint.assets import AssetStore, new_asset_id, validate_asset_id, validate_name
from xprint.errors import InvalidArgument, NotFound


def test_asset_id_and_name_validation() -> None:
    asset_id = new_asset_id()
    assert validate_asset_id(asset_id) == asset_id
    with pytest.raises(InvalidArgument):
        validate_asset_id("../../etc/passwd")
    with pytest.raises(InvalidArgument):
        validate_asset_id("a_short")
    for bad in ("", "UPPER", "with space", "-nope", "x"):
        with pytest.raises(InvalidArgument):
            validate_name(bad)


def test_create_and_add_revisions(workspace) -> None:
    store = AssetStore(workspace)
    manifest = store.create("desk-organizer")
    asset_id = manifest["asset_id"]
    assert (workspace.assets_dir / asset_id / "asset.json").is_file()

    first = store.add_revision(
        asset_id,
        "from build123d import *\ndef build_part():\n    return Box(1, 1, 1)\n",
        prompt="a 1mm cube",
        model="kimi-k3.1-pro",
        modeler="kimi",
        usage={"total_tokens": 42},
    )
    assert first["revision"] == "v001"
    assert len(first["sha256"]) == 64
    script = workspace.assets_dir / asset_id / "v001" / "model.py"
    assert "Box" in script.read_text()
    params = json.loads((workspace.assets_dir / asset_id / "v001" / "params.json").read_text())
    assert params["prompt"] == "a 1mm cube"
    assert params["usage"] == {"total_tokens": 42}

    second = store.add_revision(
        asset_id,
        "from build123d import *\ndef build_part():\n    return Box(2, 2, 2)\n",
        prompt="a 2mm cube",
        model="kimi-k3.1-pro",
        modeler="kimi",
    )
    assert second["revision"] == "v002"
    assert len(store.load(asset_id)["revisions"]) == 2


def test_unknown_asset(workspace) -> None:
    store = AssetStore(workspace)
    with pytest.raises(NotFound):
        store.load("a_20260101T000000Z_abcdef")


def test_list(workspace) -> None:
    store = AssetStore(workspace)
    store.create("one")
    store.create("two")
    listing = store.list()
    assert {item["name"] for item in listing} == {"one", "two"}
    assert all(item["revisions"] == 0 for item in listing)
    assert all(item["latest_revision"] is None for item in listing)
