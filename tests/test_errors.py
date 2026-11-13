from __future__ import annotations

from xprint.errors import (
    ConfigError,
    InvalidArgument,
    JobNotFound,
    PathOutsideWorkspace,
    XPrintError,
    error_payload,
    fail,
    ok,
)


def test_error_payload_shape() -> None:
    exc = ConfigError("bad config", hint="fix it", details={"path": "x"})
    payload = exc.to_dict()
    assert set(payload) == {"code", "message", "hint", "details"}
    assert payload["code"] == "config_invalid"
    assert payload["message"] == "bad config"
    assert payload["hint"] == "fix it"
    assert payload["details"] == {"path": "x"}


def test_error_str_includes_code_and_hint() -> None:
    text = str(PathOutsideWorkspace("escape", hint="stay inside"))
    assert "path_outside_workspace" in text
    assert "escape" in text
    assert "stay inside" in text


def test_helpers() -> None:
    assert ok(a=1) == {"ok": True, "a": 1}
    exc = InvalidArgument("nope")
    assert fail(exc)["ok"] is False
    assert fail(exc)["error"]["code"] == "invalid_argument"
    assert error_payload(exc)["message"] == "nope"


def test_subclass_codes() -> None:
    assert JobNotFound("x").to_dict()["code"] == "job_not_found"
    assert XPrintError("x").to_dict()["code"] == "internal_error"


def test_details_default_to_empty_dict() -> None:
    assert XPrintError("x").to_dict()["details"] == {}
