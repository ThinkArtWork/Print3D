from __future__ import annotations

import json

from print3d.capabilities import build_capabilities, build_system_status
from print3d.config import load_all
from print3d.jobs import JobStore
from print3d.registry import builtin_registry


def test_capabilities_shape(workspace) -> None:
    loaded, secrets = load_all()
    payload = build_capabilities(loaded, secrets, builtin_registry())
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert {p["id"] for p in payload["providers"]["cloud"]} == {"hunyuan", "tripo"}
    assert payload["providers"]["usable"] == []
    assert "cad" in payload and "render" in payload
    openai = next(item for item in payload["images"] if item["id"] == "openai")
    assert openai["model"] == "gpt-image-1"
    assert openai["has_key"] is False
    assert openai["ready"] is False
    assert payload["summary"]["image_providers_ready"] == 0
    assert isinstance(payload["warnings"], list)
    json.dumps(payload)


def test_provider_key_marks_usable(workspace, monkeypatch) -> None:
    monkeypatch.setenv("TRIPO_API_KEY", "k")
    loaded, secrets = load_all()
    payload = build_capabilities(loaded, secrets, builtin_registry())
    assert "tripo" in payload["providers"]["usable"]
    assert payload["providers"]["default"] in (None, "tripo", "hunyuan")


def test_hunyuan_api_key_is_recognised(workspace, monkeypatch) -> None:
    monkeypatch.setenv("HUNYUAN3D_API_KEY", "k")
    loaded, secrets = load_all()
    payload = build_capabilities(loaded, secrets, builtin_registry())
    hunyuan = next(p for p in payload["providers"]["cloud"] if p["id"] == "hunyuan")
    assert hunyuan["adapter"] == "hunyuan_openai"
    assert hunyuan["base_url"] == "https://api.ai3d.cloud.tencent.com"
    assert hunyuan["has_key"] is True
    assert hunyuan["key_source"] == "env"


def test_no_targets_means_no_slicer_surface(workspace) -> None:
    loaded, secrets = load_all()
    payload = build_capabilities(loaded, secrets, builtin_registry())
    assert payload["slicers"] == []
    assert payload["printers"] == []


def test_system_status(workspace) -> None:
    loaded, secrets = load_all()
    store = JobStore(workspace)
    job = store.create(kind="generate_cloud")
    payload = build_system_status(loaded, secrets, workspace, store, builtin_registry())
    assert payload["ok"] is True
    assert payload["writes_performed"] is False
    assert payload["workspace"]["root"] == str(workspace.root)
    assert payload["workspace"]["config_present"] is False
    assert payload["jobs"]["queued"] == 1
    assert job.id
    assert set(payload["adapters"]) == {
        "provider.cloud",
        "provider.local",
        "image",
        "cad",
        "render",
        "slicer",
        "printer",
    }


def test_cad_reports_the_build123d_worker(workspace) -> None:
    import importlib.util

    loaded, secrets = load_all()
    cad = build_capabilities(loaded, secrets, builtin_registry())["cad"]
    if importlib.util.find_spec("build123d") is None:
        assert cad["available"] is False
        assert "build123d" in cad["reason"]
        assert "uv pip install" in cad["hint"]
    else:
        assert cad["available"] is True
        assert cad["build123d"]
        assert cad["worker"].endswith("cad_worker.py")
        assert cad["reason"] is None


def test_cad_reports_a_missing_build123d(workspace, monkeypatch) -> None:
    from print3d import capabilities

    monkeypatch.setattr(
        capabilities, "_build123d_version", lambda python: (None, "current interpreter")
    )
    loaded, secrets = load_all()
    cad = build_capabilities(loaded, secrets, builtin_registry())["cad"]
    assert cad["available"] is False
    assert cad["reason"] == "build123d is not installed in current interpreter"
    assert "uv pip install" in cad["hint"]


def test_render_reports_the_blender_executable(workspace, monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.setattr("print3d.blender.find_blender", lambda explicit=None: Path("/opt/Blender"))
    loaded, secrets = load_all()
    render = build_capabilities(loaded, secrets, builtin_registry())["render"]
    assert render["available"] is True
    assert render["executable"] == "/opt/Blender"
    assert render["reason"] is None


def test_render_reports_a_bad_blender_path(workspace, monkeypatch) -> None:
    from print3d.errors import InvalidArgument

    def refuse(explicit=None):
        raise InvalidArgument("Blender not found at /nope", hint="Set render.blender_path.")

    monkeypatch.setattr("print3d.blender.find_blender", refuse)
    loaded, secrets = load_all()
    render = build_capabilities(loaded, secrets, builtin_registry())["render"]
    assert render["available"] is False
    assert "not found" in render["reason"]
    assert render["hint"] == "Set render.blender_path."
