from __future__ import annotations

import json

from xprint.capabilities import build_capabilities, build_system_status
from xprint.config import load_all
from xprint.jobs import JobStore
from xprint.registry import builtin_registry


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
