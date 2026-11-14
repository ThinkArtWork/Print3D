"""Tests for the printability_check and orientation_plan MCP tools."""

from __future__ import annotations

import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session
from tests.stl_helpers import write_stl

from xprint.assets import AssetStore
from xprint.server import Runtime, build_server

GOOD = """\
from build123d import *

def build_part():
    return Box(10, 10, 10)

if __name__ == "__main__":
    export_stl(build_part(), "model.stl")
"""


def call(runtime, tool, args):
    async def scenario():
        async with create_connected_server_and_client_session(build_server(runtime)) as client:
            await client.initialize()
            return await client.call_tool(tool, args)

    return json.loads(asyncio.run(scenario()).content[0].text)


def _written(workspace) -> dict:
    runtime = Runtime.create(workspace=workspace)
    return call(runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"})


def _built(workspace, tmp_path) -> tuple[Runtime, dict]:
    runtime = Runtime.create(workspace=workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    stl = tmp_path / "built.stl"
    write_stl(stl)
    AssetStore(workspace).record_build(
        written["asset_id"], written["revision"], build={"ok": True}, artifacts={"model.stl": stl}
    )
    return runtime, written


def test_printability_check_reports_metrics(workspace, tmp_path) -> None:
    runtime, written = _built(workspace, tmp_path)
    payload = call(runtime, "printability_check", {"asset_id": written["asset_id"]})

    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert payload["revision"] == "v001"
    assert payload["metrics"]["triangles"] == 4
    assert 0.0 <= payload["score"] <= 100.0
    assert payload["wall"]["samples"] > 0
    assert payload["bed_mm"] is None


def test_printability_check_requires_a_build(workspace) -> None:
    runtime = Runtime.create(workspace=workspace)
    written = call(
        runtime, "model_write", {"script": GOOD, "name": "cube", "confirm": "WRITE cube"}
    )
    payload = call(runtime, "printability_check", {"asset_id": written["asset_id"]})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert "model_build" in payload["error"]["hint"]


def test_orientation_plan_returns_ranked_candidates(workspace, tmp_path) -> None:
    runtime, written = _built(workspace, tmp_path)
    payload = call(runtime, "orientation_plan", {"asset_id": written["asset_id"], "limit": 3})

    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert [item["rank"] for item in payload["candidates"]] == [1, 2, 3]
    assert len(payload["recommended"]["rotation"]) == 3
    assert len(payload["recommended"]["down_axis"]) == 3
    assert payload["current"]["score"] >= 0.0
    assert payload["candidates"][0]["score"] >= payload["candidates"][-1]["score"]


def test_orientation_plan_validates_limit(workspace, tmp_path) -> None:
    runtime, written = _built(workspace, tmp_path)
    payload = call(runtime, "orientation_plan", {"asset_id": written["asset_id"], "limit": 0})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


def test_printability_check_unknown_asset(workspace) -> None:
    runtime = Runtime.create(workspace=workspace)
    payload = call(runtime, "printability_check", {"asset_id": "a_20990101T000000Z_ffffff"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
