from __future__ import annotations

import pytest

from print3d.errors import AdapterUnknown, InvalidArgument
from print3d.registry import Registry, builtin_registry


def test_register_get_create() -> None:
    reg = Registry()

    def factory(**kwargs: object) -> dict:
        return {"kwargs": kwargs}

    reg.register("provider.cloud", "fake", factory)
    assert reg.has("provider.cloud", "fake")
    assert reg.names("provider.cloud") == ["fake"]
    assert reg.create("provider.cloud", "fake", base_url="x") == {"kwargs": {"base_url": "x"}}


def test_unknown_lists_available() -> None:
    reg = Registry()
    reg.register("provider.cloud", "hunyuan", lambda **_: None)
    with pytest.raises(AdapterUnknown) as info:
        reg.get("provider.cloud", "nope")
    assert "hunyuan" in str(info.value)
    assert info.value.to_dict()["details"]["available"] == ["hunyuan"]


def test_duplicate_registration_requires_replace() -> None:
    reg = Registry()
    reg.register("cad", "text2cad", lambda: 1)
    with pytest.raises(InvalidArgument):
        reg.register("cad", "text2cad", lambda: 2)
    reg.register("cad", "text2cad", lambda: 2, replace=True)
    assert reg.create("cad", "text2cad") == 2


def test_unknown_kind_rejected() -> None:
    reg = Registry()
    with pytest.raises(AdapterUnknown):
        reg.register("bogus", "x", lambda: None)


def test_snapshot_shape() -> None:
    reg = builtin_registry()
    snap = reg.snapshot()
    assert set(snap) == {
        "provider.cloud",
        "provider.local",
        "image",
        "cad",
        "render",
        "slicer",
        "printer",
    }
    assert snap["image"] == ["openai"]


def test_unregister() -> None:
    reg = Registry()
    reg.register("slicer", "orca", lambda: None)
    reg.unregister("slicer", "orca")
    assert not reg.has("slicer", "orca")
    assert len(reg) == 0
