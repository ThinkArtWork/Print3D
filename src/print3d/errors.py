"""Unified error structure.

Every failure surfaced to an MCP host carries a machine-readable ``code``, a
human-readable ``message``, an actionable ``hint`` (how to fix it) and optional
``details``.  Tool handlers convert :class:`Print3DError` into a normal tool
result with ``ok: false`` so hosts can show the hint instead of a stack trace.

Security note: never put API keys into ``message``/``details``.  Use
:func:`print3d.config.redact` on any provider payload before including it.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, documented error codes (kept in sync with docs/CLI output)."""

    CONFIG_INVALID = "config_invalid"
    CONFIG_MISSING = "config_missing"
    SECRETS_INSECURE = "secrets_insecure"
    PATH_INVALID = "path_invalid"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENT = "invalid_argument"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    ADAPTER_UNKNOWN = "adapter_unknown"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    JOB_NOT_FOUND = "job_not_found"
    JOB_INVALID_STATE = "job_invalid_state"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL = "internal_error"


class Print3DError(Exception):
    """Base class for all expected Print3D failures."""

    code: ErrorCode = ErrorCode.INTERNAL
    default_hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint if hint is not None else self.default_hint
        self.details = dict(details or {})
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "hint": self.hint,
            "details": _jsonable(self.details),
        }

    def __str__(self) -> str:  # keep logs readable
        text = f"[{self.code.value}] {self.message}"
        if self.hint:
            text += f" (hint: {self.hint})"
        return text


class ConfigError(Print3DError):
    code = ErrorCode.CONFIG_INVALID
    default_hint = "Run `print3d init` to write a default config, or fix config.json."


class ConfigMissing(ConfigError):
    code = ErrorCode.CONFIG_MISSING


class SecretsInsecure(Print3DError):
    code = ErrorCode.SECRETS_INSECURE
    default_hint = "chmod 600 ~/.print3d/secrets.json"


class PathError(Print3DError):
    code = ErrorCode.PATH_INVALID
    default_hint = "Pass a path relative to the workspace, or an absolute path inside it."


class PathOutsideWorkspace(PathError):
    code = ErrorCode.PATH_OUTSIDE_WORKSPACE


class NotFound(Print3DError):
    code = ErrorCode.NOT_FOUND


class InvalidArgument(Print3DError):
    code = ErrorCode.INVALID_ARGUMENT


class ConfirmationMismatch(Print3DError):
    code = ErrorCode.CONFIRMATION_MISMATCH
    default_hint = "Copy the exact confirm string returned by the plan/preflight call."


class AdapterUnknown(Print3DError):
    code = ErrorCode.ADAPTER_UNKNOWN


class AdapterUnavailable(Print3DError):
    code = ErrorCode.ADAPTER_UNAVAILABLE


class JobNotFound(NotFound):
    code = ErrorCode.JOB_NOT_FOUND


class JobStateError(Print3DError):
    code = ErrorCode.JOB_INVALID_STATE


class NotImplementedYet(Print3DError):
    code = ErrorCode.NOT_IMPLEMENTED


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so details always serialise to JSON."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Convert any exception into the public error envelope."""
    if isinstance(exc, Print3DError):
        return exc.to_dict()
    return {
        "code": ErrorCode.INTERNAL.value,
        "message": str(exc) or exc.__class__.__name__,
        "hint": "Re-run with `print3d --verbose` for a traceback.",
    }


def fail(exc: BaseException) -> dict[str, Any]:
    """Standard tool-result envelope for a failure."""
    return {"ok": False, "error": error_payload(exc)}


def ok(**data: Any) -> dict[str, Any]:
    """Standard tool-result envelope for success."""
    return {"ok": True, **data}
